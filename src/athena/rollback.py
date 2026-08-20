"""Undoing what Athena did, and refusing to undo anything else.

`CheckpointStore` has been able to snapshot and restore files since H2 and nothing called
it, so a task that broke the workspace left it broken. This is the layer that decides when
a checkpoint is worth taking and what a rollback is allowed to touch.

The second half is the important one. A rollback that reverted the workspace wholesale
would discard a person's uncommitted work along with the agent's mistake — and the person
would have no way to know it happened. So a rollback is scoped to files this run wrote,
and a file it did not write is left alone even when it stands between the workspace and a
clean state.

Scopes mirror cancellation's, and for the same reason: undoing one task must not undo the
one beside it, and undoing a run must undo everything under it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from athena.checkpoints import Checkpoint, CheckpointStore
from athena.errors import AthenaRuntimeError
from athena.types import JSONObject
from athena.workspace import Workspace


class RollbackError(AthenaRuntimeError):
    code = "rollback_error"


class RollbackScope(StrEnum):
    """How much to undo. The same three levels cancellation uses."""

    TASK = "task"
    SUBGRAPH = "subgraph"
    RUN = "run"


#: Roles and operations worth checkpointing before. Reading changes nothing, so a
#: checkpoint before an explorer would cost a copy of the workspace to protect against an
#: agent that cannot write.
def is_worth_checkpointing(files: Sequence[str], *, writes: bool) -> bool:
    """Whether the change ahead justifies a copy of what it will touch.

    Bounded by what is actually at stake: a task with no write capability cannot damage
    anything, and a task that names no files has nothing to copy. Checkpointing everything
    unconditionally would make every run pay for the worst case.
    """
    return writes and bool(files)


@dataclass(frozen=True, slots=True)
class RollbackPoint:
    """A checkpoint, and what it belongs to."""

    checkpoint: Checkpoint
    task_id: str
    scope: RollbackScope = RollbackScope.TASK
    #: Tasks this point covers, for a subgraph or a run. Empty means just `task_id`.
    covers: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def applies_to(self, task_id: str) -> bool:
        return task_id == self.task_id or task_id in self.covers

    def to_json(self) -> JSONObject:
        return {
            "checkpoint_id": self.checkpoint.checkpoint_id,
            "task_id": self.task_id,
            "scope": self.scope.value,
            "covers": list(self.covers),
            "files": [entry.relative_path for entry in self.checkpoint.entries],
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class RollbackResult:
    """What was actually put back, and what was deliberately not."""

    restored: tuple[str, ...] = ()
    #: Files the rollback declined to touch because this run never wrote them.
    protected: tuple[str, ...] = ()
    scope: RollbackScope = RollbackScope.TASK

    @property
    def changed_anything(self) -> bool:
        return bool(self.restored)

    def to_json(self) -> JSONObject:
        return {
            "restored": list(self.restored),
            "protected": list(self.protected),
            "scope": self.scope.value,
        }


class RollbackLedger:
    """Remembers what Athena wrote, so a rollback can be honest about its limits.

    Attribution is recorded as it happens rather than inferred afterwards from a diff. A
    diff cannot tell the agent's edit from the person's, and guessing wrong in the
    permissive direction is how a rollback eats somebody's afternoon.
    """

    def __init__(self, store: CheckpointStore) -> None:
        self.store = store
        self._points: list[RollbackPoint] = []
        self._written: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()

    # -- recording ---------------------------------------------------------

    async def checkpoint(
        self,
        task_id: str,
        workspace: Workspace,
        files: Sequence[str],
        *,
        scope: RollbackScope = RollbackScope.TASK,
        covers: Iterable[str] = (),
        label: str = "",
    ) -> RollbackPoint | None:
        """Snapshot the files a task is about to touch, if there are any.

        `None` rather than an empty checkpoint when there is nothing to copy: an empty
        rollback point would later look like a rollback that found nothing to undo, which
        is a different and more worrying thing.
        """
        if not files:
            return None
        async with self._lock:
            checkpoint = await asyncio.to_thread(
                self.store.create, workspace, files, label=label or f"before {task_id}"
            )
            point = RollbackPoint(
                checkpoint=checkpoint,
                task_id=task_id,
                scope=scope,
                covers=tuple(covers),
            )
            self._points.append(point)
            return point

    def record_written(self, task_id: str, files: Iterable[str]) -> None:
        """Note that this run wrote these files. The basis of every later refusal."""
        self._written.setdefault(task_id, set()).update(files)

    def wrote(self, path: str) -> bool:
        return any(path in files for files in self._written.values())

    def points(self) -> tuple[RollbackPoint, ...]:
        return tuple(self._points)

    # -- undoing -----------------------------------------------------------

    async def roll_back(
        self,
        workspace: Workspace,
        *,
        task_id: str | None = None,
        scope: RollbackScope = RollbackScope.TASK,
    ) -> RollbackResult:
        """Undo, newest first, and only files this run wrote.

        Newest first because checkpoints overlap: two tasks that touched one file each
        left a copy of it, and restoring the older one last would put back a state that
        predates work the rollback was not asked to undo.
        """
        async with self._lock:
            relevant = self._relevant(task_id, scope)
            if not relevant:
                return RollbackResult(scope=scope)
            restored: list[str] = []
            protected: list[str] = []
            seen: set[str] = set()
            for point in reversed(relevant):
                for entry in point.checkpoint.entries:
                    path = entry.relative_path
                    if path in seen:
                        continue
                    seen.add(path)
                    if not self.wrote(path):
                        # Somebody else's file. Reverting it would discard uncommitted
                        # work with no way for its owner to find out.
                        protected.append(path)
                        continue
                    restored.append(path)
                await asyncio.to_thread(self._restore_selected, point, workspace, set(restored))
            for point in relevant:
                self._points.remove(point)
            return RollbackResult(
                restored=tuple(sorted(set(restored))),
                protected=tuple(sorted(set(protected))),
                scope=scope,
            )

    def _relevant(self, task_id: str | None, scope: RollbackScope) -> list[RollbackPoint]:
        if scope is RollbackScope.RUN:
            return list(self._points)
        if task_id is None:
            raise RollbackError("A task or subgraph rollback needs a task id")
        if scope is RollbackScope.SUBGRAPH:
            return [point for point in self._points if point.applies_to(task_id)]
        return [point for point in self._points if point.task_id == task_id]

    def _restore_selected(
        self, point: RollbackPoint, workspace: Workspace, allowed: set[str]
    ) -> None:
        """Restore only the entries this run is entitled to put back.

        The store restores a whole checkpoint, so the filtering happens by handing it a
        checkpoint containing only the permitted entries. Building a narrower checkpoint
        is safer than teaching the store about attribution it has no way to verify.
        """
        entries = tuple(
            entry for entry in point.checkpoint.entries if entry.relative_path in allowed
        )
        if not entries:
            return
        narrowed = Checkpoint(
            checkpoint_id=point.checkpoint.checkpoint_id,
            label=point.checkpoint.label,
            workspace_id=point.checkpoint.workspace_id,
            entries=entries,
            created_at=point.checkpoint.created_at,
        )
        self.store.restore(narrowed, workspace)

    async def discard(self, task_id: str) -> None:
        """Forget a task's checkpoints, once its work is accepted."""
        async with self._lock:
            keep: list[RollbackPoint] = []
            for point in self._points:
                if point.task_id == task_id:
                    await asyncio.to_thread(self.store.discard, point.checkpoint.checkpoint_id)
                else:
                    keep.append(point)
            self._points = keep

    async def discard_all(self) -> None:
        async with self._lock:
            for point in self._points:
                await asyncio.to_thread(self.store.discard, point.checkpoint.checkpoint_id)
            self._points.clear()
            self._written.clear()


__all__ = [
    "RollbackError",
    "RollbackLedger",
    "RollbackPoint",
    "RollbackResult",
    "RollbackScope",
    "is_worth_checkpointing",
]
