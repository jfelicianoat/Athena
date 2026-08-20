"""Where a task's work happens.

Today there is exactly one strategy: everyone shares the one workspace, and the
concurrency scheduler keeps conflicting writes apart. That is the minimum that is
actually needed, so it is the only thing implemented.

The abstraction exists because the alternatives are real and near — a git worktree per
writing task, a container, a remote host over SSH — and each of them answers the same
question ("give this task somewhere to work, then clean up"). Declaring that question now
means adding one later is implementing a strategy rather than rewriting the runtime.

None of them is implemented yet, and specifically:

**Worktrees are not built until parallel *writing* tasks demonstrably need them.** A
worktree per task buys isolation and costs a checkout, a merge, and a second copy of every
build artefact. Right now writes are serialised, so the isolation buys nothing and the cost
is real. The trigger to revisit is evidence that two write tasks genuinely need to run at
once — not the observation that worktrees exist.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from athena.errors import AthenaRuntimeError
from athena.types import JSONObject
from athena.workspace import Workspace


class IsolationKind(StrEnum):
    #: One workspace, shared; conflicting work is serialised rather than separated.
    SHARED = "shared"
    #: A git worktree per task. Declared, not implemented.
    WORKTREE = "worktree"
    #: A container per task. Declared, not implemented.
    CONTAINER = "container"
    #: A remote host per task. Declared, not implemented.
    REMOTE = "remote"


class WorkspaceIsolationUnavailable(AthenaRuntimeError):
    """The requested isolation exists as a concept but not yet as an implementation."""

    code = "workspace_isolation_unavailable"


@dataclass(frozen=True, slots=True)
class WorkspaceLease:
    """A task's claim on somewhere to work."""

    lease_id: str
    task_id: str
    workspace: Workspace
    kind: IsolationKind
    #: True when writes here can collide with another lease's writes.
    shared: bool = True
    metadata: JSONObject = field(default_factory=dict)


@runtime_checkable
class WorkspaceStrategy(Protocol):
    kind: IsolationKind

    async def acquire(self, task_id: str) -> WorkspaceLease: ...

    async def release(self, lease: WorkspaceLease) -> None: ...


class SharedWorkspaceStrategy:
    """Everyone works in the same directory.

    Safe only because the concurrency scheduler serialises conflicting writes. If that
    guarantee ever weakens, this strategy stops being adequate — which is precisely the
    condition that would justify building the worktree one.
    """

    kind = IsolationKind.SHARED

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self._leases: dict[str, WorkspaceLease] = {}

    async def acquire(self, task_id: str) -> WorkspaceLease:
        lease = WorkspaceLease(
            lease_id=f"shared:{task_id}",
            task_id=task_id,
            workspace=self.workspace,
            kind=self.kind,
            shared=True,
            metadata={"root": str(self.workspace.root)},
        )
        self._leases[lease.lease_id] = lease
        return lease

    async def release(self, lease: WorkspaceLease) -> None:
        # Nothing to tear down: the directory outlives every lease on it.
        self._leases.pop(lease.lease_id, None)

    def active(self) -> tuple[WorkspaceLease, ...]:
        return tuple(self._leases.values())


class UnimplementedStrategy:
    """Placeholder that refuses clearly instead of pretending.

    A strategy that silently fell back to the shared workspace would be worse than one that
    is missing: a caller asking for isolation would believe it had some.
    """

    def __init__(self, kind: IsolationKind, reason: str) -> None:
        self.kind = kind
        self.reason = reason

    async def acquire(self, task_id: str) -> WorkspaceLease:
        raise WorkspaceIsolationUnavailable(
            f"{self.kind.value} isolation is not implemented: {self.reason}",
            details={"task_id": task_id, "kind": self.kind.value},
        )

    async def release(self, lease: WorkspaceLease) -> None:
        del lease


def default_strategies(workspace: Workspace) -> Mapping[IsolationKind, WorkspaceStrategy]:
    return {
        IsolationKind.SHARED: SharedWorkspaceStrategy(workspace),
        IsolationKind.WORKTREE: UnimplementedStrategy(
            IsolationKind.WORKTREE,
            "writes are serialised today, so a worktree per task would cost a checkout "
            "and a merge to buy isolation nothing currently needs",
        ),
        IsolationKind.CONTAINER: UnimplementedStrategy(
            IsolationKind.CONTAINER, "no container runtime is a dependency of Athena"
        ),
        IsolationKind.REMOTE: UnimplementedStrategy(
            IsolationKind.REMOTE, "remote execution has no transport in the core"
        ),
    }


__all__ = [
    "IsolationKind",
    "SharedWorkspaceStrategy",
    "UnimplementedStrategy",
    "WorkspaceIsolationUnavailable",
    "WorkspaceLease",
    "WorkspaceStrategy",
    "default_strategies",
]
