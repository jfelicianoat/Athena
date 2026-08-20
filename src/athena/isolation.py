"""Giving two writers a workspace each, so they stop queueing behind one lock.

`GraphExecutor` serialises writers. That is correct and it is also the bottleneck: two
coders working on unrelated subsystems take turns for no reason other than that the runtime
cannot tell they are unrelated. A git worktree gives each one a real checkout of the same
repository, so they can work at the same time and the question of whether their changes
agree is asked once, afterwards, by looking at the diffs.

This ships after the shared-workspace executor rather than instead of it, and the ordering
was not caution for its own sake: debugging a scheduler and a filesystem-isolation layer at
the same time means never knowing which one is wrong.

Three things this deliberately does not do.

**It does not merge automatically.** Two diffs that both apply cleanly can still be wrong
together, and a runtime that merged on the strength of "no conflict" would be asserting
something git never claimed. Integration is a task, run by the executor like any other,
with its own verification.

**It does not isolate readers.** An explorer cannot corrupt anything, and giving it a
private checkout would cost a copy of the repository to protect against nothing.

**It does not survive the process.** A worktree that outlived its run would be an orphan
directory nobody remembers creating, on somebody's disk, with somebody's code in it.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from athena.cancellation import CancellationToken
from athena.errors import AthenaRuntimeError, ToolExecutionError
from athena.process_tools import run_process
from athena.subagents import SubagentRole
from athena.types import JSONObject
from athena.workspace import Workspace

#: How long a git plumbing command may take. Creating a worktree is a local operation;
#: anything slower than this is a repository in a state worth reporting rather than waiting
#: out.
_GIT_TIMEOUT_SECONDS = 120.0


class IsolationError(AthenaRuntimeError):
    code = "workspace_isolation_error"


class IsolationMode(StrEnum):
    """How much separation a task gets."""

    #: Everyone shares one checkout. Writers take turns. The default, and correct whenever
    #: the tasks are few or the repository is not a git repository at all.
    SHARED = "shared"
    #: Each writing task gets its own worktree. Readers still share.
    PER_WRITER = "per_writer"


@dataclass(frozen=True, slots=True)
class IsolatedWorkspace:
    """One task's view of the repository, and how to put it back."""

    workspace: Workspace
    #: `None` when the task is simply using the shared checkout, which is not a thing to
    #: tidy up afterwards.
    worktree_path: Path | None = None
    branch: str | None = None

    @property
    def is_isolated(self) -> bool:
        return self.worktree_path is not None

    def to_json(self) -> JSONObject:
        return {
            "root": str(self.workspace.root),
            "isolated": self.is_isolated,
            "branch": self.branch,
        }


class WorkspaceIsolationPolicy:
    """Decides who gets their own checkout, and creates it.

    The decision is deterministic and narrow: a writing role, in `PER_WRITER` mode, in a
    git repository. Anything else shares. There is no heuristic about whether two tasks
    "look independent" — that is the judgement a merge conflict exists to make, and it
    makes it with facts rather than guesses.
    """

    def __init__(
        self,
        mode: IsolationMode = IsolationMode.SHARED,
        *,
        writing_roles: Sequence[SubagentRole] = (SubagentRole.CODER,),
    ) -> None:
        self.mode = mode
        self.writing_roles = frozenset(writing_roles)
        self._created: dict[str, IsolatedWorkspace] = {}

    def wants_isolation(self, role: SubagentRole) -> bool:
        return self.mode is IsolationMode.PER_WRITER and role in self.writing_roles

    async def acquire(
        self,
        task_id: str,
        role: SubagentRole,
        workspace: Workspace,
        cancellation: CancellationToken,
    ) -> IsolatedWorkspace:
        """A workspace for this task: its own if it has earned one, the shared one if not.

        Falling back to the shared checkout when isolation is impossible — no git, a
        repository with no commits — is deliberate. Refusing to run would turn a
        performance feature into a prerequisite.
        """
        if not self.wants_isolation(role):
            return IsolatedWorkspace(workspace)
        if not await _is_git_repository(workspace.root, cancellation):
            return IsolatedWorkspace(workspace)
        try:
            isolated = await _create_worktree(task_id, workspace, cancellation)
        except (IsolationError, ToolExecutionError):
            # A worktree that could not be created is not a reason to abandon the task.
            return IsolatedWorkspace(workspace)
        self._created[task_id] = isolated
        return isolated

    async def release(self, task_id: str, cancellation: CancellationToken) -> None:
        """Remove the worktree. A run that ends does not leave checkouts behind."""
        isolated = self._created.pop(task_id, None)
        if isolated is None or isolated.worktree_path is None:
            return
        await _remove_worktree(isolated, cancellation)

    async def release_all(self, cancellation: CancellationToken) -> None:
        for task_id in tuple(self._created):
            await self.release(task_id, cancellation)

    def active(self) -> tuple[IsolatedWorkspace, ...]:
        return tuple(self._created.values())


async def _git(
    argv: tuple[str, ...], cwd: Path, cancellation: CancellationToken
) -> tuple[int, str, str]:
    """One git command, with the mandatory timeout every process in Athena carries."""
    return await run_process(
        argv, cwd=cwd, timeout_seconds=_GIT_TIMEOUT_SECONDS, cancellation=cancellation
    )


async def _is_git_repository(root: Path, cancellation: CancellationToken) -> bool:
    try:
        code, _, _ = await _git(("git", "rev-parse", "--git-dir"), root, cancellation)
    except (ToolExecutionError, OSError):
        return False
    return code == 0


async def _create_worktree(
    task_id: str, workspace: Workspace, cancellation: CancellationToken
) -> IsolatedWorkspace:
    """A detached worktree beside the repository, on its own branch.

    Beside rather than inside: a worktree under the repository root would be visible to
    every glob, grep and test run in the original checkout, and the first thing it would
    do is make the project's own test suite find two copies of everything.
    """
    suffix = uuid4().hex[:8]
    branch = f"athena/{task_id}-{suffix}"
    path = workspace.root.parent / ".athena-worktrees" / f"{workspace.root.name}-{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    code, _, error = await _git(
        ("git", "worktree", "add", "-b", branch, str(path), "HEAD"),
        workspace.root,
        cancellation,
    )
    if code != 0:
        raise IsolationError(
            "git refused to create a worktree",
            details={"task_id": task_id, "detail": error[-400:]},
        )
    return IsolatedWorkspace(
        workspace=Workspace.from_path(path),
        worktree_path=path,
        branch=branch,
    )


async def _remove_worktree(isolated: IsolatedWorkspace, cancellation: CancellationToken) -> None:
    """Ask git to remove it, then make sure it is gone.

    `git worktree remove` refuses when the checkout is dirty, which is exactly the state a
    task that did work leaves it in. The changes have already been read out as a diff by
    then, so forcing is not discarding anything nobody looked at.
    """
    path = isolated.worktree_path
    if path is None:
        return
    with _suppress_process_errors():
        await _git(("git", "worktree", "remove", "--force", str(path)), path.parent, cancellation)
    if path.exists():
        # git can leave the directory behind if it never fully registered the worktree.
        shutil.rmtree(path, ignore_errors=True)


class _suppress_process_errors:
    """A tidy-up failure must not become the run's failure."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        return isinstance(exc, (ToolExecutionError, OSError, asyncio.TimeoutError))


@dataclass(frozen=True, slots=True)
class IntegrationCandidate:
    """One isolated task's result, ready to be considered for integration."""

    task_id: str
    branch: str
    diff: str
    files: tuple[str, ...]


async def collect_diff(
    isolated: IsolatedWorkspace, task_id: str, cancellation: CancellationToken
) -> IntegrationCandidate | None:
    """What a task actually changed in its own checkout.

    Read as a diff rather than by copying files, because a diff is reviewable and a
    directory is not — and because the integration step needs to compare changes, not
    contents.
    """
    if isolated.worktree_path is None or isolated.branch is None:
        return None
    root = isolated.worktree_path
    staged, _, _ = await _git(("git", "add", "-A"), root, cancellation)
    if staged != 0:
        return None
    diff_code, diff_text, _ = await _git(("git", "diff", "--cached"), root, cancellation)
    names_code, names_text, _ = await _git(
        ("git", "diff", "--cached", "--name-only"), root, cancellation
    )
    if diff_code != 0 or names_code != 0:
        return None
    files = tuple(line.strip() for line in names_text.splitlines() if line.strip())
    if not files:
        return None
    return IntegrationCandidate(
        task_id=task_id, branch=isolated.branch, diff=diff_text, files=files
    )


def overlapping_files(
    candidates: Sequence[IntegrationCandidate],
) -> dict[str, tuple[str, ...]]:
    """Files more than one task changed.

    Detection, not resolution. Two tasks touching one file is a fact worth surfacing before
    anything is integrated; whether they actually disagree is a question for the merge, and
    a runtime that answered it by reading the diffs would be reimplementing git badly.
    """
    owners: dict[str, list[str]] = {}
    for candidate in candidates:
        for path in candidate.files:
            owners.setdefault(path, []).append(candidate.task_id)
    return {path: tuple(tasks) for path, tasks in sorted(owners.items()) if len(tasks) > 1}


__all__ = [
    "IntegrationCandidate",
    "IsolatedWorkspace",
    "IsolationError",
    "IsolationMode",
    "WorkspaceIsolationPolicy",
    "collect_diff",
    "overlapping_files",
]
