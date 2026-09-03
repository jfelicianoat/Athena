"""Endpoints de runs: listado, arranque idempotente, estado y reanudacion."""

from __future__ import annotations

import asyncio

from athena.adapters.service.projections import (
    error_to_json,
    run_summary_to_json,
    session_to_json,
    status_from_json,
)
from athena.adapters.service.runs import RunOptions, build_workspace
from athena.adapters.service.server.auxiliares import (
    _positive_int,
)
from athena.adapters.service.server.http import (
    _IDEMPOTENCY_ENTRIES,
    Request,
    Response,
)
from athena.adapters.service.server.transporte import TransporteMixin
from athena.errors import (
    AthenaRuntimeError,
    ToolValidationError,
)
from athena.permissions import PermissionDecision
from athena.rollback import RollbackScope
from athena.run_event_log import replay
from athena.types import JSONObject
from athena.workspace import Workspace


class EndpointsRunsMixin(TransporteMixin):
    """Todo lo que cuelga de `/v1/runs`."""

    async def _list_runs(self, request: Request) -> Response:
        raw = request.query.get("status")
        status = status_from_json(raw) if raw else None
        if raw and status is None:
            raise ToolValidationError(f"Unknown status: {raw}")
        records = await self.registry.list(status)
        return Response(200, {"runs": [run_summary_to_json(record) for record in records]})

    async def _start_run(self, request: Request) -> Response:
        """Create a run and return its id, without waiting for it to do anything.

        The request is over in milliseconds. Holding it open for the length of an agent
        run would tie the work's lifetime to a socket's, and sockets die for reasons that
        have nothing to do with the work — a sleeping laptop, a proxy's idle timeout, a
        client that was restarted. Progress arrives on the event stream instead.
        """
        payload = request.json()
        objective = payload.get("objective")
        if not isinstance(objective, str) or not objective.strip():
            raise ToolValidationError("objective must be a non-empty string")
        root = payload.get("workspace")
        if not isinstance(root, str) or not root:
            raise ToolValidationError("workspace must be a path")
        key = request.headers.get("idempotency-key", "").strip()
        if key:
            return await self._idempotent_start(key, request, objective, root, payload)
        return await self._create_run(objective, root, payload, status=201)

    async def _idempotent_start(
        self,
        key: str,
        request: Request,
        objective: str,
        root: str,
        payload: JSONObject,
    ) -> Response:
        """Create at most one run per `Idempotency-Key`.

        A retry is not a second request. Starting a run is expensive and not reversible in
        the way a read is — two agents on one workspace is exactly the outcome a client
        retrying a timed-out POST is trying to avoid.

        The in-flight case is handled with a future rather than a "seen" set, because the
        window that matters is precisely the one where the first call has not finished:
        a check-then-act across `await registry.start(...)` would let both callers miss.
        """
        del request
        existing = self._idempotency.get(key)
        if existing is not None:
            try:
                run_id = await asyncio.shield(existing)
            except asyncio.CancelledError:
                if not existing.cancelled():
                    # This request is being cancelled, not the run it was waiting on.
                    raise
                # The call we were waiting on failed and withdrew its key. Waiting for a
                # run that will never exist would be a worse answer than doing the work.
                existing = None
            if existing is not None:
                return Response(
                    200,
                    {
                        "run_id": run_id,
                        "idempotent_replay": True,
                        "workspace_id": Workspace.from_path(root).workspace_id,
                    },
                )

        pending: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._idempotency[key] = pending
        try:
            response = await self._create_run(objective, root, payload, status=201)
            created = (response.payload or {}).get("run_id")
            if not isinstance(created, str):
                raise AthenaRuntimeError("A created run has no id to be idempotent about")
        except BaseException:
            # A failed attempt must not become a cached answer: the caller is entitled to
            # retry the same key and actually get a run. Withdrawing the key *and*
            # cancelling the future is what lets a concurrent waiter fall through above,
            # instead of blocking on a promise nobody is going to keep.
            self._idempotency.pop(key, None)
            if not pending.done():
                pending.cancel()
            raise
        pending.set_result(created)
        self._trim_idempotency()
        return response

    def _trim_idempotency(self) -> None:
        """Keep the map bounded. A retry that arrives an hour later is a new request."""
        while len(self._idempotency) > _IDEMPOTENCY_ENTRIES:
            self._idempotency.pop(next(iter(self._idempotency)))

    async def _create_run(
        self, objective: str, root: str, payload: JSONObject, *, status: int
    ) -> Response:
        workspace = build_workspace(root, self.config.authorized_workspace)
        options = RunOptions.from_json(payload)
        run_id = await self.registry.start(objective, workspace, options)
        return Response(
            status,
            {
                "run_id": run_id,
                "workspace_id": workspace.workspace_id,
                "writes": options.writes.value,
                "exec": options.execution.value,
            },
        )

    async def _get_run(self, run_id: str) -> Response:
        record = await self.registry.snapshot(run_id)
        if record is None:
            return Response(404, error_to_json("not_found", f"Unknown run: {run_id}"))
        return Response(200, session_to_json(record))

    def _rollback_points(self, run_id: str) -> Response:
        """Que se podria deshacer de este run, sin deshacer nada."""
        libro = self.registry.orchestrator.ledger_for(run_id)
        if libro is None:
            return Response(
                404, error_to_json("rollback_disabled", "This deployment takes no checkpoints")
            )
        return Response(
            200,
            {"run_id": run_id, "points": [punto.to_json() for punto in libro.points()]},
        )

    async def _rollback(self, run_id: str, request: Request) -> Response:
        """Deshacer lo que este run escribio, y solo eso.

        Se pide: nada se deshace por su cuenta. Un rollback automatico tiraria trabajo que
        una persona podria querer mirar —lo dice `checkpoints.py` desde H2— y esta ruta es
        lo que convierte esa decision en algo que alguien puede ejercer, en vez de en un
        modulo entero que no importaba nadie.
        """
        libro = self.registry.orchestrator.ledger_for(run_id)
        if libro is None:
            return Response(
                404, error_to_json("rollback_disabled", "This deployment takes no checkpoints")
            )
        payload = request.json()
        crudo = payload.get("scope", RollbackScope.RUN.value)
        try:
            scope = RollbackScope(str(crudo))
        except ValueError as exc:
            raise ToolValidationError("scope must be one of task, subgraph, run") from exc
        task_id = payload.get("task_id")
        if task_id is not None and not isinstance(task_id, str):
            raise ToolValidationError("task_id must be a string")
        run = self.registry.run(run_id)
        resultado = await libro.roll_back(run.workspace, task_id=task_id, scope=scope)
        return Response(200, resultado.to_json())

    async def _history(self, run_id: str, request: Request) -> Response:
        """Lo que ocurrió en un run, leido del registro y no del run vivo.

        Distinto de `/v1/runs/{id}/events`, que es el stream: aquel sirve mientras el run
        pasa y no existe para quien llega tarde. Este contesta despues, incluso tras un
        reinicio, e incluye lo que hicieron los delegados atribuido a quien lo hizo.
        """
        log = self.registry.event_log
        if log is None:
            return Response(
                404, error_to_json("history_disabled", "This deployment keeps no durable log")
            )
        after = _positive_int(request.query.get("after"))
        task_id = request.query.get("task")
        events = (
            await log.read_task(run_id, task_id) if task_id else await log.read(run_id, after=after)
        )
        if not events and after == 0 and not task_id:
            # Un run del que no consta nada no es un run vacio: o no existio, o es
            # anterior al log. Decir 200 con una lista vacia haria pasar por historia
            # completa la ausencia de historia.
            return Response(404, error_to_json("not_found", f"No durable history for {run_id}"))
        return Response(
            200,
            {
                "run_id": run_id,
                "events": [event.to_json() for event in events],
                # El resumen viaja con los hechos porque se deriva de ellos: calcularlo en
                # el cliente obligaria a cada cliente a repetir la misma lectura y a
                # ponerse de acuerdo en como se lee.
                "summary": replay(events),
            },
        )

    async def _resume_run(self, run_id: str, request: Request) -> Response:
        payload = request.json()
        root = payload.get("workspace")
        if not isinstance(root, str) or not root:
            raise ToolValidationError("workspace must be a path")
        workspace = build_workspace(root, self.config.authorized_workspace)
        resumed = await self.registry.resume(run_id, workspace)
        return Response(202, {"run_id": resumed, "resumed": True})

    def _acknowledge(self, run_id: str, request_id: str) -> Response:
        window = (
            self.config.approval_timeout_seconds
            if self.config.approval_timeout_seconds is not None
            else 300.0
        )
        pending = self.approvals.acknowledge(request_id, window)
        if pending is None or pending.run_id != run_id:
            return Response(404, error_to_json("not_found", "No such approval request"))
        return Response(200, pending.to_json())

    def _decide(self, run_id: str, request_id: str, request: Request) -> Response:
        payload = request.json()
        raw = payload.get("decision")
        if raw not in ("allow", "deny"):
            raise ToolValidationError("decision must be 'allow' or 'deny'")
        subscriber_id = request.headers.get("x-athena-subscriber")
        if not self.registry.controls(run_id, subscriber_id):
            return Response(
                403,
                error_to_json(
                    "not_controller",
                    "Another client controls this run; observers may not approve",
                ),
            )
        existing = self.approvals.get(request_id)
        if existing is None or existing.run_id != run_id:
            return Response(404, error_to_json("not_found", "No such approval request"))
        if existing.consumed:
            # Single use: a replayed POST must not approve a second action.
            return Response(409, error_to_json("already_resolved", "This request was answered"))
        decision = PermissionDecision.ALLOW if raw == "allow" else PermissionDecision.DENY
        self.approvals.resolve(request_id, decision)
        return Response(200, {"request_id": request_id, "decision": decision.value})
