"""Live runs, and who is allowed to steer them.

The registry owns three things the HTTP layer must not: which runs are live, who is
subscribed to each, and which single client may send intents. Everything else — the loop,
the permission engine, verification — belongs to Athena proper and is only assembled here.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from athena.adapters.service.approvals import (
    DEFAULT_APPROVAL_TIMEOUT_SECONDS,
    DEFAULT_DELIVERY_TIMEOUT_SECONDS,
    ApprovalRegistry,
    PendingApproval,
    RemotePermissionPrompt,
)
from athena.adapters.service.orchestration import (
    ExecutionMode,
    OrchestrationSettings,
    Orchestrator,
    RunShape,
)
from athena.agent_loop import AgentLoop, AgentLoopConfig, AgentRunResult, AgentRunStatus
from athena.cancellation import CancellationSource
from athena.context import ContextBuilder
from athena.delegation import DelegateTaskTool
from athena.errors import AthenaRuntimeError, ToolValidationError
from athena.events import EventBus, EventName, RuntimeEvent
from athena.git_tools import GitCommitTool, git_read_tools
from athena.goals import Goal, GoalBoard
from athena.graph_executor import GraphResult
from athena.graph_store import StoredPlan
from athena.metrics import MetricsCollector, SqliteMetricsStore
from athena.models import ModelProvider
from athena.mutation_tools import workspace_mutation_tools
from athena.permissions import PermissionPolicy, PolicyPermissionEngine
from athena.process_tools import BashTool
from athena.profiles import Evidence, ProfileRegistry
from athena.registry import ToolRegistry
from athena.repository_tools import repository_read_tools
from athena.run_event_log import RunEventLog
from athena.security import redact_sensitive
from athena.session_store import SessionRecord, SessionStore
from athena.state import AgentStatus, ExecutionOutcome, SessionState
from athena.stores import ToolResultStore
from athena.subagent_provider import (
    NativeAthenaSubagentProvider,
    SubagentProviderRegistry,
    SubagentService,
)
from athena.subagents import SubagentRunner
from athena.tool_executor import ToolExecutor
from athena.tools import Tool
from athena.types import JSONObject
from athena.verification import (
    ArtifactVerificationPolicy,
    CommandVerificationPolicy,
    VerificationPlanner,
    VerificationPolicy,
)
from athena.workspace import Workspace

#: How many events a slow client may fall behind before it is dropped rather than allowed
#: to hold the runtime's memory hostage.
_SUBSCRIBER_QUEUE_LIMIT = 512

#: How many recent events a run keeps so a client that drops can pick up where it left off.
#:
#: Bounded on purpose. An unbounded journal would make the runtime's memory a function of
#: how long a client stays away, which is not a number the runtime gets to choose. When a
#: client has been gone longer than this, the snapshot is still there — it just costs a
#: full resynchronisation instead of a replay.
_REPLAY_BUFFER_SIZE = 256

#: How long `start` waits for the loop to announce itself before giving up.
_START_TIMEOUT_SECONDS = 10.0


class CapabilityMode(StrEnum):
    OFF = "off"
    ASK = "ask"
    ALLOW = "allow"


#: Las que cambian el workspace, y las que ejecutan algo. Por nombre, porque es lo que el
#: perfil declara: deducirlo de `is_read_only()` mezclaria dos preguntas —que hace una
#: tool y quien puede usarla— que el resto del sistema mantiene separadas a proposito.
def _paths(raw: object) -> tuple[str, ...]:
    """Rutas relativas pedidas por el cliente, saneadas aqui y comprobadas mas tarde.

    Aqui solo se exige que sean cadenas: si estan dentro del workspace lo decide el
    propio workspace al resolverlas, que es el unico sitio donde esa pregunta tiene una
    respuesta fiable.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise ToolValidationError("deliverables must be a list of paths")
    return tuple(item.strip() for item in raw if isinstance(item, str) and item.strip())


_MUTATING = frozenset({"write_file", "edit_file", "git_commit"})
_EXECUTING = frozenset({"bash"})


@dataclass(frozen=True, slots=True)
class RunOptions:
    """What a client may choose about a run. Defaults follow ADR-017 §14: it asks."""

    writes: CapabilityMode = CapabilityMode.ASK
    execution: CapabilityMode = CapabilityMode.ASK
    max_iterations: int = 12
    max_repair_cycles: int = 2
    session_timeout_seconds: float = 900.0
    #: Qué se hace con el objetivo. `auto` —lo normal— deja que decidan las señales del
    #: repositorio y no una casilla de la interfaz; `hierarchical` y `direct` fijan el
    #: camino para quien necesita saber cuál corrió.
    execution_mode: ExecutionMode = ExecutionMode.AUTO
    #: Para que se esta usando Athena en este run. Vacio = el de por defecto del
    #: despliegue. Un nombre desconocido es un 400, no una caida al de por defecto:
    #: quien pide `documents` y recibe el de software no se entera hasta que Athena
    #: intenta ejecutar los tests de una carpeta de textos.
    profile: str = ""
    #: Los entregables que se esperan, si quien encarga el trabajo los sabe nombrar. Sin
    #: ellos la evidencia por artefactos comprueba lo que el run dice haber escrito, que
    #: es mas debil; con ellos comprueba lo que se pidio.
    deliverables: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, payload: Mapping[str, object]) -> RunOptions:
        def mode(key: str, default: CapabilityMode) -> CapabilityMode:
            raw = payload.get(key)
            if raw is None:
                return default
            try:
                return CapabilityMode(str(raw))
            except ValueError as exc:
                raise ToolValidationError(f"{key} must be one of off, ask, allow") from exc

        def positive_int(key: str, default: int) -> int:
            raw = payload.get(key, default)
            if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
                raise ToolValidationError(f"{key} must be a positive integer")
            return raw

        raw_mode = payload.get("execution_mode", ExecutionMode.AUTO.value)
        try:
            # `chosen`, no `mode`: ahí arriba `mode` ya es el lector de capacidades, y
            # taparlo dejaba el parseo de writes/exec devolviendo un enum de otra cosa.
            chosen = ExecutionMode(str(raw_mode))
        except ValueError as exc:
            raise ToolValidationError(
                "execution_mode must be one of auto, hierarchical, direct"
            ) from exc

        timeout = payload.get("session_timeout_seconds", 900.0)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ToolValidationError("session_timeout_seconds must be a positive number")

        return cls(
            writes=mode("writes", CapabilityMode.ASK),
            execution=mode("exec", CapabilityMode.ASK),
            max_iterations=positive_int("max_iterations", 12),
            max_repair_cycles=positive_int("max_repair_cycles", 2)
            if payload.get("max_repair_cycles") is not None
            else 2,
            session_timeout_seconds=float(timeout),
            execution_mode=chosen,
            profile=str(payload.get("profile") or ""),
            deliverables=_paths(payload.get("deliverables")),
        )


@dataclass(slots=True)
class Subscriber:
    subscriber_id: str
    run_id: str
    queue: asyncio.Queue[RuntimeEvent | None]
    #: Exactly one subscriber per run may send intents.
    controls: bool = False
    dropped: int = 0


@dataclass(slots=True)
class LiveRun:
    run_id: str
    workspace: Workspace
    options: RunOptions
    cancellation: CancellationSource
    task: asyncio.Task[AgentRunResult] | None = None
    prompt: RemotePermissionPrompt | None = None
    subscribers: dict[str, Subscriber] = field(default_factory=dict)
    controller_id: str | None = None
    #: Cómo se decidió ejecutar este run, tal y como se anunció.
    #:
    #: Guardado además de publicado porque se decide antes de que nadie pueda suscribirse:
    #: un cliente que sólo escuchase el flujo no lo vería nunca, y es justo lo que quiere
    #: saber quien pregunta por qué su objetivo no se planificó.
    shape: JSONObject | None = None
    #: El encargo vigente y su historia. Vive en el run y no en el bucle porque quien lo
    #: revisa habla con el servicio, no con el bucle, y el bucle puede estar dentro de una
    #: llamada al modelo cuando llega el cambio.
    goal: GoalBoard | None = None
    #: The tail of this run's event stream, newest last. Ordering here is the ordering the
    #: bus published in, which is what makes "preserve order per run" a property of the
    #: transport rather than a hope about scheduling.
    recent: deque[RuntimeEvent] = field(default_factory=lambda: deque(maxlen=_REPLAY_BUFFER_SIZE))

    @property
    def finished(self) -> bool:
        return self.task is not None and self.task.done()

    def replay_after(self, event_id: str) -> tuple[RuntimeEvent, ...] | None:
        """Events this run published after `event_id`, or `None` if it is too old.

        `None` and `()` mean different things and the caller must tell them apart: an empty
        tuple is "you are up to date", `None` is "that id fell out of the window, resync".
        Collapsing them would silently let a client believe it had missed nothing.
        """
        buffered = tuple(self.recent)
        for index, event in enumerate(buffered):
            if event.event_id == event_id:
                return buffered[index + 1 :]
        return None


class RunRegistry:
    """Starts, observes, steers and recovers runs."""

    def __init__(
        self,
        provider: ModelProvider,
        event_bus: EventBus,
        session_store: SessionStore,
        result_store: ToolResultStore,
        *,
        approvals: ApprovalRegistry | None = None,
        delivery_timeout_seconds: float | None = None,
        approval_timeout_seconds: float | None = None,
        orchestration: OrchestrationSettings | None = None,
        metrics: MetricsCollector | None = None,
        metrics_store: SqliteMetricsStore | None = None,
        event_log: RunEventLog | None = None,
        profiles: ProfileRegistry | None = None,
    ) -> None:
        #: Los perfiles que este despliegue ofrece. Uno solo por defecto seria decir que
        #: Athena sirve para una cosa, que es justo lo que la fase venia a desmentir.
        self.profiles = profiles or ProfileRegistry()
        self.provider = provider
        self.event_bus = event_bus
        self.session_store = session_store
        self.result_store = result_store
        self.approvals = approvals or ApprovalRegistry()
        self.delivery_timeout_seconds = delivery_timeout_seconds
        self.approval_timeout_seconds = approval_timeout_seconds
        self.orchestrator = Orchestrator(
            provider, event_bus, session_store, result_store, orchestration
        )
        #: Cuenta lo que ocurre en cada run. Se suscribe al bus como cualquier otro
        #: observador y no puede alterar nada: una medición capaz de tumbar el run que
        #: mide convertiría un problema de instrumentación en una caída.
        self.metrics = metrics
        self.metrics_store = metrics_store
        #: Los hechos que sobreviven al proceso. Se suscribe como cualquier observador y
        #: filtra por su cuenta: qué merece durar lo decide el log, no quien publica.
        self.event_log = event_log
        #: Escrituras del log en vuelo, sostenidas para que nadie las recolecte.
        self._writes: set[asyncio.Task[None]] = set()
        if metrics is not None:
            event_bus.subscribe(metrics.observe)
        if event_log is not None:
            event_bus.subscribe(self._durable)
        self._runs: dict[str, LiveRun] = {}
        event_bus.subscribe(self._fan_out)

    # -- fan-out ----------------------------------------------------------

    def _fan_out(self, event: RuntimeEvent) -> None:
        run = self._runs.get(event.session_id)
        if run is None:
            return
        # Recorded before delivery, so an event a slow subscriber never received is still
        # one it can replay. The buffer is the reason dropping a subscriber is survivable
        # rather than lossy.
        run.recent.append(event)
        if event.name is EventName.PLAN_DECIDED:
            # El registro se entera de la forma por donde se entera todo el mundo, en vez
            # de que el orquestador tenga que conocer al registro para contárselo.
            run.shape = dict(event.payload)
        for subscriber in tuple(run.subscribers.values()):
            try:
                subscriber.queue.put_nowait(event)
            except asyncio.QueueFull:
                # A client too slow to keep up loses events, not the runtime its memory.
                # The snapshot it fetches on reconnect is what makes this recoverable.
                subscriber.dropped += 1

    def subscribe(self, run_id: str, *, control: bool = False) -> Subscriber:
        run = self._require(run_id)
        subscriber = Subscriber(
            subscriber_id=str(uuid4()),
            run_id=run_id,
            queue=asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_LIMIT),
        )
        if control and run.controller_id is None:
            run.controller_id = subscriber.subscriber_id
            subscriber.controls = True
        run.subscribers[subscriber.subscriber_id] = subscriber
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        run = self._runs.get(subscriber.run_id)
        if run is None:
            return
        run.subscribers.pop(subscriber.subscriber_id, None)
        if run.controller_id == subscriber.subscriber_id:
            run.controller_id = None
        with contextlib.suppress(asyncio.QueueFull):
            subscriber.queue.put_nowait(None)

    def shape_of(self, run_id: str) -> JSONObject | None:
        """La forma anunciada de un run vivo, si ya se anunció."""
        run = self._runs.get(run_id)
        return None if run is None else run.shape

    def has_client(self, run_id: str) -> bool:
        run = self._runs.get(run_id)
        return bool(run and run.subscribers)

    def controls(self, run_id: str, subscriber_id: str | None) -> bool:
        """One writer per run: two UIs approving the same request is a race worth removing."""
        run = self._runs.get(run_id)
        if run is None:
            return False
        if run.controller_id is None:
            return True
        return run.controller_id == subscriber_id

    # -- lifecycle --------------------------------------------------------

    def tools_for(self, options: RunOptions, event_bus: EventBus) -> tuple[Tool, ...]:
        """Which tools a run gets. `off` means the tool does not exist for that run.

        Dos filtros, y en este orden: el perfil dice que herramientas existen para esta
        clase de trabajo, y las capacidades del run dicen cuales de esas se le conceden.
        El primero es estructural —lo que el perfil no incluye no esta en el catalogo, asi
        que no se puede pedir— y el segundo es politica. Al reves, un run de documentos
        con `exec=allow` tendria shell por el camino de las capacidades.
        """
        perfil = self.profiles.get(options.profile)
        disponibles: dict[str, Tool] = {
            tool.spec.name: tool
            for tool in (
                *repository_read_tools(),
                *git_read_tools(),
                *workspace_mutation_tools(event_bus),
                GitCommitTool(),
                BashTool(event_bus=event_bus),
            )
        }
        del_perfil = perfil.catalog_from(disponibles)
        tools: list[Tool] = []
        for name, tool in del_perfil.items():
            if not isinstance(tool, Tool):  # pragma: no cover - el catalogo es de tools
                continue
            if name in _MUTATING and options.writes is CapabilityMode.OFF:
                continue
            if name in _EXECUTING and options.execution is CapabilityMode.OFF:
                continue
            tools.append(tool)
        return tuple(tools)

    def revise_goal(self, run_id: str, text: str, *, base_revision: int, reason: str = "") -> Goal:
        """Cambiar el encargo de un run que sigue trabajando.

        Sincrono a proposito: escribir la revision es inmediato, y cuando la recoge quien
        trabaja es otra pregunta —la contesta el evento `goal.revised`— que el cliente
        necesita poder distinguir. Prometerle que ya se esta aplicando seria comodo y
        falso: el bucle puede estar a mitad de una llamada al modelo.
        """
        run = self._require(run_id)
        if run.finished:
            raise ToolValidationError(f"El run {run_id} ya termino: su objetivo no cambia")
        if run.goal is None:  # pragma: no cover - todo run vivo se crea con tablero
            raise ToolValidationError(f"El run {run_id} no admite revisiones")
        return run.goal.revise(text, base_revision=base_revision, reason=reason)

    def goal_of(self, run_id: str) -> GoalBoard:
        run = self._require(run_id)
        if run.goal is None:  # pragma: no cover - todo run vivo se crea con tablero
            raise ToolValidationError(f"El run {run_id} no tiene objetivo registrado")
        return run.goal

    def verification_for(self, options: RunOptions, workspace: Workspace) -> VerificationPolicy:
        """Como se prueba que el trabajo esta hecho, segun para que se use Athena.

        Un solo sitio y no tres. Los caminos directo, jerarquico y reanudado montaban cada
        uno el suyo, asi que un perfil nuevo habria entrado en uno y no en los otros — y el
        mismo run se habria verificado distinto segun por donde entrase.
        """
        perfil = self.profiles.get(options.profile)
        if perfil.evidence is Evidence.PRODUCED_ARTIFACTS:
            return ArtifactVerificationPolicy(options.deliverables)
        return CommandVerificationPolicy(VerificationPlanner(workspace), event_bus=self.event_bus)

    def _ask(self, run_id: str) -> RemotePermissionPrompt:
        """El canal por el que este run pregunta, sea cual sea su forma.

        Uno por run y no uno por bucle: un run jerárquico que se crease el suyo aparte
        dejaría a `resolve_permission` contestando a un registro que ya nadie escucha, y
        la aprobación se perdería sin que nada lo dijese.
        """
        existing = self._runs[run_id].prompt
        if existing is not None:
            return existing
        prompt = RemotePermissionPrompt(
            self.approvals,
            run_id,
            self._publish_approval,
            lambda: self.has_client(run_id),
            delivery_timeout_seconds=(
                self.delivery_timeout_seconds
                if self.delivery_timeout_seconds is not None
                else DEFAULT_DELIVERY_TIMEOUT_SECONDS
            ),
            approval_timeout_seconds=(
                self.approval_timeout_seconds
                if self.approval_timeout_seconds is not None
                else DEFAULT_APPROVAL_TIMEOUT_SECONDS
            ),
        )
        self._runs[run_id].prompt = prompt
        return prompt

    @staticmethod
    def policy_for(options: RunOptions) -> PermissionPolicy:
        """La autoridad de un run, dicha una vez.

        La usan el bucle y el grafo. Escrita dos veces, bastaría con tocar una para que un
        run jerárquico tuviese permisos que su equivalente monoagente no tiene, y nada en
        las pruebas de ninguno de los dos lo notaría.
        """
        return PermissionPolicy(
            allow_workspace_writes=options.writes is CapabilityMode.ALLOW,
            allow_local_execution=options.execution is CapabilityMode.ALLOW,
        )

    def _delegation_tool(self, options: RunOptions) -> DelegateTaskTool:
        """La herramienta con la que un run monoagente puede pedir un especialista.

        Se arma con la autoridad del propio run, así que el delegado nunca puede más que
        quien lo pide. Y con el servicio de subagentes, no con un runner concreto: quien
        delega no elige implementación.
        """
        catalog = {tool.spec.name: tool for tool in self.tools_for(options, self.event_bus)}
        # Sin prompt propio: los permisos del delegado los resuelve el motor de su
        # perfil, ya recortado a la autoridad del padre. Un delegado capaz de preguntar
        # por su cuenta abriría una segunda vía de aprobación para el mismo run, y el
        # cliente vería preguntas sin saber de quién son.
        runner = SubagentRunner(
            self.provider, catalog, self.event_bus, self.result_store, prompt=None
        )
        service = SubagentService(SubagentProviderRegistry((NativeAthenaSubagentProvider(runner),)))
        return DelegateTaskTool(service, catalog, self.policy_for(options))

    def _build(
        self, run_id: str, workspace: Workspace, options: RunOptions, notes: str = ""
    ) -> AgentLoop:
        registry = ToolRegistry(
            (*self.tools_for(options, self.event_bus), self._delegation_tool(options))
        )
        prompt = self._ask(run_id)
        executor = ToolExecutor(
            registry,
            PolicyPermissionEngine(self.policy_for(options)),
            self.result_store,
            self.event_bus,
            prompt=prompt,
        )
        return AgentLoop(
            self.provider,
            registry,
            executor,
            ContextBuilder(
                workspace, notes=notes, subject=self.profiles.get(options.profile).subject
            ),
            self.event_bus,
            verification=self.verification_for(options, workspace),
            session_store=self.session_store,
            config=AgentLoopConfig(
                max_iterations=options.max_iterations,
                session_timeout_seconds=options.session_timeout_seconds,
                max_repair_cycles=options.max_repair_cycles,
            ),
        )

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

    def _require(self, run_id: str) -> LiveRun:
        run = self._runs.get(run_id)
        if run is None:
            raise ToolValidationError(f"Unknown or finished run: {run_id}")
        return run


def _from_graph(run_id: str, workspace: Workspace, result: GraphResult) -> AgentRunResult:
    """El resultado de un plan, dicho en el vocabulario del bucle.

    Quien espera un run no debería tener que preguntar de qué forma se ejecutó. La
    respuesta es la de las tareas que dejaron algo escrito, no un veredicto propio: un
    grafo no tiene nada que contar que sus tareas no hayan demostrado ya.
    """
    status = {
        ExecutionOutcome.COMPLETED: AgentRunStatus.COMPLETED,
        ExecutionOutcome.FAILED: AgentRunStatus.FAILED,
        ExecutionOutcome.CANCELLED: AgentRunStatus.CANCELLED,
        ExecutionOutcome.TIMED_OUT: AgentRunStatus.FAILED,
    }[result.outcome]
    answer = "\n".join(item.summary for item in result.evidence if item.summary)
    return AgentRunResult(
        status,
        SessionState(session_id=run_id, workspace_id=workspace.workspace_id),
        answer=answer or None,
        verification=result.goal_verification,
    )


def build_workspace(
    root: Path | str, authorized: Callable[[Path], bool] | None = None
) -> Workspace:
    """Resolve a workspace, honouring an external authorisation check when supplied."""
    workspace = Workspace.from_path(root)
    if authorized is not None and not authorized(workspace.root):
        raise ToolValidationError(f"Workspace is not authorised: {workspace.root}")
    return workspace


__all__ = [
    "CapabilityMode",
    "LiveRun",
    "RunOptions",
    "RunRegistry",
    "Subscriber",
    "build_workspace",
]
