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
import contextlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum

from athena.checkpoints import Checkpoint, CheckpointStore, file_digest
from athena.errors import AthenaRuntimeError
from athena.hooks import Hook, HookContext, HookEvent, HookResult
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
    #: Files this run wrote that somebody changed afterwards. Left exactly as they are:
    #: putting Athena's copy back would erase that later work without a word (A11).
    conflicts: tuple[str, ...] = ()
    #: Files whose copy could not be used (missing or damaged). Also left untouched.
    failed: tuple[str, ...] = ()

    @property
    def changed_anything(self) -> bool:
        return bool(self.restored)

    def to_json(self) -> JSONObject:
        return {
            "restored": list(self.restored),
            "protected": list(self.protected),
            "conflicts": list(self.conflicts),
            "failed": list(self.failed),
            "scope": self.scope.value,
        }


class RollbackLedger:
    """Remembers what Athena wrote, so a rollback can be honest about its limits.

    Attribution is recorded as it happens rather than inferred afterwards from a diff. A
    diff cannot tell the agent's edit from the person's, and guessing wrong in the
    permissive direction is how a rollback eats somebody's afternoon.

    What survives the process: every checkpoint carries its run, task and scope, and
    each copy records the hash before the write and the hash Athena left behind. That is
    enough to rebuild the ledger after a restart with `load` (A13) and to notice that a
    file changed after Athena wrote it (A11).
    """

    def __init__(self, store: CheckpointStore, *, run_id: str = "") -> None:
        self.store = store
        self.run_id = run_id
        self._points: list[RollbackPoint] = []
        self._written: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()
        #: Where the files live, learned from the first checkpoint. Needed to hash what a
        #: write left behind when it is confirmed.
        self._workspace: Workspace | None = None

    @classmethod
    def load(cls, store: CheckpointStore, run_id: str) -> RollbackLedger:
        """The ledger of a run, rebuilt from what its checkpoints left on disk."""
        ledger = cls(store, run_id=run_id)
        if not run_id:
            return ledger
        for checkpoint in store.list():
            if checkpoint.run_id != run_id or checkpoint.rolled_back:
                continue
            try:
                scope = RollbackScope(checkpoint.scope)
            except ValueError:
                scope = RollbackScope.TASK
            point = RollbackPoint(
                checkpoint=checkpoint,
                task_id=checkpoint.task_id or run_id,
                scope=scope,
                covers=checkpoint.covers,
                created_at=checkpoint.created_at,
            )
            ledger._points.append(point)
            for entry in checkpoint.entries:
                if entry.written:
                    ledger._written.setdefault(point.task_id, set()).add(entry.relative_path)
        return ledger

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
        self._workspace = workspace
        async with self._lock:
            checkpoint = await asyncio.to_thread(
                self.store.create,
                workspace,
                files,
                label=label or f"before {task_id}",
                run_id=self.run_id,
                task_id=task_id,
                scope=scope.value,
                covers=tuple(covers),
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
        """Note that this run wrote these files, once the write has happened.

        The basis of every later refusal. It confirms the newest copy of each file that
        was still waiting and stores the hash the write left behind: a rollback only puts
        a copy back while the file still has exactly that content.
        """
        paths = list(files)
        self._written.setdefault(task_id, set()).update(paths)
        workspace = self._workspace
        if workspace is None:
            return
        for raw in paths:
            try:
                resolved = workspace.resolve(raw, must_exist=False)
            except AthenaRuntimeError:
                continue
            relative = resolved.relative_to(workspace.root).as_posix()
            after = file_digest(resolved)
            for index in range(len(self._points) - 1, -1, -1):
                point = self._points[index]
                entry = next(
                    (item for item in point.checkpoint.entries if item.relative_path == relative),
                    None,
                )
                if entry is None:
                    continue
                if not entry.written:
                    with contextlib.suppress(OSError):
                        updated = self.store.mark_written(point.checkpoint, relative, after)
                        self._points[index] = replace(point, checkpoint=updated)
                break

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
        """Undo, newest first, and only what this run wrote and nobody touched since.

        Newest first, and every copy of a file in turn: two edits of one file left two
        copies, and undoing both means going back through the second to reach the state
        before the first. The old code stopped at the newest copy, so undoing a run that
        edited a file twice left the first edit in place.

        Each step checks that the file still has the content Athena left. If it does not,
        somebody changed it afterwards: that file stops there, is reported as a conflict,
        and none of its older copies is applied either.
        """
        async with self._lock:
            relevant = self._relevant(task_id, scope)
            if not relevant:
                return RollbackResult(scope=scope)
            restored: list[str] = []
            protected: list[str] = []
            conflicts: list[str] = []
            failed: list[str] = []
            blocked: set[str] = set()
            stuck: set[str] = set()
            for point in reversed(relevant):
                if point.checkpoint.workspace_id not in ("", workspace.workspace_id):
                    raise RollbackError(
                        "Refusing to roll back into a different workspace than the one "
                        "the checkpoint was taken in"
                    )
                for entry in point.checkpoint.entries:
                    path = entry.relative_path
                    if path in blocked:
                        continue
                    if not entry.written:
                        # Somebody else's file, or a write that never happened. Reverting
                        # it would discard work with no way for its owner to find out.
                        protected.append(path)
                        continue
                    try:
                        target = workspace.resolve(path, must_exist=False)
                    except AthenaRuntimeError:
                        blocked.add(path)
                        failed.append(path)
                        stuck.add(point.checkpoint.checkpoint_id)
                        continue
                    if await asyncio.to_thread(file_digest, target) != entry.after_sha256:
                        blocked.add(path)
                        conflicts.append(path)
                        stuck.add(point.checkpoint.checkpoint_id)
                        continue
                    try:
                        await asyncio.to_thread(
                            self.store.restore_entry, point.checkpoint, entry, workspace
                        )
                    except (AthenaRuntimeError, OSError):
                        blocked.add(path)
                        failed.append(path)
                        stuck.add(point.checkpoint.checkpoint_id)
                        continue
                    restored.append(path)
            for point in relevant:
                if point.checkpoint.checkpoint_id in stuck:
                    # Queda disponible: si la persona resuelve el conflicto, puede volver a
                    # pedirlo.
                    continue
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(self.store.mark_rolled_back, point.checkpoint)
                self._points.remove(point)
            done = set(restored)
            return RollbackResult(
                restored=tuple(sorted(done)),
                protected=tuple(sorted(set(protected) - done - set(conflicts))),
                scope=scope,
                conflicts=tuple(sorted(set(conflicts))),
                failed=tuple(sorted(set(failed))),
            )

    def _relevant(self, task_id: str | None, scope: RollbackScope) -> list[RollbackPoint]:
        if scope is RollbackScope.RUN:
            return list(self._points)
        if task_id is None:
            raise RollbackError("A task or subgraph rollback needs a task id")
        if scope is RollbackScope.SUBGRAPH:
            return [point for point in self._points if point.applies_to(task_id)]
        return [point for point in self._points if point.task_id == task_id]

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


def _relatives(workspace: Workspace, payload: JSONObject) -> list[str]:
    crudos = payload.get("resources")
    rutas = [item for item in crudos if isinstance(item, str)] if isinstance(crudos, list) else []
    relativas: list[str] = []
    for ruta in rutas:
        try:
            # `must_exist=False` tambien al sacar la relativa: un fichero que se va a crear
            # todavia no existe, y exigirlo aqui dejaba sin copia justo las creaciones, que
            # el rollback luego no podia deshacer (A10).
            resuelta = workspace.resolve(ruta, must_exist=False)
            relativas.append(workspace.relative(resuelta, must_exist=False))
        except AthenaRuntimeError:
            continue
    return relativas


def checkpointing_hook(ledger: RollbackLedger, workspace: Workspace, *, task_id: str = "") -> Hook:
    """Copiar un fichero justo antes de que lo editen.

    Este es el sitio, y no el principio de una tarea. Un plan real casi nunca nombra los
    ficheros que va a tocar, asi que copiar «lo que la tarea declaro» dejaba sin copia
    exactamente los runs que mas la necesitaban — los que el modelo condujo por su cuenta.
    `PRE_EDIT` sabe el fichero concreto y llega antes de la escritura, que son las dos
    cosas que hacen falta.

    Observacional, nunca bloqueante. Una copia que no se pudo hacer es una red de seguridad
    que falta; convertirla en un veto sobre la edicion haria que un disco lleno impidiera
    trabajar, lo cual es peor y ademas sorprendente.

    Solo copia: que la escritura ocurrio lo anota `confirming_hook` en `POST_EDIT`, que
    solo llega si la escritura salio bien. Antes se anotaba aqui, antes de escribir, y una
    escritura fallida quedaba contada como hecha (A10).
    """

    async def copiar(context: HookContext) -> HookResult:
        relativas = _relatives(workspace, context.payload)
        if relativas:
            with contextlib.suppress(AthenaRuntimeError, OSError):
                await ledger.checkpoint(
                    task_id or context.session_id,
                    workspace,
                    relativas,
                    scope=RollbackScope.RUN if not task_id else RollbackScope.TASK,
                    label=f"antes de editar {', '.join(relativas)}",
                )
        return HookResult()

    return Hook(
        name="rollback.checkpoint",
        event=HookEvent.PRE_EDIT,
        handler=copiar,
        blocking=False,
        order=10,
    )


def confirming_hook(ledger: RollbackLedger, workspace: Workspace, *, task_id: str = "") -> Hook:
    """Anotar lo escrito cuando ya esta escrito, con la huella que dejo."""

    async def confirmar(context: HookContext) -> HookResult:
        relativas = _relatives(workspace, context.payload)
        if relativas:
            with contextlib.suppress(AthenaRuntimeError, OSError):
                ledger.record_written(task_id or context.session_id, relativas)
        return HookResult()

    return Hook(
        name="rollback.confirm",
        event=HookEvent.POST_EDIT,
        handler=confirmar,
        blocking=False,
        order=10,
    )


def checkpointing_hooks(
    ledger: RollbackLedger, workspace: Workspace, *, task_id: str = ""
) -> tuple[Hook, Hook]:
    """Los dos ganchos que hacen falta para poder deshacer: copiar antes, anotar despues."""
    return (
        checkpointing_hook(ledger, workspace, task_id=task_id),
        confirming_hook(ledger, workspace, task_id=task_id),
    )


__all__ = [
    "RollbackError",
    "RollbackLedger",
    "RollbackPoint",
    "RollbackResult",
    "RollbackScope",
    "checkpointing_hook",
    "checkpointing_hooks",
    "confirming_hook",
    "is_worth_checkpointing",
]
