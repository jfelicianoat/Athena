"""Que recibe un run al nacer: herramientas, permisos, hooks y verificacion.

Es donde se traducen las capacidades pedidas a objetos concretos. Un run que
no pidio ejecutar no recibe `bash`, y eso se decide aqui y en ningun otro
sitio.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from athena.adapters.service.approvals import (
    DEFAULT_APPROVAL_TIMEOUT_SECONDS,
    DEFAULT_DELIVERY_TIMEOUT_SECONDS,
    ApprovalRegistry,
    PendingApproval,
    RemotePermissionPrompt,
)
from athena.adapters.service.orchestration import (
    OrchestrationSettings,
    Orchestrator,
    budgeted,
)
from athena.adapters.service.runs.opciones import (
    _EXECUTING,
    _MUTATING,
    _SUBSCRIBER_QUEUE_LIMIT,
    CapabilityMode,
    RunOptions,
)
from athena.adapters.service.runs.suscripcion import LiveRun, Subscriber
from athena.agent_loop import AgentLoop, AgentLoopConfig, AgentRunResult, AgentRunStatus
from athena.context import ContextBuilder
from athena.delegation import DelegateTaskTool
from athena.errors import ToolValidationError
from athena.events import EventBus, EventName, RuntimeEvent
from athena.git_tools import GitCommitTool, git_read_tools
from athena.goals import Goal, GoalBoard
from athena.graph_executor import GraphResult
from athena.hooks import HookRegistry
from athena.metrics import MetricsCollector, SqliteMetricsStore
from athena.model_catalog import ModelCatalog
from athena.model_pinning import pinned
from athena.models import ModelProvider
from athena.mutation_tools import workspace_mutation_tools
from athena.permissions import PermissionPolicy, PolicyPermissionEngine
from athena.process_tools import BashTool
from athena.profiles import Evidence, ProfileRegistry
from athena.registry import ToolRegistry
from athena.repository_tools import repository_read_tools
from athena.rollback import checkpointing_hooks
from athena.run_event_log import RunEventLog
from athena.session_store import SessionStore
from athena.state import ExecutionOutcome, SessionState
from athena.stores import ToolResultStore
from athena.subagent_provider import (
    NativeAthenaSubagentProvider,
    SubagentProviderRegistry,
    SubagentService,
)
from athena.subagents import DEFAULT_PROFILES, SubagentRunner
from athena.tool_executor import ToolExecutor
from athena.tools import Tool
from athena.types import JSONObject
from athena.verification import (
    AnswerVerificationPolicy,
    ArtifactVerificationPolicy,
    CommandVerificationPolicy,
    PermissionCheckAuthorizer,
    VerificationPlanner,
    VerificationPolicy,
)
from athena.workspace import Workspace


class ConstruccionMixin:
    """Estado del registro y armado de cada run."""

    if TYPE_CHECKING:
        # Lo que aporta `CicloMixin`, declarado para el comprobador de tipos (A24).
        def _durable(self, event: RuntimeEvent) -> None: ...
        def _require(self, run_id: str) -> LiveRun: ...
        def _publish_approval(self, pending: PendingApproval) -> None: ...

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
        models: ModelCatalog | None = None,
    ) -> None:
        #: Los perfiles que este despliegue ofrece. Uno solo por defecto seria decir que
        #: Athena sirve para una cosa, que es justo lo que la fase venia a desmentir.
        self.profiles = profiles or ProfileRegistry()
        #: Los modelos entre los que un run puede elegir. `None` = este despliegue no
        #: ofrece eleccion y corre siempre con lo que tenga configurado el proveedor, que
        #: es la conducta anterior y sigue siendo valida.
        self.models = models
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
        #: A qué run pertenece cada sesión que no es la del propio run.
        #:
        #: Existe porque el fan-out entregaba sólo lo publicado con el id del run, y en un
        #: run jerárquico las tareas publican con el id de la tarea y los delegados con el
        #: suyo. El resultado era que `subagent.started`, `subagent.completed` y todo lo
        #: que hace un delegado se publicaba correctamente y **no llegaba a nadie**: ni al
        #: cliente que estaba mirando el run, ni a ninguna otra parte. El log duradero ya
        #: aprendía este linaje para poder contar la historia después; esto es lo mismo,
        #: en vivo, para poder contarla mientras pasa.
        self._lineage: dict[str, str] = {}
        event_bus.subscribe(self._fan_out)

    # -- fan-out ----------------------------------------------------------

    def _fan_out(self, event: RuntimeEvent) -> None:
        run_id = self._lineage.get(event.session_id, event.session_id)
        run = self._runs.get(run_id)
        if run is None:
            return
        self._learn_lineage(event, run_id)
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

    def _learn_lineage(self, event: RuntimeEvent, run_id: str) -> None:
        """Aprender de qué run es una sesión, por los dos eventos que lo dicen.

        Se aprende de lo que alguien **anunció** y no de una cercanía temporal: una tarea
        consta porque el ejecutor la empezó, y un delegado porque su padre lo arrancó.
        Adoptar sesiones por proximidad haría que dos runs simultáneos se robaran eventos.
        """
        if event.name is EventName.TASK_STARTED:
            task_id = event.payload.get("task_id")
            if isinstance(task_id, str) and task_id:
                # El ejecutor de grafos usa el id de la tarea como sesión de quien la
                # ejecuta, así que esto es lo que hace que lo suyo llegue al run.
                self._lineage[task_id] = run_id
        elif event.name is EventName.SUBAGENT_STARTED:
            child = event.payload.get("session_id")
            if isinstance(child, str) and child:
                self._lineage[child] = run_id

    def _forget_lineage(self, run_id: str) -> None:
        """Soltar las sesiones de un run que ya no está vivo.

        Sin esto el mapa crece con cada tarea y cada delegado de cada run que haya pasado
        por el proceso, y un servicio largo acabaría recordando linajes de trabajo que
        nadie puede ya consultar.
        """
        self._lineage = {sesion: raiz for sesion, raiz in self._lineage.items() if raiz != run_id}

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
        if run.hierarchical:
            # El grafo no lee el tablero entre tareas: aceptar la revision y seguir con el
            # encargo anterior es lo que encontro la auditoria (A06). Se dice que no.
            raise ToolValidationError(
                f"El run {run_id} se ejecuta como plan de tareas y no puede cambiar de "
                "objetivo a mitad: cancelalo y lanza uno nuevo con el objetivo revisado"
            )
        return run.goal.revise(text, base_revision=base_revision, reason=reason)

    def goal_of(self, run_id: str) -> GoalBoard:
        run = self._require(run_id)
        if run.goal is None:  # pragma: no cover - todo run vivo se crea con tablero
            raise ToolValidationError(f"El run {run_id} no tiene objetivo registrado")
        return run.goal

    def _hooks_for(self, run_id: str, workspace: Workspace) -> HookRegistry:
        """Lo que se engancha a las acciones de un run. Hoy: copiar antes de editar.

        En `PRE_EDIT` y no al empezar una tarea: un plan real casi nunca nombra los
        ficheros que va a tocar, asi que copiar «lo que la tarea declaro» dejaba sin copia
        justo los runs que mas la necesitaban. Se vio en un run real contra el broker, que
        arreglo un bug y no dejo un solo punto al que volver.
        """
        libro = self.orchestrator.ledger_for(run_id)
        if libro is None:
            return HookRegistry()
        return HookRegistry(checkpointing_hooks(libro, workspace))

    def verification_for(
        self, run_id: str, options: RunOptions, workspace: Workspace
    ) -> VerificationPolicy:
        """Como se prueba que el trabajo esta hecho, segun para que se use Athena.

        Un solo sitio y no tres. Los caminos directo, jerarquico y reanudado montaban cada
        uno el suyo, asi que un perfil nuevo habria entrado en uno y no en los otros — y el
        mismo run se habria verificado distinto segun por donde entrase.

        Ejecutar las comprobaciones del proyecto es ejecutar su codigo, asi que pasa por
        la misma autoridad que `bash`: con `execution=off` no se ejecuta nada, con `ask`
        se pregunta por el mismo canal que el resto del run (A01).
        """
        perfil = self.profiles.get(options.profile)
        if perfil.evidence is Evidence.PRODUCED_ARTIFACTS:
            return ArtifactVerificationPolicy(options.deliverables)
        if perfil.evidence is Evidence.ANSWER_ONLY:
            return AnswerVerificationPolicy()
        authorizer = PermissionCheckAuthorizer(
            PolicyPermissionEngine(self.policy_for(options)),
            enabled=options.execution is not CapabilityMode.OFF,
            prompt=self._ask(run_id),
            event_bus=self.event_bus,
        )
        return CommandVerificationPolicy(
            VerificationPlanner(workspace), authorizer=authorizer, event_bus=self.event_bus
        )

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
            self.provider_for(options), catalog, self.event_bus, self.result_store, prompt=None
        )
        service = SubagentService(SubagentProviderRegistry((NativeAthenaSubagentProvider(runner),)))
        # El reloj del despliegue, igual que en el camino jerárquico. Sin esto un
        # delegado corría con los presupuestos de fábrica —cinco minutos para el
        # explorer— mientras el mismo despliegue permitía nueve para una sola llamada al
        # modelo: medido contra este broker, toda delegación moría por construcción antes
        # de terminar su primer turno, y el run se quedaba con un `subagent.failed` que
        # no decía nada del trabajo pedido.
        reloj = self.orchestrator.settings.task_timeout_seconds
        profiles = {role: budgeted(profile, reloj) for role, profile in DEFAULT_PROFILES.items()}
        return DelegateTaskTool(service, catalog, self.policy_for(options), profiles=profiles)

    def provider_for(self, options: RunOptions) -> ModelProvider:
        """El proveedor de este run, con su modelo fijado en toda llamada.

        Lo usan el bucle, el planificador, los delegados y los hijos de un grafo. Antes
        solo el bucle ponia el modelo en su peticion y el resto salia con `None` (A06).
        """
        return pinned(self.provider, self.model_for(options))

    def model_for(self, options: RunOptions) -> str:
        """El modelo con el que corre este run, o un error si pidio uno que no se ofrece.

        Sin catalogo no hay eleccion que validar: el run sale con cadena vacia y el
        proveedor aplica lo que tenga configurado. Pedir un modelo a un despliegue que no
        ofrece ninguno si es un error — es pedir algo que nadie puede conceder.
        """
        if self.models is None:
            if options.model:
                raise ToolValidationError(
                    "Este despliegue no ofrece eleccion de modelo: configura "
                    "ATHENA_ALLOWED_MODELS para poder pedir uno"
                )
            return ""
        return self.models.resolve(options.model)

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
            hooks=self._hooks_for(run_id, workspace),
        )
        return AgentLoop(
            self.provider_for(options),
            registry,
            executor,
            ContextBuilder(
                workspace, notes=notes, subject=self.profiles.get(options.profile).subject
            ),
            self.event_bus,
            verification=self.verification_for(run_id, options, workspace),
            session_store=self.session_store,
            config=AgentLoopConfig(
                max_iterations=options.max_iterations,
                session_timeout_seconds=options.session_timeout_seconds,
                max_repair_cycles=options.max_repair_cycles,
                model=self.model_for(options),
                # Quien encarga un cambio lo dice, y un run asi no termina con solo texto
                # (A04). Antes solo el escritorio lo decidia, con una lista de palabras.
                require_workspace_change=options.require_change,
            ),
        )


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
