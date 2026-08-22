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
from athena.checkpoints import CheckpointStore
from athena.delegation import confine
from athena.diagnosis import diagnose_result, inconclusive_reason
from athena.errors import (
    AthenaRuntimeError,
    ToolValidationError,
    VerificationInconclusive,
)
from athena.events import EventBus, EventName, RuntimeEvent
from athena.graph_executor import GraphExecutor, GraphResult
from athena.graph_store import SqliteGraphStore, StoredPlan
from athena.hooks import HookRegistry
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
from athena.project_memory import (
    MemoryKind,
    SqliteProjectMemory,
    VerificationState,
    render_for_context,
)
from athena.rollback import RollbackLedger, checkpointing_hook
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
from athena.types import JSONObject, JSONValue
from athena.verification import VerificationPolicy, VerificationResult
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
    #: Donde se guardan las copias previas a una escritura, si el despliegue quiere poder
    #: deshacer. Sin ella no se copia nada y no hay nada que deshacer, que es exactamente
    #: lo que pasaba antes: `rollback.py` existia entero y no lo importaba nadie.
    checkpoints: CheckpointStore | None = None
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


class ShapeReason(StrEnum):
    """Why a run ended up with the shape it has, in a value that survives rewording.

    The sentences beside these are for people and will be rewritten; a count of how often
    a deployment falls back for want of a planner must not depend on the wording holding
    still. Anything that aggregates reads this and never the prose.
    """

    #: The caller required the loop.
    CALLER_REQUIRED_DIRECT = "caller_required_direct"
    #: The caller required a graph.
    CALLER_REQUIRED_HIERARCHICAL = "caller_required_hierarchical"
    #: `auto`, and this deployment has no planning layer to offer.
    PLANNING_UNAVAILABLE = "planning_unavailable"
    #: `auto`, and the evidence about the goal did not argue for decomposing it.
    POLICY_DECLINED = "policy_declined"
    #: `auto`, and the plan that came back is worth executing as a graph.
    POLICY_ENDORSED = "policy_endorsed"
    #: `auto`, and the plan is valid but buys nothing a loop does not already do.
    PLAN_NOT_WORTHWHILE = "plan_not_worthwhile"
    #: `auto`, and no usable plan came back, so the goal runs directly.
    PLAN_REFUSED = "plan_refused"
    #: `hierarchical`, and no usable plan came back, so the whole goal is one task.
    NO_USABLE_PLAN = "no_usable_plan"


@dataclass(frozen=True, slots=True)
class RunShape:
    """How this run will be executed, and why.

    `mode` is what was asked for and `hierarchical` is what will happen. They agree except
    in `AUTO`, where the second is the answer to a question the first only posed.

    `reason` is separate from `decision.explanation` because they answer different
    questions and can disagree. A deployment with planning switched off runs a goal on the
    loop while the policy still holds that decomposing it was worth it — and reporting the
    policy's sentence there would have the run explain itself with a verdict that is not
    the one it acted on.
    """

    mode: ExecutionMode
    hierarchical: bool
    decision: DecompositionDecision
    signals: DecompositionSignals
    code: ShapeReason = ShapeReason.POLICY_ENDORSED
    reason: str = ""
    #: What the scout could not establish, carried so a client can show it rather than
    #: being told a guess was a measurement.
    assumed: tuple[str, ...] = ()

    def to_json(self) -> JSONObject:
        """The four fields anything counting needs, plus the sentences people read.

        `policy_verdict` is `decompose` or `decline` rather than the policy's sentence:
        the sentence is next to it under `policy_explanation`, and a dashboard grouping
        runs by what the policy thought should not have to match prose to do it.
        """
        return {
            "execution_mode": self.mode.value,
            "executed_as": "hierarchical" if self.hierarchical else "direct",
            "reason_code": self.code.value,
            "reason": self.reason,
            "policy_verdict": "decompose" if self.decision.decompose else "decline",
            "policy_explanation": self.decision.explanation,
            "criteria_met": list(self.decision.reasons),
            "assumed_signals": list(self.assumed),
        }

    def as_decided(self, *, hierarchical: bool, code: ShapeReason, reason: str) -> RunShape:
        """The same run, once something later settled what it will actually do."""
        return replace(self, hierarchical=hierarchical, code=code, reason=reason)


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
        self._ledgers: dict[str, RollbackLedger] = {}

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
            declined = "The caller required the loop, so nothing was measured."
            return RunShape(
                mode,
                hierarchical=False,
                decision=DecompositionDecision(False, (), declined),
                signals=DecompositionSignals(),
                code=ShapeReason.CALLER_REQUIRED_DIRECT,
                reason=declined,
            )
        if mode is ExecutionMode.HIERARCHICAL and not self.settings.planning:
            # Refused rather than quietly downgraded. `HIERARCHICAL` is a requirement, not
            # a preference: somebody who asks for a graph is usually measuring one, and a
            # loop that reported itself as a run would corrupt the measurement instead of
            # failing it. `AUTO` asks for the best available strategy, so the same missing
            # layer is a fallback there rather than a broken promise.
            raise ToolValidationError(
                "This deployment does not do hierarchical runs; execution_mode must be "
                "auto or direct"
            )
        scouted = self.scout.scout(workspace, objective)
        signals = merge(scouted, supplied) if supplied is not None else scouted.signals
        decision = self.settings.policy.assess(signals)
        if mode is ExecutionMode.HIERARCHICAL:
            hierarchical, code, reason = (
                True,
                ShapeReason.CALLER_REQUIRED_HIERARCHICAL,
                "The caller required hierarchical execution.",
            )
        elif not self.settings.planning:
            hierarchical, code, reason = (
                False,
                ShapeReason.PLANNING_UNAVAILABLE,
                "auto -> direct: this deployment has planning switched off, so the loop "
                "is the best strategy available.",
            )
        elif decision.decompose:
            hierarchical, code, reason = (
                True,
                ShapeReason.POLICY_ENDORSED,
                decision.explanation,
            )
        else:
            hierarchical, code, reason = (
                False,
                ShapeReason.POLICY_DECLINED,
                f"auto -> direct: {decision.explanation}",
            )
        return RunShape(
            mode,
            hierarchical=hierarchical,
            decision=decision,
            signals=signals,
            code=code,
            reason=reason,
            assumed=scouted.assumed,
        )

    async def announce(self, run_id: str, shape: RunShape) -> None:
        """Report a shape that was settled before any planning happened."""
        await self._decided(
            run_id,
            shape,
            hierarchical=shape.hierarchical,
            code=shape.code,
            reason=shape.reason,
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

    def ledger_for(self, run_id: str) -> RollbackLedger | None:
        """El libro de deshacer de un run, creado la primera vez que se pide.

        Uno por run y no uno global: un rollback de run tiene que poder deshacer todo lo
        de ese run y nada de los demas, y un libro compartido no sabria donde acaba uno.
        """
        if self.settings.checkpoints is None:
            return None
        libro = self._ledgers.get(run_id)
        if libro is None:
            libro = RollbackLedger(self.settings.checkpoints)
            self._ledgers[run_id] = libro
        return libro

    async def learn_from(
        self, project_id: str, verification: VerificationResult | None, run_id: str
    ) -> int:
        """Guardar los comandos que se ejecutaron y pasaron, ya verificados.

        Aqui `VERIFIED` no es un ascenso de cortesia: el runtime ejecuto ese comando en
        este workspace y salio bien, que es literalmente «algo lo comprobo». Es la unica
        promocion honesta que Athena puede hacerse a si misma; la siguiente —que una
        persona lo respalde— tiene que venir de una persona.

        Y es lo unico que se escribe automaticamente. Dejar que un run guardase sus
        conclusiones convertiria la memoria en una segunda copia, mas vieja, de lo que el
        modelo creyo entender.
        """
        if self.settings.memory is None or verification is None:
            return 0
        aprendidos = 0
        for evidence in verification.evidence:
            comando = evidence.metadata.get("command")
            if not evidence.metadata.get("passed") or not isinstance(comando, str):
                continue
            if not comando.strip():
                continue
            try:
                item = await self.settings.memory.propose(
                    project_id,
                    MemoryKind.VERIFIED_COMMAND,
                    comando.strip(),
                    source=f"run:{run_id}",
                    source_reference=str(evidence.metadata.get("name") or ""),
                )
                await self.settings.memory.approve(
                    item.id, state=VerificationState.VERIFIED, confidence=0.9
                )
                aprendidos += 1
            except AthenaRuntimeError as error:
                # Aprender no puede tumbar un run que ya termino bien. Lo que se pierde es
                # un recuerdo; lo que se salvaria fallando aqui, nada.
                _logger.warning("memory.learn_failed code=%s", error.code)
        return aprendidos

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
                await self._decided(
                    run_id,
                    shape,
                    hierarchical=False,
                    code=ShapeReason.PLAN_REFUSED,
                    reason=f"auto -> direct: no usable plan came back ({reason}), so "
                    "the goal runs on the loop.",
                )
                return None
            # The caller asked for a graph, so a graph is what runs. One task holding the
            # whole goal is a truthful plan — it says the work was not divided — and it
            # keeps the guarantee the mode exists for: this path is always the graph path.
            graph = _whole_goal(objective)
            await self._decided(
                run_id,
                shape,
                hierarchical=True,
                code=ShapeReason.NO_USABLE_PLAN,
                reason=f"hierarchical: no usable plan ({reason}), so the whole goal runs "
                "as one task.",
            )
        elif shape.mode is ExecutionMode.HIERARCHICAL:
            await self._decided(
                run_id, shape, hierarchical=True, code=shape.code, reason=shape.reason
            )
        else:
            # The plan exists and is valid; whether it is worth executing as a graph is a
            # different question, and the policy owns it. Asking it here rather than inside
            # `TaskGraph.build` keeps validity and worth apart: a structurally fine plan
            # that buys nothing is not an invalid plan.
            worth = self.settings.policy.assess_plan(graph)
            if not worth.decompose:
                await self._decided(
                    run_id,
                    shape,
                    hierarchical=False,
                    code=ShapeReason.PLAN_NOT_WORTHWHILE,
                    reason=f"auto -> direct: {worth.explanation}",
                )
                return None
            await self._decided(
                run_id,
                shape,
                hierarchical=True,
                code=ShapeReason.POLICY_ENDORSED,
                reason=worth.explanation,
            )

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
        libro = self.ledger_for(run_id)
        runner = SubagentRunner(
            self.provider,
            catalog,
            self.event_bus,
            self.result_store,
            profiles=profiles,
            prompt=prompt,
            # Los ganchos bajan al hijo: en un run jerarquico las escrituras pasan ahi, y
            # unos ganchos que se quedasen arriba no verian ni una sola escritura del run.
            hooks=None if libro is None else HookRegistry((checkpointing_hook(libro, workspace),)),
        )
        executor = GraphExecutor(
            runner,
            manager,
            self.event_bus,
            goal_verification=verification,
            board=self.settings.board,
            store=self.settings.graphs,
            rollback=libro,
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

    async def _decided(
        self,
        run_id: str,
        shape: RunShape,
        *,
        hierarchical: bool,
        code: ShapeReason,
        reason: str,
    ) -> None:
        """Say once, when the answer is final, how this run will execute and why.

        Once: a run that announced a shape before planning and a different one after would
        leave whoever is counting having to guess which announcement to believe.
        """
        settled = shape.as_decided(hierarchical=hierarchical, code=code, reason=reason)
        _logger.info(
            "plan.decided requested=%s selected=%s code=%s",
            settled.mode.value,
            "hierarchical" if hierarchical else "direct",
            code.value,
        )
        await self._publish(run_id, EventName.PLAN_DECIDED, settled.to_json())

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
        payload = {**_ending(result), "outcome": result.outcome.value}
        await self._publish(
            run_id,
            EventName.AGENT_CANCELLED
            if status is AgentStatus.CANCELLED
            else EventName.AGENT_FAILED,
            payload,
        )

    async def _publish(self, run_id: str, name: EventName, payload: JSONObject) -> None:
        await self.event_bus.publish(RuntimeEvent(name, run_id, payload))


def _ending(result: GraphResult) -> dict[str, JSONValue]:
    """Por que no termino bien, en los terminos de quien tendria que hacer algo.

    Tres finales distintos que antes se contaban como uno: una tarea que fallo, un plan
    que termino entero y no se pudo dar por bueno, y un plan que no llego al final. Decir
    «the plan did not finish» del segundo mandaba a mirar unas tareas que estaban todas
    completadas — la peor pista posible, porque parece informacion.
    """
    failed = [item for item in result.evidence if not item.succeeded]
    if failed:
        return {
            "error_code": next(
                (item.error_code for item in failed if item.error_code), "graph_incomplete"
            ),
            "message": failed[0].summary,
        }
    goal = result.goal_verification
    if goal is not None and not goal.permits_completion:
        razon = inconclusive_reason(diagnose_result(goal))
        if razon is None:
            return {"error_code": "verification_failure", "message": goal.summary}
        return {
            "error_code": VerificationInconclusive.code,
            "message": goal.summary,
            "reason": razon.value,
        }
    return {"error_code": "graph_incomplete", "message": "The plan did not finish"}


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
