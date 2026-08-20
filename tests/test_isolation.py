"""Two writers, two checkouts, and nothing merged on trust.

These run against real git, because the interesting failures are git's: a worktree that
refuses to be removed while dirty, a repository with no commits, a directory that survives
a failed creation. A fake would agree with whatever this module assumed.

The rule under test throughout is that isolation is an optimisation and never a
prerequisite. Anything that cannot be isolated runs shared, because refusing to work would
turn a performance feature into a dependency on git.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from athena.cancellation import CancellationSource
from athena.isolation import (
    IntegrationCandidate,
    IsolationMode,
    WorkspaceIsolationPolicy,
    collect_diff,
    overlapping_files,
)
from athena.subagents import SubagentRole
from athena.workspace import Workspace


def _git(root: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=root, check=True, capture_output=True, timeout=60)


def _repository(tmp_path: Path) -> Workspace:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "athena@example.invalid")
    _git(root, "config", "user.name", "Athena Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    return Workspace.from_path(root)


def _plain(tmp_path: Path) -> Workspace:
    root = tmp_path / "plain"
    root.mkdir()
    (root / "notes.md").write_text("no git here\n", encoding="utf-8")
    return Workspace.from_path(root)


# ---------------------------------------------------------------------- who gets isolated


def test_a_reader_does_not_get_its_own_checkout() -> None:
    """An explorer cannot corrupt anything.

    Giving it a private copy of the repository would cost a checkout to protect against
    nothing at all.
    """
    policy = WorkspaceIsolationPolicy(IsolationMode.PER_WRITER)

    assert policy.wants_isolation(SubagentRole.CODER)
    assert not policy.wants_isolation(SubagentRole.EXPLORER)
    assert not policy.wants_isolation(SubagentRole.VERIFIER)


def test_shared_mode_isolates_nobody() -> None:
    # The default, and correct whenever the tasks are few enough that a lock costs less
    # than a checkout.
    policy = WorkspaceIsolationPolicy()

    assert not policy.wants_isolation(SubagentRole.CODER)


def test_a_writer_gets_a_real_checkout(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = _repository(tmp_path)
        policy = WorkspaceIsolationPolicy(IsolationMode.PER_WRITER)
        token = CancellationSource().token

        isolated = await policy.acquire("T01", SubagentRole.CODER, workspace, token)

        assert isolated.is_isolated
        assert isolated.workspace.root != workspace.root
        assert (isolated.workspace.root / "calc.py").exists(), "a real checkout, not an empty dir"
        assert isolated.branch is not None
        await policy.release_all(token)

    asyncio.run(scenario())


def test_the_checkout_lives_beside_the_repository_not_inside_it(tmp_path: Path) -> None:
    """Inside, every glob and every test run would find two copies of the project.

    The first thing that breaks is the project's own suite discovering its tests twice.
    """

    async def scenario() -> None:
        workspace = _repository(tmp_path)
        policy = WorkspaceIsolationPolicy(IsolationMode.PER_WRITER)
        token = CancellationSource().token

        isolated = await policy.acquire("T01", SubagentRole.CODER, workspace, token)

        assert isolated.worktree_path is not None
        assert workspace.root not in isolated.worktree_path.parents
        await policy.release_all(token)

    asyncio.run(scenario())


def test_two_writers_get_different_checkouts(tmp_path: Path) -> None:
    """The point of the whole module: they stop taking turns."""

    async def scenario() -> None:
        workspace = _repository(tmp_path)
        policy = WorkspaceIsolationPolicy(IsolationMode.PER_WRITER)
        token = CancellationSource().token

        first = await policy.acquire("T01", SubagentRole.CODER, workspace, token)
        second = await policy.acquire("T02", SubagentRole.CODER, workspace, token)

        assert first.workspace.root != second.workspace.root
        assert first.branch != second.branch
        assert len(policy.active()) == 2
        await policy.release_all(token)

    asyncio.run(scenario())


def test_changes_in_one_checkout_are_invisible_in_the_other(tmp_path: Path) -> None:
    # If they were not, the isolation would be decorative and the lock would still be
    # doing the real work.
    async def scenario() -> None:
        workspace = _repository(tmp_path)
        policy = WorkspaceIsolationPolicy(IsolationMode.PER_WRITER)
        token = CancellationSource().token
        first = await policy.acquire("T01", SubagentRole.CODER, workspace, token)
        second = await policy.acquire("T02", SubagentRole.CODER, workspace, token)

        (first.workspace.root / "calc.py").write_text("def add(a, b):\n    return 0\n")

        assert "return a + b" in (second.workspace.root / "calc.py").read_text()
        assert "return a + b" in (workspace.root / "calc.py").read_text()
        await policy.release_all(token)

    asyncio.run(scenario())


# --------------------------------------------------------- isolation is never a prerequisite


def test_a_repository_without_git_runs_shared(tmp_path: Path) -> None:
    """Refusing to work would turn a performance feature into a dependency on git."""

    async def scenario() -> None:
        workspace = _plain(tmp_path)
        policy = WorkspaceIsolationPolicy(IsolationMode.PER_WRITER)
        token = CancellationSource().token

        isolated = await policy.acquire("T01", SubagentRole.CODER, workspace, token)

        assert not isolated.is_isolated
        assert isolated.workspace.root == workspace.root

    asyncio.run(scenario())


def test_a_repository_with_no_commits_runs_shared(tmp_path: Path) -> None:
    # `git worktree add … HEAD` has nothing to point at. Falling back is the only useful
    # answer; failing the task would punish it for the repository's history.
    async def scenario() -> None:
        root = tmp_path / "fresh"
        root.mkdir()
        _git(root, "init", "-b", "main")
        workspace = Workspace.from_path(root)
        policy = WorkspaceIsolationPolicy(IsolationMode.PER_WRITER)

        isolated = await policy.acquire(
            "T01", SubagentRole.CODER, workspace, CancellationSource().token
        )

        assert not isolated.is_isolated

    asyncio.run(scenario())


# --------------------------------------------------------------------------- tidying up


def test_a_run_leaves_no_checkouts_behind(tmp_path: Path) -> None:
    """An orphan worktree is a directory nobody remembers creating, with somebody's code."""

    async def scenario() -> None:
        workspace = _repository(tmp_path)
        policy = WorkspaceIsolationPolicy(IsolationMode.PER_WRITER)
        token = CancellationSource().token
        isolated = await policy.acquire("T01", SubagentRole.CODER, workspace, token)
        path = isolated.worktree_path
        assert path is not None and path.exists()

        await policy.release("T01", token)

        assert not path.exists()
        assert policy.active() == ()

    asyncio.run(scenario())


def test_a_dirty_checkout_is_still_removed(tmp_path: Path) -> None:
    """`git worktree remove` refuses when the checkout is dirty.

    That is exactly the state a task that did work leaves it in, and by then the changes
    have already been read out as a diff — so forcing discards nothing nobody looked at.
    """

    async def scenario() -> None:
        workspace = _repository(tmp_path)
        policy = WorkspaceIsolationPolicy(IsolationMode.PER_WRITER)
        token = CancellationSource().token
        isolated = await policy.acquire("T01", SubagentRole.CODER, workspace, token)
        assert isolated.worktree_path is not None
        (isolated.workspace.root / "calc.py").write_text("changed\n", encoding="utf-8")

        await policy.release("T01", token)

        assert not isolated.worktree_path.exists()

    asyncio.run(scenario())


def test_releasing_something_that_was_never_isolated_is_not_an_error(tmp_path: Path) -> None:
    async def scenario() -> None:
        policy = WorkspaceIsolationPolicy(IsolationMode.PER_WRITER)

        await policy.release("never-existed", CancellationSource().token)

    del tmp_path
    asyncio.run(scenario())


# ------------------------------------------------------------------------- what changed


def test_a_task_s_work_comes_back_as_a_reviewable_diff(tmp_path: Path) -> None:
    """A diff rather than a directory, because a diff is reviewable and a directory is not."""

    async def scenario() -> None:
        workspace = _repository(tmp_path)
        policy = WorkspaceIsolationPolicy(IsolationMode.PER_WRITER)
        token = CancellationSource().token
        isolated = await policy.acquire("T01", SubagentRole.CODER, workspace, token)
        (isolated.workspace.root / "calc.py").write_text(
            "def add(a, b):\n    return a + b + 0\n", encoding="utf-8"
        )

        candidate = await collect_diff(isolated, "T01", token)

        assert candidate is not None
        assert candidate.files == ("calc.py",)
        assert "return a + b + 0" in candidate.diff
        assert candidate.branch == isolated.branch
        await policy.release_all(token)

    asyncio.run(scenario())


def test_a_task_that_changed_nothing_offers_nothing(tmp_path: Path) -> None:
    # An empty candidate would make the integration step consider a merge of nothing.
    async def scenario() -> None:
        workspace = _repository(tmp_path)
        policy = WorkspaceIsolationPolicy(IsolationMode.PER_WRITER)
        token = CancellationSource().token
        isolated = await policy.acquire("T01", SubagentRole.CODER, workspace, token)

        assert await collect_diff(isolated, "T01", token) is None
        await policy.release_all(token)

    asyncio.run(scenario())


def test_a_shared_workspace_has_no_diff_to_collect(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = _repository(tmp_path)
        policy = WorkspaceIsolationPolicy()
        token = CancellationSource().token
        shared = await policy.acquire("T01", SubagentRole.CODER, workspace, token)

        assert await collect_diff(shared, "T01", token) is None

    asyncio.run(scenario())


# ------------------------------------------------------------------------- integration


def test_two_tasks_touching_one_file_is_surfaced_before_anything_is_merged() -> None:
    """Detection, not resolution.

    Whether they actually disagree is a question for git. A runtime that answered it by
    reading the diffs would be reimplementing merge, badly.
    """
    candidates = [
        IntegrationCandidate("T01", "athena/T01", "diff", ("calc.py", "api.py")),
        IntegrationCandidate("T02", "athena/T02", "diff", ("calc.py",)),
        IntegrationCandidate("T03", "athena/T03", "diff", ("storage.py",)),
    ]

    overlaps = overlapping_files(candidates)

    assert overlaps == {"calc.py": ("T01", "T02")}
    assert "api.py" not in overlaps
    assert "storage.py" not in overlaps


def test_tasks_on_separate_files_have_nothing_to_reconcile() -> None:
    candidates = [
        IntegrationCandidate("T01", "athena/T01", "diff", ("api.py",)),
        IntegrationCandidate("T02", "athena/T02", "diff", ("storage.py",)),
    ]

    assert overlapping_files(candidates) == {}


def test_nothing_is_merged_automatically() -> None:
    """Two diffs that both apply cleanly can still be wrong together.

    Merging on the strength of "no conflict" would assert something git never claimed, so
    the module offers no way to do it.
    """
    import athena.isolation as isolation

    exported = set(isolation.__all__)
    assert not any("merge" in name.lower() for name in exported)
    assert not any("apply" in name.lower() for name in exported)
    assert "overlapping_files" in exported, "it reports, and leaves the decision alone"


def test_isolation_holds_no_agent_logic() -> None:
    import ast

    import athena

    module = Path(athena.__file__).parent / "isolation.py"
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    imported = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("athena")
    }

    assert "athena.agent_loop" not in imported
    assert "athena.models" not in imported, "it moves files, it does not think"


@pytest.mark.parametrize("mode", list(IsolationMode))
def test_every_mode_produces_a_usable_workspace(mode: IsolationMode, tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = _repository(tmp_path)
        policy = WorkspaceIsolationPolicy(mode)
        token = CancellationSource().token

        isolated = await policy.acquire("T01", SubagentRole.CODER, workspace, token)

        assert isolated.workspace.root.is_dir()
        assert (isolated.workspace.root / "calc.py").exists()
        await policy.release_all(token)

    asyncio.run(scenario())
