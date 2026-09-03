"""Ciclo de vida de un run: arranque, trabajo, reanudacion y cierre.

Un run que arranca queda registrado antes de empezar a trabajar: si el
proceso se cae en medio, lo que quedo a medias se puede encontrar.
"""

from __future__ import annotations

import asyncio
import contextlib
from uuid import uuid4

from athena.adapters.service.approvals import (
    PendingApproval,
)
from athena.adapters.service.orchestration import (
    RunShape,
)
from athena.adapters.service.runs.construccion import ConstruccionMixin, _from_graph
from athena.adapters.service.runs.opciones import (
    _START_TIMEOUT_SECONDS,
    RunOptions,
)
from athena.adapters.service.runs.suscripcion import LiveRun
from athena.agent_loop import AgentRunResult
from athena.cancellation import CancellationSource
from athena.errors import AthenaRuntimeError, ToolValidationError
from athena.events import EventName, RuntimeEvent
from athena.goals import GoalBoard
from athena.graph_store import StoredPlan
from athena.security import redact_sensitive
from athena.session_store import SessionRecord
from athena.state import AgentStatus
from athena.workspace import Workspace


class CicloMixin(ConstruccionMixin):
    """Arranque, ejecucion, reanudacion y apagado de los runs."""

    def _durable(self, event: RuntimeEvent) -> None:
        """Guardar un hecho sin bloquear a quien lo publicó.

        El bus es síncrono y el log escribe en disco, así que se agenda en vez de
        esperarse: un observador que hiciera esperar al runtime convertiría la
        persistencia de un dato en latencia de cada acción.
        """
        if self.event_log is None:
            return
        with contextlib.suppress(RuntimeError):
            # Con referencia: una tarea suelta puede recolectarse a media escritura, y el
            # hecho se perdería justo cuando el log existe para no perderlo.
            task = asyncio.ensure_future(self._store_event(event))
            self._writes.add(task)
            task.add_done_callback(self._writes.discard)

    async def _store_event(self, event: RuntimeEvent) -> None:
        if self.event_log is None:
            return
        with contextlib.suppress(AthenaRuntimeError, OSError):
            await self.event_log.record(event)

    def _publish_approval(self, pending: PendingApproval) -> None:
        """Approvals reach the client the same way everything else does: as an event.

        Redaction is applied here explicitly. This event goes straight to the
        subscribers rather than through `EventBus.publish`, so it would otherwise
        be the one payload in the system that never passed a redactor — and it is
        the payload most likely to carry a tool's arguments.
        """
        run = self._runs.get(pending.run_id)
        if run is None:
            return
        payload = redact_sensitive({**pending.to_json(), "awaiting_decision": True})
        event = RuntimeEvent(
            EventName.PERMISSION_REQUESTED,
            pending.run_id,
            payload if isinstance(payload, dict) else {},
            pending.request_id,
        )
        self._fan_out(event)

    async def _execute(
        self,
        run_id: str,
        objective: str,
        workspace: Workspace,
        options: RunOptions,
        shape: RunShape,
        source: CancellationSource,
    ) -> AgentRunResult:
        """Ejecuta el run con la forma decidida, y con el bucle si el plan no sale.

        Que un plan no llegue a existir no es motivo para fallar: significa que este
        objetivo se hace directamente, que es como se hacía antes de que hubiera planes.
        """
        try:
            return await self._work(run_id, objective, workspace, options, shape, source)
        finally:
            await self._measure(run_id)

    async def _measure(self, run_id: str) -> None:
        """Guardar lo contado, cuando el run ya no va a cambiar.

        Al final y no por evento: lo que se compara es el run entero, y una fila por
        suceso sería otra cosa. Un fallo al guardar se traga a propósito, por el mismo
        motivo por el que el colector no puede lanzar.
        """
        if self.metrics is None or self.metrics_store is None:
            return
        counted = self.metrics.get(run_id)
        if counted is None:
            return
        try:
            await self.metrics_store.save(counted)
        except AthenaRuntimeError:
            return

    async def _work(
        self,
        run_id: str,
        objective: str,
        workspace: Workspace,
        options: RunOptions,
        shape: RunShape,
        source: CancellationSource,
    ) -> AgentRunResult:
        prompt = self._ask(run_id)
        # Lo recordado se pide una vez y se reparte: dos consultas a la memoria por el
        # mismo objetivo darían la misma respuesta y costarían el doble.
        notes = await self.orchestrator.recall(workspace.workspace_id, objective)
        if not shape.hierarchical:
            # La forma ya está decidida sin haber planificado, así que se anuncia aquí. Un
            # run que se planifica la anuncia más tarde, cuando el plan permite juzgarla.
            await self.orchestrator.announce(run_id, shape)
        if shape.hierarchical:
            catalog = {tool.spec.name: tool for tool in self.tools_for(options, self.event_bus)}
            result = await self.orchestrator.run_graph(
                run_id,
                objective,
                workspace,
                shape,
                catalog,
                self.policy_for(options),
                verification=self.verification_for(options, workspace),
                prompt=prompt,
                cancellation=source.token,
            )
            if result is not None:
                return _from_graph(run_id, workspace, result)
        loop = self._build(run_id, workspace, options, notes)
        resultado = await loop.run(
            objective,
            workspace,
            source.token,
            session_id=run_id,
            # El mismo tablero que ve el servicio: dos copias serian dos objetivos, y el
            # cliente estaria revisando uno que nadie lee.
            goal=self._runs[run_id].goal,
        )
        # Lo que se aprendio, del lado de la evidencia. Athena leia su memoria de proyecto
        # en cada run y no escribia en ella nunca: la recordaba vacia y volvia a descubrir
        # los mismos comandos cada vez.
        await self.orchestrator.learn_from(workspace.workspace_id, resultado.verification, run_id)
        return resultado

    async def start(
        self, objective: str, workspace: Workspace, options: RunOptions | None = None
    ) -> str:
        """Begin a run and return only once it is genuinely addressable.

        Returning the id before the run has persisted anything would hand a client an
        identifier that answers 404 for its first few milliseconds. The signal to wait for
        is `session.persisted`, not `agent.started`: both shapes announce themselves
        *before* they write, so the earlier event would still race the store.
        """
        settings = options or RunOptions()
        # El perfil se resuelve antes de que exista el run, por el mismo motivo que la
        # forma: un nombre que no existe tiene que rebotar como peticion invalida, no
        # dejar un run creado que fallara mas tarde por una razon que ya se sabia.
        self.profiles.get(settings.profile)
        # Y el modelo por el mismo motivo. Antes de `decide`, que gasta una lectura del
        # repositorio y puede gastar una llamada al modelo: rechazar despues de eso seria
        # cobrar el trabajo de preparar un run que ya se sabia invalido.
        self.model_for(settings)
        # La forma se decide antes de que exista el run. Al revés, una petición rechazada
        # —pedir grafo donde no hay planificación— dejaba un run vivo que nadie iba a
        # ejecutar ni a cerrar, contado en `/v1/health` y ocupando memoria hasta el
        # reinicio.
        shape = self.orchestrator.decide(workspace, objective, mode=settings.execution_mode)
        run_id = str(uuid4())
        source = CancellationSource()
        self._runs[run_id] = LiveRun(run_id, workspace, settings, source, goal=GoalBoard(objective))

        started = asyncio.Event()

        def note(event: RuntimeEvent) -> None:
            if event.session_id == run_id:
                started.set()

        unsubscribe = self.event_bus.subscribe(note, (EventName.SESSION_PERSISTED,))
        self._runs[run_id].task = asyncio.ensure_future(
            self._execute(run_id, objective, workspace, settings, shape, source)
        )
        try:
            await asyncio.wait_for(started.wait(), timeout=_START_TIMEOUT_SECONDS)
        except TimeoutError:
            # The loop never got going; do not hand back an id nothing will answer for.
            source.cancel()
            self._runs.pop(run_id, None)
            self._forget_lineage(run_id)
            raise AthenaRuntimeError("The run did not start in time") from None
        finally:
            unsubscribe()
        return run_id

    async def resume(self, run_id: str, workspace: Workspace) -> str:
        """Pick a stopped run back up, in the shape it actually had.

        A run that was a plan is resumed as a plan. Resuming it on the loop would be a
        different run wearing the same id: it would redo work whose evidence is already
        stored, and it would do it without the specialists the plan chose.
        """
        record = await self.session_store.load(run_id)
        if record is None:
            raise ToolValidationError(f"Unknown run: {run_id}")
        if not record.resumable:
            raise ToolValidationError(
                f"Run {run_id} is {record.status.value}, not recovery_pending"
            )
        options = self._runs[run_id].options if run_id in self._runs else RunOptions()
        source = CancellationSource()
        stored = await self.orchestrator.stored_plan(run_id)
        if stored is not None:
            undecided = stored.graph.needs_recovery()
            if undecided:
                # Deliberately a refusal. A task that was running when the process died
                # may have written files or not, and the runtime cannot tell which; both
                # re-running it and skipping it are decisions with consequences, and
                # neither is the runtime's to take on somebody's behalf.
                names = ", ".join(node.id for node in undecided)
                raise ToolValidationError(
                    f"Run {run_id} was a plan and these tasks were interrupted with an "
                    f"unknown outcome: {names}. Somebody has to say what happened to them "
                    "before the plan can go on."
                )
            self._runs[run_id] = LiveRun(run_id, workspace, options, source)
            self._runs[run_id].task = asyncio.ensure_future(
                self._continue(run_id, stored, workspace, options, source)
            )
            return run_id

        self._runs[run_id] = LiveRun(run_id, workspace, options, source)
        loop = self._build(run_id, workspace, options)
        self._runs[run_id].task = asyncio.ensure_future(
            loop.resume(run_id, workspace, source.token)
        )
        return run_id

    async def _continue(
        self,
        run_id: str,
        stored: StoredPlan,
        workspace: Workspace,
        options: RunOptions,
        source: CancellationSource,
    ) -> AgentRunResult:
        result = await self.orchestrator.continue_graph(
            run_id,
            stored,
            workspace,
            {tool.spec.name: tool for tool in self.tools_for(options, self.event_bus)},
            self.policy_for(options),
            verification=self.verification_for(options, workspace),
            prompt=self._ask(run_id),
            cancellation=source.token,
        )
        return _from_graph(run_id, workspace, result)

    async def cancel(self, run_id: str) -> None:
        run = self._require(run_id)
        self.approvals.cancel_run(run_id)
        run.cancellation.cancel()

    async def wait(self, run_id: str) -> AgentRunResult:
        run = self._require(run_id)
        if run.task is None:
            raise ToolValidationError(f"Run {run_id} has not started")
        return await run.task

    async def snapshot(self, run_id: str) -> SessionRecord | None:
        return await self.session_store.load(run_id)

    async def list(self, status: AgentStatus | None = None) -> tuple[SessionRecord, ...]:
        return await self.session_store.list_sessions(status)

    async def mark_interrupted(self) -> tuple[str, ...]:
        return await self.session_store.mark_interrupted()

    async def shutdown(self) -> None:
        for run_id in tuple(self._runs):
            with contextlib.suppress(AthenaRuntimeError):
                await self.cancel(run_id)
        for run in tuple(self._runs.values()):
            if run.task is not None and not run.task.done():
                run.task.cancel()
                with contextlib.suppress(BaseException):
                    await run.task
        self._lineage.clear()
        await self.drain()

    async def drain(self) -> None:
        """Esperar a que lo que se estaba guardando termine de guardarse.

        Las escrituras del log van agendadas para no hacer esperar al bus, asi que al
        apagar hay unas cuantas en vuelo. Cerrar sin esperarlas perderia justo los ultimos
        hechos de un run —los que dicen como acabo—, que son los que alguien vendria a
        buscar despues.
        """
        while self._writes:
            await asyncio.gather(*tuple(self._writes), return_exceptions=True)

    def replay(self, run_id: str, last_event_id: str) -> tuple[RuntimeEvent, ...] | None:
        """What a reconnecting client missed, or `None` if it must resynchronise."""
        run = self._runs.get(run_id)
        if run is None:
            return None
        return run.replay_after(last_event_id)

    def live_ids(self) -> tuple[str, ...]:
        return tuple(self._runs)

    def run(self, run_id: str) -> LiveRun:
        """El run vivo con ese id, o un error que lo dice."""
        return self._require(run_id)

    def _require(self, run_id: str) -> LiveRun:
        run = self._runs.get(run_id)
        if run is None:
            raise ToolValidationError(f"Unknown or finished run: {run_id}")
        return run
