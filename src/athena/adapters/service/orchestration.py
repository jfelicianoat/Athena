"""Deciding, per run, whether the service runs a loop or a graph.

Everything V0.2 built — planning, delegation, the graph executor, memory — lived one layer
away from the only door a real user comes through. `RunRegistry` assembled an `AgentLoop`
and nothing else, so a run started from ChatyGPT was the V0.1 monoagent runtime however
much machinery sat beside it. This is the door.

The decision is made the way `DecompositionPolicy` always intended and never got to: with
evidence. `RepositoryScout` measures what the repository can show, the caller may fill in
what it cannot, and the policy answers. No model is asked whether it would like to be
decomposed.

**Most runs stay monoagent, and that is the point.** A graph for "fix the failing test"
adds hand-offs between steps that were never independent. The loop is not the fallback
here; it is the default, and the graph has to be argued for.

Whichever path runs, the run looks the same from outside: same `agent.started`, same
terminal `agent.*` event, same persisted session. A client watching a run must not have to
know which shape executed — and one that stops at `agent.completed` must still stop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum

from athena.cancellation import CancellationScope, CancellationToken, chained_source
from athena.delegation import confine
from athena.errors import AthenaRuntimeError, ToolValidationError
from athena.events import EventBus, EventName, RuntimeEvent
from athena.graph_executor import GraphExecutor, GraphResult
from athena.graph_store import SqliteGraphStore, StoredPlan
from athena.models import ModelProvider
from athena.permissions import PermissionPolicy, PermissionPrompt
from athena.planning import (
    DecompositionDecision,
    DecompositionPolicy,
    DecompositionSignals,
    PlanBoard,
    Planner,
    PlanningLimits,
    PlanStatus,
    TaskGraph,
    TaskNode,
)
from athena.project_memory import MemoryKind, SqliteProjectMemory, render_for_context
from athena.scouting import RepositoryScout, merge
from athena.session_store import SessionRecord, SessionStore
from athena.state import AgentStatus, ExecutionOutcome
from athena.stores import ToolResultStore
from athena.subagents import (
    DEFAULT_PROFILES,
    SubagentProfile,
    SubagentRole,
    SubagentRunner,
)
from athena.tasks import TaskManager
from athena.tools import Tool
from athena.types import JSONObject
from athena.verification import VerificationPolicy
from athena.working_state import PlanStep, StepStatus, WorkingState
from athena.workspace import Workspace

_logger = logging.getLogger(__name__)

#: How much remembered context a run is given. Selective, because a prompt carrying every
#: fact Athena ever recorded would spend the window on things this run has no use for.
_RECALLED = 6

#: A plan status as the working state records it. The two vocabularies are deliberately
#: different — `PlanStatus` describes a node in a graph, `StepStatus` describes a line a
#: person reads — so the mapping is stated once here rather than guessed at each use.
_AS_STEP = {
    PlanStatus.COMPLETED: StepStatus.DONE,
    PlanStatus.RUNNING: StepStatus.IN_PROGRESS,
    PlanStatus.FAILED: StepStatus.BLOCKED,
    PlanStatus.BLOCKED: StepStatus.BLOCKED,
}


@dataclass(frozen=True, slots=True)
class OrchestrationSettings:
    """What the service is willing and able to do beyond a single loop.

    Every field is optional and absent means "as before". A deployment that wants the V0.1
    behaviour gets it by configuring nothing, which is the only way to add a layer to a
    working system without betting the working system on it.
    """

    planning: bool = False
    limits: PlanningLimits = field(default_factory=PlanningLimits)
    policy: DecompositionPolicy = field(default_factory=DecompositionPolicy)
    memory: SqliteProjectMemory | None = None
    graphs: SqliteGraphStore | None = None
    board: PlanBoard | None = None
    #: Cuánto puede durar una tarea del plan, si el despliegue lo sabe mejor que el
    #: perfil. Los presupuestos por defecto —cinco minutos para explorar, diez para
    #: escribir— se escribieron pensando en modelos que contestan en segundos, y una sola
    #: llamada a un modelo local de 30B medida contra este broker tardó nueve minutos.
    #: `None` deja el presupuesto del perfil, que es lo correcto cuando nadie mide nada.
    task_timeout_seconds: float | None = None


class ExecutionMode(StrEnum):
    """What the caller wants the runtime to do with a goal.

    Three named modes rather than a boolean with three states. `hierarchical: null` reads
    as "unset", which a client cannot tell apart from "not supported" or "left at the
    default" — and the difference decides whether a run is planned at all.
    """

    #: Let the evidence decide. The normal way to run Athena.
    AUTO = "auto"
    #: Always execute through a `TaskGraph`, even if the plan holds a single task. Costs
    #: hand-offs a simple goal does not need, and is worth it when the graph itself is
    #: what is being observed: tests, debugging, benchmarks, experiments.
    HIERARCHICAL = "hierarchical"
    #: Always the loop, whatever the repository looks like.
    DIRECT = "direct"


@dataclass(frozen=True, slots=True)
class RunShape:
    """How this run will be executed, and why.

    `mode` is what was asked for and `hierarchical` is what will happen. They agree except
    in `AUTO`, where the second is the answer to a question the first only posed.
    """

    mode: ExecutionMode
    hierarchical: bool
    decision: DecompositionDecision
    signals: DecompositionSignals
    #: What the scout could not establish, carried so a client can show it rather than
    #: being told a guess was a measurement.
    assumed: tuple[str, ...] = ()

    def to_json(self) -> JSONObject:
        return {
            "execution_mode": self.mode.value,
            "hierarchical": self.hierarchical,
            "reasons": list(self.decision.reasons),
            "explanation": self.decision.explanation,
            "assumed_signals": list(self.assumed),
        }


class Orchestrator:
    """Chooses the shape of a run, and runs a graph when one is chosen."""

    def __init__(
        self,
        provider: ModelProvider,
        event_bus: EventBus,
        session_store: SessionStore,
        result_store: ToolResultStore,
        settings: OrchestrationSettings | None = None,
    ) -> None:
        self.provider = provider
        self.event_bus = event_bus
        self.session_store = session_store
        self.result_store = result_store
        self.settings = settings or OrchestrationSettings()
        self.scout = RepositoryScout()

    # -- the decision ------------------------------------------------------

    def decide(
        self,
        workspace: Workspace,
        objective: str,
        supplied: DecompositionSignals | None = None,
        *,
        mode: ExecutionMode = ExecutionMode.AUTO,
    ) -> RunShape:
        """Measure, let the caller fill the gaps, and apply the policy.

        Never asks a model. The party that would most like the answer to be "yes" is not
        the party that should be giving it — and in `AUTO` the answer costs a filesystem
        scan, not a model call, which is what makes `AUTO` affordable as the default.

        `DIRECT` is answered without measuring anything: the reading would change nothing
        and the caller has already decided.
        """
        if mode is ExecutionMode.DIRECT:
            return RunShape(
                mode,
                hierarchical=False,
                decision=DecompositionDecision(
                    False, (), "The caller asked for the loop directly."
                ),
                signals=DecompositionSignals(),
            )
        if mode is ExecutionMode.HIERARCHICAL and not self.settings.planning:
            # Refused rather than quietly downgraded. Somebody who asks for a graph is
            # usually measuring one, and handing them a loop that reports itself as a run
            # would corrupt the measurement instead of failing it.
            raise ToolValidationError(
                "This deployment does not do hierarchical runs; execution_mode must be "
                "auto or direct"
            )
        scouted = self.scout.scout(workspace, objective)
        signals = merge(scouted, supplied) if supplied is not None else scouted.signals
        decision = self.settings.policy.assess(signals)
        hierarchical = mode is ExecutionMode.HIERARCHICAL or (
            self.settings.planning and decision.decompose
        )
        return RunShape(
            mode,
            hierarchical=hierarchical,
            decision=decision,
            signals=signals,
            assumed=scouted.assumed,
        )

    # -- context -----------------------------------------------------------

    async def recall(self, project_id: str, objective: str) -> str:
        """What Athena remembers about this project, labelled with how sure it is.

        Empty when there is nothing to say: an empty preamble in every prompt would spend
        tokens communicating absence.
        """
        if self.settings.memory is None:
            return ""
        try:
            found = await self.settings.memory.search(project_id, objective, limit=_RECALLED)
        except AthenaRuntimeError as error:
            _logger.warning("memory.recall_failed code=%s", error.code)
            return ""
        return render_for_context(found)

    async def remember_command(self, project_id: str, command: str) -> None:
        """Remember a check that actually ran, as a proposal.

        Proposed, never verified: the run believing its own command worked is exactly the
        kind of conclusion that should not become a fact without something checking it.
        """
        if self.settings.memory is None or not command.strip():
            return
        try:
            await self.settings.memory.propose(
                project_id,
                MemoryKind.VERIFIED_COMMAND,
                command.strip(),
                source=f"run:{project_id}",
            )
        except AthenaRuntimeError as error:
            _logger.warning("memory.propose_failed code=%s", error.code)

    # -- the graph path ----------------------------------------------------

    async def run_graph(
        self,
        run_id: str,
        objective: str,
        workspace: Workspace,
        shape: RunShape,
        catalog: dict[str, Tool],
        policy: PermissionPolicy,
        *,
        verification: VerificationPolicy | None = None,
        prompt: PermissionPrompt | None = None,
        cancellation: CancellationToken,
    ) -> GraphResult | None:
        """Plan the work, execute the plan, and leave a session behind either way.

        `None` when no usable plan came back. The caller falls through to the loop rather
        than failing the run: a plan that could not be made is a reason to work directly,
        not a reason to do nothing.

        The session is written *before* planning, not after. Planning is a model call and
        a model call takes as long as it takes; a client handed a run id it cannot fetch
        for the first minute would reasonably conclude the run does not exist.
        """
        empty = WorkingState(objective=objective)
        await self._persist(run_id, workspace, empty, AgentStatus.RUNNING)
        planner = Planner(self.provider, policy=self.settings.policy, limits=self.settings.limits)
        graph: TaskGraph | None
        try:
            graph = await planner.plan(
                objective, shape.signals, cancellation, decided=_settled(shape)
            )
        except AthenaRuntimeError as error:
            _logger.warning("planning.refused code=%s", error.code)
            graph = None
            reason = error.code
        else:
            reason = "not_worth_decomposing"

        if graph is None:
            if shape.mode is not ExecutionMode.HIERARCHICAL:
                # A refused plan is information, not a failure. The loop can still do this,
                # and saying so is more useful than failing a run over a malformed reply.
                await self._publish(
                    run_id,
                    EventName.RECOVERY_ACTION,
                    {"action": "run_directly", "reason": reason},
                )
                return None
            # The caller asked for a graph, so a graph is what runs. One task holding the
            # whole goal is a truthful plan — it says the work was not divided — and it
            # keeps the guarantee the mode exists for: this path is always the graph path.
            await self._publish(
                run_id,
                EventName.RECOVERY_ACTION,
                {"action": "whole_goal_as_one_task", "reason": reason},
            )
            graph = _whole_goal(objective)

        if self.settings.board is not None:
            self.settings.board.record(run_id, graph)
        # Anunciado aquí y no antes: hasta que hay plan, este run todavía podía acabar
        # siendo del bucle, y el bucle publica su propio arranque. Dos `agent.started`
        # para un run se leen como dos intentos.
        await self._publish(
            run_id, EventName.AGENT_STARTED, {"objective": objective, "resumed": False}
        )
        await self._persist(run_id, workspace, _with_plan(empty, graph), AgentStatus.RUNNING)

        return await self._drive(
            run_id,
            objective,
            workspace,
            graph,
            catalog,
            policy,
            verification=verification,
            prompt=prompt,
            cancellation=cancellation,
        )

    async def stored_plan(self, run_id: str) -> StoredPlan | None:
        """El plan que este run dejó a medias, si dejó alguno.

        `recover` marca como pendientes de decisión las tareas que estaban en marcha
        cuando el proceso murió: no se sabe si llegaron a hacer lo suyo, y esa diferencia
        no la puede resolver el propio runtime.
        """
        if self.settings.graphs is None:
            return None
        try:
            return await self.settings.graphs.recover(run_id)
        except AthenaRuntimeError as error:
            _logger.warning("plan.not_recovered code=%s", error.code)
            return None

    async def continue_graph(
        self,
        run_id: str,
        stored: StoredPlan,
        workspace: Workspace,
        catalog: dict[str, Tool],
        policy: PermissionPolicy,
        *,
        verification: VerificationPolicy | None = None,
        prompt: PermissionPrompt | None = None,
        cancellation: CancellationToken,
    ) -> GraphResult:
        """Seguir un plan donde se quedó, sin volver a planificar.

        Las tareas que ya terminaron no se repiten: el frontal listo del grafo las salta
        solo. Pedirle otro plan al modelo daría uno distinto y tiraría la evidencia que
        las tareas hechas ya habían producido.
        """
        objective = stored.objective or _first_goal(stored.graph)
        await self._publish(
            run_id, EventName.AGENT_STARTED, {"objective": objective, "resumed": True}
        )
        return await self._drive(
            run_id,
            objective,
            workspace,
            stored.graph,
            catalog,
            policy,
            verification=verification,
            prompt=prompt,
            cancellation=cancellation,
        )

    async def _drive(
        self,
        run_id: str,
        objective: str,
        workspace: Workspace,
        graph: TaskGraph,
        catalog: dict[str, Tool],
        policy: PermissionPolicy,
        *,
        verification: VerificationPolicy | None,
        prompt: PermissionPrompt | None,
        cancellation: CancellationToken,
    ) -> GraphResult:
        """Ejecuta un grafo, venga de planificar o de recuperarlo.

        Una sola forma de armar el ejecutor. Con dos, un run reanudado podría acabar con
        permisos, perfiles o verificación distintos de los del run original, y nada en las
        pruebas de ninguno de los dos caminos lo notaría.
        """
        manager = TaskManager()
        # Los perfiles se recortan a la autoridad de este run. Sin esto, el perfil del
        # coder escribe sin preguntar —así viene definido— y un run creado con «pregunta
        # antes de escribir» escribiría igualmente en cuanto se planificase: la elección
        # del cliente la anularía en silencio una constante del módulo de subagentes.
        profiles = {
            role: _budgeted(
                confine(profile, policy, frozenset(catalog)),
                self.settings.task_timeout_seconds,
            )
            for role, profile in DEFAULT_PROFILES.items()
        }
        runner = SubagentRunner(
            self.provider,
            catalog,
            self.event_bus,
            self.result_store,
            profiles=profiles,
            prompt=prompt,
        )
        executor = GraphExecutor(
            runner,
            manager,
            self.event_bus,
            goal_verification=verification,
            board=self.settings.board,
            store=self.settings.graphs,
        )
        # A scope of its own so cancelling the run stops the plan, while a subgraph giving
        # up does not read as the user having cancelled the whole thing.
        scoped = chained_source(cancellation, CancellationScope.SUBGRAPH)
        try:
            result = await executor.execute(graph, workspace, scoped.token, run_id=run_id)
        finally:
            await manager.shutdown()

        await self._finish(run_id, workspace, objective, result)
        return result

    # -- keeping the run addressable ---------------------------------------

    async def _persist(
        self, run_id: str, workspace: Workspace, working: WorkingState, status: AgentStatus
    ) -> None:
        """Write a session for the run itself.

        A graph run has no `AgentLoop` writing one, and without it `GET /v1/runs/{id}`
        answers 404 for a run the client is watching, the recovery list never mentions it,
        and a reconnecting client resynchronises against nothing.
        """
        record = SessionRecord(
            session_id=run_id,
            workspace_id=workspace.workspace_id,
            status=status,
            working_memory=working,
            # Copiada al nivel del registro igual que hace el bucle. Es de donde la lee la
            # proyección, así que dejarla sólo dentro de la memoria de trabajo enseñaba un
            # run terminado sin nada que respaldarlo — teniéndolo.
            verification=dict(working.verification),
            updated_at=datetime.now(UTC),
        )
        try:
            await self.session_store.save(record)
        except AthenaRuntimeError as error:
            _logger.warning("session.not_persisted code=%s", error.code)
            return
        await self._publish(run_id, EventName.SESSION_PERSISTED, {"status": status.value})

    async def _finish(
        self, run_id: str, workspace: Workspace, objective: str, result: GraphResult
    ) -> None:
        """Record how the plan ended, and say so in the vocabulary a client stops on.

        `graph.completed` already went out, but a client's definition of "this run is over"
        is the `agent.*` event — the same one the loop publishes. Leaving it out would end
        a finished graph run with a client still waiting for it.
        """
        status = {
            ExecutionOutcome.COMPLETED: AgentStatus.COMPLETED,
            ExecutionOutcome.FAILED: AgentStatus.FAILED,
            ExecutionOutcome.CANCELLED: AgentStatus.CANCELLED,
            ExecutionOutcome.TIMED_OUT: AgentStatus.FAILED,
        }[result.outcome]
        summary = "" if result.goal_verification is None else result.goal_verification.summary
        working = _with_plan(WorkingState(objective=objective), result.graph).modifying(
            files_modified=result.files_changed
        )
        if result.goal_verification is not None:
            working = working.verified(
                {"status": result.goal_verification.status.value, "summary": summary}
            )
        await self._persist(run_id, workspace, working, status)

        if status is AgentStatus.COMPLETED:
            await self._publish(
                run_id,
                EventName.AGENT_COMPLETED,
                {
                    "tasks": len(result.graph),
                    "repair_cycles": 0,
                    "verification": summary,
                    "files_changed": list(result.files_changed),
                },
            )
            return
        failed = [item for item in result.evidence if not item.succeeded]
        code = next((item.error_code for item in failed if item.error_code), "graph_incomplete")
        message = failed[0].summary if failed else "The plan did not finish"
        await self._publish(
            run_id,
            EventName.AGENT_CANCELLED
            if status is AgentStatus.CANCELLED
            else EventName.AGENT_FAILED,
            {"error_code": code, "message": message, "outcome": result.outcome.value},
        )

    async def _publish(self, run_id: str, name: EventName, payload: JSONObject) -> None:
        await self.event_bus.publish(RuntimeEvent(name, run_id, payload))


def _whole_goal(objective: str) -> TaskGraph:
    """The goal as a plan of one task, for a run that must go through the graph.

    Its acceptance criterion is the project's own checks, which is the same thing the
    executor verifies at the end. Inventing a narrower criterion would let the task report
    success against a bar nobody set.
    """
    return TaskGraph.build(
        [
            TaskNode(
                id="whole",
                goal=objective,
                expected_output="The objective, carried out in the workspace.",
                acceptance_criteria=("The project's own verification commands pass.",),
                suggested_role=SubagentRole.CODER,
                toolsets=DEFAULT_PROFILES[SubagentRole.CODER].toolsets,
            )
        ]
    )


def _budgeted(profile: SubagentProfile, seconds: float | None) -> SubagentProfile:
    """Dar a una tarea el reloj del despliegue, sin tocar sus otros límites.

    Sólo el reloj. Las iteraciones y las llamadas a herramienta acotan cuánto *hace* un
    delegado, y eso no cambia porque el modelo sea lento; el tiempo sí.
    """
    if seconds is None:
        return profile
    return replace(profile, budget=replace(profile.budget, timeout_seconds=seconds))


def _first_goal(graph: TaskGraph) -> str:
    """Con qué nombrar un plan cuyo objetivo no se guardó.

    Un plan viejo puede no traerlo. El objetivo de su primera tarea describe mal el
    conjunto, pero describe algo real; inventar una frase sería peor.
    """
    nodes = graph.topological_order()
    return nodes[0].goal if nodes else "Plan sin objetivo registrado"


def _settled(shape: RunShape) -> DecompositionDecision:
    """La decisión ya tomada, en los términos en que el planificador la entiende.

    Cuando el cliente pidió el plan que la política no habría propuesto, la razón es ésa y
    se dice: la explicación viaja al informe del run, y llamarla "criterios cumplidos"
    sería atribuir a la evidencia una decisión que no tomó.
    """
    if shape.decision.decompose == shape.hierarchical:
        return shape.decision
    return DecompositionDecision(
        shape.hierarchical,
        shape.decision.reasons,
        "Requested by the caller rather than argued for by the evidence.",
    )


def _with_plan(working: WorkingState, graph: TaskGraph) -> WorkingState:
    """Put the plan where a reconnecting client looks for it.

    The snapshot is what a client resynchronises against, so a plan that lives only in the
    event stream disappears the moment somebody closes a laptop lid. Dependency order,
    because that is the order the work happens in and any other order invites a reader to
    infer a sequence that is not there.
    """
    nodes = graph.topological_order()
    steps = tuple(
        PlanStep(node.goal, _AS_STEP.get(node.status, StepStatus.PENDING), task_id=node.id)
        for node in nodes
    )
    if not steps:
        return working
    running = next(
        (index for index, node in enumerate(nodes) if node.status is PlanStatus.RUNNING), None
    )
    return working.with_plan(steps, running)


__all__ = ["ExecutionMode", "OrchestrationSettings", "Orchestrator", "RunShape"]
