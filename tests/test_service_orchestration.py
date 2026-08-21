"""The service choosing between a loop and a graph, from the client's side of the door.

Every layer below this has its own suite. What none of them could show is the thing that
was actually broken: a run created over HTTP only ever assembled an `AgentLoop`, so the
planning layer existed and never ran for anybody using ChatyGPT. These tests are written
against `RunRegistry.start`, the same entry point the HTTP adapter calls, because that is
where the gap was and a unit test one level down would have agreed with the bug.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from athena.adapters.service.approvals import PendingApproval
from athena.adapters.service.orchestration import (
    ExecutionMode,
    OrchestrationSettings,
    Orchestrator,
    _budgeted,
)
from athena.adapters.service.runs import CapabilityMode, RunOptions, RunRegistry
from athena.cancellation import CancellationSource, CancellationToken
from athena.context import ContextBuilder
from athena.errors import ToolValidationError
from athena.events import EventName, InMemoryEventBus, ModelEvent, RuntimeEvent
from athena.graph_store import SqliteGraphStore
from athena.models import (
    ModelCapabilities,
    ModelHealth,
    ModelHealthStatus,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
)
from athena.permissions import PermissionDecision
from athena.planning import (
    DecompositionPolicy,
    DecompositionSignals,
    PlanStatus,
    TaskGraph,
    TaskNode,
)
from athena.session_store import SessionRecord, SqliteSessionStore
from athena.state import AgentStatus
from athena.stores import SqliteToolResultStore
from athena.subagents import DEFAULT_PROFILES, SubagentRole
from athena.types import JSONObject
from athena.working_state import StepStatus, WorkingState
from athena.workspace import Workspace

WORKING = "def add(a, b):\n    return a + b\n"
TEST = "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"

PLAN = json.dumps(
    {
        "tasks": [
            {
                "id": "T01",
                "goal": "describe how addition is implemented",
                "expected_output": "the function named",
                "acceptance_criteria": ["names a file and a function"],
                "suggested_role": "explorer",
            },
            {
                "id": "T02",
                "goal": "record the result of the addition test",
                "expected_output": "a sentence about the test",
                "acceptance_criteria": ["states whether it passes"],
                "dependencies": ["T01"],
                "suggested_role": "coder",
            },
        ]
    }
)

#: Enough of the six criteria to clear both of the policy's gates.
STRONG = DecompositionSignals(
    independently_verifiable_outputs=3,
    has_meaningful_dependencies=True,
    subsystems_touched=2,
    distinct_roles_required=2,
)

#: One thing to check at the end, which is the case the policy exists to refuse.
WEAK = DecompositionSignals(independently_verifiable_outputs=1)


def _git(root: Path, *arguments: str) -> None:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments], capture_output=True, check=False, text=True
    )
    if completed.returncode != 0:
        pytest.fail(f"git {' '.join(arguments)} failed: {completed.stderr}")


def _sandbox(root: Path) -> Workspace:
    """A repository whose own checks pass, so verification proves something real."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "calc.py").write_text(WORKING, encoding="utf-8")
    (root / "test_calc.py").write_text(TEST, encoding="utf-8")
    command = f'"{sys.executable}" -m pytest -q'
    (root / "AGENTS.md").write_text(
        f"# Sandbox\n\n## Verification\n\n```\n{command}\n```\n", encoding="utf-8"
    )
    _commit(root)
    return Workspace.from_path(root)


def _wide_sandbox(root: Path) -> Workspace:
    """A repository the scout can read as more than one thing.

    Two source directories and two checks it can actually run, because that is what makes
    `independently_verifiable_outputs` a measurement rather than a guess: the scout counts
    the project's own commands, and a project defining one command has one output whatever
    anybody says about it.
    """
    root.mkdir(parents=True, exist_ok=True)
    for package in ("api", "core"):
        (root / package).mkdir()
        (root / package / "__init__.py").write_text("", encoding="utf-8")
        (root / package / "unit.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "test_all.py").write_text(
        "from api.unit import VALUE\n\n\ndef test_value():\n    assert VALUE == 1\n",
        encoding="utf-8",
    )
    checks = f'"{sys.executable}" -m pytest -q\n"{sys.executable}" -m compileall -q api core'
    (root / "AGENTS.md").write_text(
        f"# Wide\n\n## Verification\n\n```\n{checks}\n```\n", encoding="utf-8"
    )
    _commit(root)
    return Workspace.from_path(root)


def _commit(root: Path) -> None:
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "athena@example.invalid")
    _git(root, "config", "user.name", "Athena Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")


class _Scripted(ModelProvider):
    """Answers the planner and the specialists, and remembers who asked.

    The planner is told apart by its response schema rather than by matching prose: the
    schema is what makes a request a planning request, so a change to the wording of the
    instructions cannot quietly turn this fake into something that never plans.
    """

    def __init__(self, plan: str = PLAN, *, calls: Sequence[ModelToolCall] = ()) -> None:
        self.plan = plan
        self._calls = list(calls)
        self.planning_requests = 0
        self.prompts: list[str] = []

    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        cancellation.raise_if_cancelled()
        self.prompts.append("\n".join(message.content for message in request.messages))
        if request.response_schema is not None:
            self.planning_requests += 1
            return ModelResponse(self.plan, "scripted", "stop")
        if self._calls and "Athena's coder" in self.prompts[-1]:
            return ModelResponse("", "scripted", "tool_use", tool_calls=(self._calls.pop(0),))
        return ModelResponse("done", "scripted", "stop")

    async def stream(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> AsyncIterator[ModelEvent]:
        del request, cancellation
        if False:
            yield ModelEvent(EventName.MODEL_COMPLETED, "never")

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(False, True, True)

    async def health(self, cancellation: CancellationToken) -> ModelHealth:
        cancellation.raise_if_cancelled()
        return ModelHealth(ModelHealthStatus.HEALTHY)


def _registry(
    tmp_path: Path,
    provider: ModelProvider,
    bus: InMemoryEventBus,
    *,
    planning: bool = True,
    graphs: SqliteGraphStore | None = None,
) -> RunRegistry:
    return RunRegistry(
        provider,
        bus,
        SqliteSessionStore(tmp_path / "sessions.db"),
        SqliteToolResultStore(tmp_path / "results.db"),
        orchestration=OrchestrationSettings(planning=planning, graphs=graphs),
    )


def _orchestrator(tmp_path: Path, *, planning: bool) -> Orchestrator:
    return Orchestrator(
        _Scripted(),
        InMemoryEventBus(),
        SqliteSessionStore(tmp_path / "s.db"),
        SqliteToolResultStore(tmp_path / "r.db"),
        OrchestrationSettings(planning=planning),
    )


# ------------------------------------------------------------------ the decision


def test_one_verifiable_output_stays_on_the_loop(tmp_path: Path) -> None:
    """The gate that matters, reached through the service rather than the policy.

    A graph over a single output buys nothing and costs hand-offs, and the client cannot
    be the one deciding that — it does not know what is in the repository.
    """
    workspace = _sandbox(tmp_path / "repo")
    shape = _orchestrator(tmp_path, planning=True).decide(workspace, "fix a typo", WEAK)
    assert not shape.hierarchical
    assert shape.mode is ExecutionMode.AUTO
    assert "one verifiable output" in shape.decision.explanation.lower()


def test_evidence_for_several_outputs_argues_for_a_graph(tmp_path: Path) -> None:
    """Two checks over two subsystems: two things that can be proved apart."""
    workspace = _wide_sandbox(tmp_path / "wide")
    shape = _orchestrator(tmp_path, planning=True).decide(workspace, "rework the parser", STRONG)
    assert shape.hierarchical
    assert len(shape.decision.reasons) >= 2


def test_what_a_caller_supplies_cannot_overrule_what_the_repository_shows(
    tmp_path: Path,
) -> None:
    """The caller fills gaps; it does not get to contradict the filesystem.

    A client convinced its goal has three separable outputs, in a repository defining one
    check, does not get a graph. The direction matters: were it the other way round, any
    client could talk the runtime into decomposing anything by asserting it should.
    """
    workspace = _sandbox(tmp_path / "repo")
    shape = _orchestrator(tmp_path, planning=True).decide(workspace, "rework the parser", STRONG)
    assert not shape.hierarchical
    assert shape.signals.independently_verifiable_outputs == 1
    # What it could not measure, it did take: dependencies are not visible in a filesystem.
    assert shape.signals.has_meaningful_dependencies
    assert "has_meaningful_dependencies" in shape.assumed


def test_direct_never_asks_the_repository_anything(tmp_path: Path) -> None:
    """`direct` es una orden, no una preferencia: ni se mide ni se delibera.

    Leer el repositorio para después ignorar la lectura sería trabajo cuyo resultado ya no
    puede cambiar nada, y dejaría un informe con señales que no se usaron para decidir.
    """
    workspace = _wide_sandbox(tmp_path / "wide")
    shape = _orchestrator(tmp_path, planning=True).decide(
        workspace, "rework the parser", STRONG, mode=ExecutionMode.DIRECT
    )
    assert not shape.hierarchical
    assert shape.mode is ExecutionMode.DIRECT
    assert shape.signals == DecompositionSignals()
    assert "nothing was measured" in shape.reason


def test_hierarchical_overrules_a_policy_that_would_have_said_no(tmp_path: Path) -> None:
    """Un objetivo simple pedido en modo grafo sale por el grafo.

    Es el modo de las pruebas, el depurado y los benchmarks: quien lo pide quiere observar
    el grafo, y darle el bucle mediría otra cosa sin decírselo.
    """
    workspace = _sandbox(tmp_path / "repo")
    shape = _orchestrator(tmp_path, planning=True).decide(
        workspace, "fix a typo", WEAK, mode=ExecutionMode.HIERARCHICAL
    )
    assert shape.hierarchical
    # La política sigue diciendo lo que piensa; lo que cambia es quién decide.
    assert not shape.decision.decompose


def test_a_required_capability_fails_loud_and_an_optional_one_falls_back(
    tmp_path: Path,
) -> None:
    """La misma capa ausente, dos respuestas, porque no se pidió igual.

    En `hierarchical` la planificación es un requisito: quien la exige suele estar
    midiéndola, y un bucle que se presentase como el run pedido corrompería la medición
    en vez de fallarla. En `auto` no es un requisito sino una optimización —«elige la
    mejor estrategia disponible»— y el bucle es una estrategia perfectamente válida.

    Lo que no cambia entre los dos casos es que quede dicho por qué.
    """
    workspace = _wide_sandbox(tmp_path / "wide")
    orquestador = _orchestrator(tmp_path, planning=False)

    with pytest.raises(ToolValidationError, match="auto or direct"):
        orquestador.decide(workspace, "rework the parser", STRONG, mode=ExecutionMode.HIERARCHICAL)

    degradado = orquestador.decide(workspace, "rework the parser", STRONG)
    assert not degradado.hierarchical
    assert "auto -> direct" in degradado.reason
    assert "planning switched off" in degradado.reason
    # Y la política sigue diciendo lo suyo, que es distinto de lo que se hizo: sin esta
    # separación el run se explicaría con un veredicto sobre el que no actuó.
    assert degradado.decision.decompose
    assert degradado.to_json()["executed_as"] == "direct"


def test_the_mode_travels_with_the_shape_it_produced(tmp_path: Path) -> None:
    """Lo pedido y lo ocurrido se informan por separado.

    En `auto` no coinciden por casualidad: uno es la pregunta y el otro la respuesta, y un
    cliente que quiera explicar por qué su run fue como fue necesita los dos.
    """
    workspace = _wide_sandbox(tmp_path / "wide")
    informe = (
        _orchestrator(tmp_path, planning=True)
        .decide(workspace, "rework the parser", STRONG)
        .to_json()
    )

    assert informe["execution_mode"] == "auto"
    assert informe["executed_as"] == "hierarchical"
    assert informe["criteria_met"]
    assert informe["reason"]


# ------------------------------------------------------------------ the graph path


def test_a_graph_run_is_addressable_and_ends_like_any_other(tmp_path: Path) -> None:
    """The properties a client depends on, none of which the graph layer had.

    A run started from ChatyGPT must answer `GET /v1/runs/{id}` immediately, must show its
    plan to a client that reconnects, and must finish with the `agent.*` event a client
    stops on. A graph that only published `graph.completed` would leave the application
    waiting for a run that ended minutes ago.
    """

    async def scenario() -> None:
        workspace = _sandbox(tmp_path / "repo")
        bus = InMemoryEventBus()
        seen: list[RuntimeEvent] = []
        bus.subscribe(seen.append)
        provider = _Scripted()
        registry = _registry(tmp_path, provider, bus)
        run_id = await registry.start(
            "investigate the addition path",
            workspace,
            RunOptions(
                writes=CapabilityMode.ALLOW,
                execution=CapabilityMode.ALLOW,
                execution_mode=ExecutionMode.HIERARCHICAL,
            ),
        )
        try:
            await asyncio.wait_for(registry.wait(run_id), timeout=180)
        finally:
            await registry.shutdown()

        names = [event.name for event in seen]
        assert provider.planning_requests == 1
        assert EventName.GRAPH_STARTED in names
        assert EventName.AGENT_STARTED in names
        assert EventName.AGENT_COMPLETED in names, "a client waiting on agent.* never stops"

        started = [event for event in seen if event.name is EventName.TASK_STARTED]
        assert [event.payload["task_id"] for event in started] == ["T01", "T02"]
        # Dependencies travel with the event because whoever draws the plan needs them
        # before it has the graph, and may never have the graph at all.
        assert started[1].payload["dependencies"] == ["T01"]

        record = await registry.snapshot(run_id)
        assert record is not None
        assert record.status is AgentStatus.COMPLETED
        plan = record.working_memory.current_plan
        assert [step.description for step in plan] == [
            "describe how addition is implemented",
            "record the result of the addition test",
        ]
        assert all(step.status is StepStatus.DONE for step in plan)
        # Los pasos conservan la identidad de sus tareas: es lo que permite a un cliente
        # que reconecta reconocer en el plan las tareas de las que luego recibe eventos.
        assert [step.task_id for step in plan] == ["T01", "T02"]
        # Y la verificación está donde la proyección la lee. Un run terminado sin nada
        # que lo respalde es exactamente lo que la verificación existe para impedir.
        assert record.verification.get("status") == "passed"

    asyncio.run(scenario())


def test_a_run_is_fetchable_before_the_plan_exists(tmp_path: Path) -> None:
    """`start` must not return an id that answers 404 while a model is being asked.

    Planning is a model call, and a slow one. If the session were written after the plan
    came back, a client would spend that whole time fetching a run the store had never
    heard of and would reasonably give up on it.
    """

    async def scenario() -> None:
        workspace = _sandbox(tmp_path / "repo")
        bus = InMemoryEventBus()
        slow = _SlowPlanner()
        registry = _registry(tmp_path, slow, bus)
        run_id = await registry.start(
            "investigate the addition path",
            workspace,
            RunOptions(execution_mode=ExecutionMode.HIERARCHICAL),
        )
        try:
            record = await registry.snapshot(run_id)
            assert record is not None, "the run is not addressable yet"
            assert record.working_memory.objective == "investigate the addition path"
            assert not slow.released.is_set(), "the plan came back before the assertion"
        finally:
            slow.release()
            await registry.shutdown()

    asyncio.run(scenario())


class _SlowPlanner(_Scripted):
    """A planner that has not answered yet, and can be told to stay that way."""

    def __init__(self) -> None:
        super().__init__()
        self.released = asyncio.Event()

    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        if request.response_schema is not None:
            await self.released.wait()
        return await super().complete(request, cancellation)

    def release(self) -> None:
        self.released.set()


def test_auto_runs_a_refused_plan_directly_instead_of_failing_the_run(tmp_path: Path) -> None:
    """En `auto`, un plan que no se puede validar es motivo para trabajar directamente.

    La alternativa —tumbar el run porque un modelo devolvió JSON mal formado— convertiría
    la capa de planificación en una forma nueva de morir, mal negocio para una capa cuyo
    propósito entero es que los objetivos difíciles salgan bien más a menudo.
    """

    async def scenario() -> None:
        workspace = _wide_sandbox(tmp_path / "wide")
        bus = InMemoryEventBus()
        seen: list[RuntimeEvent] = []
        bus.subscribe(seen.append)
        provider = _Scripted(plan="not a plan at all")
        registry = _registry(tmp_path, provider, bus)
        run_id = await registry.start(
            "rework the parser across api and core",
            workspace,
            RunOptions(max_iterations=3),
        )
        try:
            result = await asyncio.wait_for(registry.wait(run_id), timeout=180)
        finally:
            await registry.shutdown()

        assert provider.planning_requests == 1, "auto no llegó a intentar el plan"
        assert result.status.value in ("completed", "failed")
        forma = _shape_of(seen)
        assert forma["executed_as"] == "direct", "la caída al bucle no se anunció"
        assert EventName.GRAPH_STARTED not in [event.name for event in seen]

    asyncio.run(scenario())


def test_hierarchical_keeps_its_promise_when_the_plan_is_refused(tmp_path: Path) -> None:
    """En `hierarchical`, un plan rechazado no puede acabar en el bucle.

    Quien fija el modo lo hace para saber qué camino corrió. Caer al bucle en silencio
    dejaría un benchmark comparando el grafo consigo mismo la mitad de las veces. Una
    tarea que contiene el objetivo entero es un plan verdadero: dice que no se dividió.
    """

    async def scenario() -> None:
        workspace = _sandbox(tmp_path / "repo")
        bus = InMemoryEventBus()
        seen: list[RuntimeEvent] = []
        bus.subscribe(seen.append)
        provider = _Scripted(plan="{ esto no es un plan }")
        registry = _registry(tmp_path, provider, bus)
        run_id = await registry.start(
            "investigate the addition path",
            workspace,
            RunOptions(
                writes=CapabilityMode.ALLOW,
                execution=CapabilityMode.ALLOW,
                execution_mode=ExecutionMode.HIERARCHICAL,
            ),
        )
        try:
            await asyncio.wait_for(registry.wait(run_id), timeout=180)
        finally:
            await registry.shutdown()

        names = [event.name for event in seen]
        assert EventName.GRAPH_STARTED in names, "el modo prometía un grafo"
        forma = _shape_of(seen)
        assert forma["executed_as"] == "hierarchical"
        assert "no usable plan" in str(forma["reason"]), (
            "el objetivo sin dividir se ejecutó sin decirlo"
        )

        record = await registry.snapshot(run_id)
        assert record is not None
        plan = record.working_memory.current_plan
        assert [step.description for step in plan] == ["investigate the addition path"]

    asyncio.run(scenario())


def test_hierarchical_uses_the_graph_even_when_the_policy_says_it_is_not_worth_it(
    tmp_path: Path,
) -> None:
    """Y sin que la política llegue a impedirlo: el modo ya respondió esa pregunta."""

    async def scenario() -> None:
        workspace = _sandbox(tmp_path / "repo")
        bus = InMemoryEventBus()
        seen: list[RuntimeEvent] = []
        bus.subscribe(seen.append)
        provider = _Scripted()
        registry = _registry(tmp_path, provider, bus)
        run_id = await registry.start(
            "investigate the addition path",
            workspace,
            RunOptions(
                writes=CapabilityMode.ALLOW,
                execution=CapabilityMode.ALLOW,
                execution_mode=ExecutionMode.HIERARCHICAL,
            ),
        )
        try:
            await asyncio.wait_for(registry.wait(run_id), timeout=180)
        finally:
            await registry.shutdown()

        assert provider.planning_requests == 1
        assert EventName.GRAPH_STARTED in [event.name for event in seen]

    asyncio.run(scenario())


def test_a_task_asks_for_permission_through_the_run_that_owns_it(tmp_path: Path) -> None:
    """The approval a graph task needs must reach the client watching the run.

    A graph run that built a permission prompt of its own would ask a registry nobody is
    listening to: the client would answer `resolve_permission` for a request the runtime
    never recorded, and the approval would vanish rather than fail loudly.
    """

    async def scenario() -> None:
        workspace = _sandbox(tmp_path / "repo")
        bus = InMemoryEventBus()
        provider = _Scripted(
            calls=[
                ModelToolCall(
                    "call-1",
                    "write_file",
                    {"path": "notes.md", "content": "written by a task"},
                )
            ]
        )
        registry = _registry(tmp_path, provider, bus)
        run_id = await registry.start(
            "investigate the addition path",
            workspace,
            RunOptions(writes=CapabilityMode.ASK, execution_mode=ExecutionMode.HIERARCHICAL),
        )
        subscriber = registry.subscribe(run_id, control=True)
        try:
            pending = await asyncio.wait_for(_await_approval(registry, run_id), timeout=180)
            assert pending.run_id == run_id
            assert pending.request.tool_name == "write_file"
            registry.approvals.acknowledge(pending.request_id, 60.0)
            assert registry.approvals.resolve(pending.request_id, PermissionDecision.ALLOW)
            await asyncio.wait_for(registry.wait(run_id), timeout=180)
        finally:
            registry.unsubscribe(subscriber)
            await registry.shutdown()

        assert (workspace.root / "notes.md").read_text(encoding="utf-8") == "written by a task"

    asyncio.run(scenario())


async def _await_approval(registry: RunRegistry, run_id: str) -> PendingApproval:
    while True:
        pending = registry.approvals.pending_for(run_id)
        if pending:
            return pending[0]
        await asyncio.sleep(0.05)


# ------------------------------------------------------------------ what the model is told


def _prompt(workspace: Workspace, *tools: str) -> str:
    async def build() -> str:
        request = await ContextBuilder(workspace).build_request(
            objective="do something",
            history=(),
            important_state={},
            tool_definitions=tuple({"name": name} for name in tools),
            cancellation=CancellationSource().token,
        )
        return request.messages[0].content

    return asyncio.run(build())


def test_a_run_with_writers_is_not_told_it_cannot_write(tmp_path: Path) -> None:
    """The one place the model was misinformed about its own capabilities.

    The system message said "read-only tools. Never claim to modify files" for every run,
    including runs holding `edit_file` — true when the runtime had only readers, and a
    contradiction ever since. A model told it cannot write, while being handed a writer,
    is being set up to refuse the work it was started for.
    """
    workspace = _sandbox(tmp_path / "repo")
    system = _prompt(workspace, "read_file", "glob", "write_file", "edit_file", "bash")
    assert "edit_file" in system
    assert "read-only" not in system
    assert "Never claim to modify files" not in system


def test_a_read_only_run_is_still_told_so(tmp_path: Path) -> None:
    workspace = _sandbox(tmp_path / "repo")
    system = _prompt(workspace, "read_file", "glob", "grep")
    assert "read-only" in system


def test_what_athena_remembers_is_labelled_rather_than_asserted(tmp_path: Path) -> None:
    """Recalled memory reaches the prompt as hints with their standing attached.

    It is not stated as fact because it is not fact: it is what an earlier session
    believed, and the repository in front of the model outranks it.
    """
    workspace = _sandbox(tmp_path / "repo")

    async def build() -> str:
        request = await ContextBuilder(
            workspace, notes="- [verified_command, unverified] pytest -q"
        ).build_request(
            objective="do something",
            history=(),
            important_state={},
            tool_definitions=({"name": "read_file"},),
            cancellation=CancellationSource().token,
        )
        return request.messages[0].content

    system = asyncio.run(build())
    assert "unverified" in system
    assert "pytest -q" in system


def test_the_prompt_follows_the_capabilities_the_run_was_actually_given(tmp_path: Path) -> None:
    """The wording is derived from the same list the run is handed, not configured beside it.

    Two sources of truth about what a run can do is how the read-only sentence survived
    three subsystems being added. One source cannot drift.
    """
    workspace = _sandbox(tmp_path / "repo")
    bus = InMemoryEventBus()
    registry = _registry(tmp_path, _Scripted(), bus)
    writing = registry.tools_for(RunOptions(writes=CapabilityMode.ASK), bus)
    reading = registry.tools_for(
        RunOptions(writes=CapabilityMode.OFF, execution=CapabilityMode.OFF), bus
    )
    assert "read-only" not in _prompt(workspace, *(tool.spec.name for tool in writing))
    assert "read-only" in _prompt(workspace, *(tool.spec.name for tool in reading))


# ------------------------------------------------------------------ picking a plan back up


async def _seeded(tmp_path: Path, run_id: str, *, running: bool) -> SqliteGraphStore:
    """A plan already half done, as a restart would have left it.

    `T01` finished and its evidence is recorded; `T02` is either still marked running —
    which is what a process killed mid-task leaves behind — or waiting its turn.
    """
    store = SqliteGraphStore(tmp_path / "graphs.db")
    graph = TaskGraph.build(
        [
            TaskNode(
                id="T01",
                goal="describe how addition is implemented",
                expected_output="the function named",
                acceptance_criteria=("names a file and a function",),
                suggested_role=SubagentRole.EXPLORER,
            ),
            TaskNode(
                id="T02",
                goal="record the result of the addition test",
                expected_output="a sentence about the test",
                acceptance_criteria=("states whether it passes",),
                dependencies=("T01",),
                suggested_role=SubagentRole.CODER,
            ),
        ]
    )
    graph.transition("T01", PlanStatus.READY)
    graph.transition("T01", PlanStatus.RUNNING)
    graph.transition("T01", PlanStatus.COMPLETED)
    if running:
        graph.transition("T02", PlanStatus.READY)
        graph.transition("T02", PlanStatus.RUNNING)
    await store.save(run_id, graph, objective="investigate the addition path")
    return store


async def _stopped(tmp_path: Path, run_id: str, workspace: Workspace) -> None:
    """A session left behind by a runtime that stopped watching it."""
    sessions = SqliteSessionStore(tmp_path / "sessions.db")
    await sessions.save(
        SessionRecord(
            session_id=run_id,
            workspace_id=workspace.workspace_id,
            status=AgentStatus.RECOVERY_PENDING,
            working_memory=WorkingState(objective="investigate the addition path"),
        )
    )


def test_a_plan_stopped_between_tasks_carries_on_where_it_stopped(tmp_path: Path) -> None:
    """Resuming re-runs what was never done, and only that.

    Asking the model for a fresh plan would produce a different one and throw away the
    evidence the finished tasks had already produced — which is the evidence the run will
    be judged on.
    """

    async def scenario() -> None:
        workspace = _sandbox(tmp_path / "repo")
        run_id = "run-between"
        graphs = await _seeded(tmp_path, run_id, running=False)
        await _stopped(tmp_path, run_id, workspace)

        bus = InMemoryEventBus()
        seen: list[RuntimeEvent] = []
        bus.subscribe(seen.append)
        provider = _Scripted()
        registry = _registry(tmp_path, provider, bus, graphs=graphs)

        await registry.resume(run_id, workspace)
        try:
            await asyncio.wait_for(registry.wait(run_id), timeout=180)
        finally:
            await registry.shutdown()

        assert provider.planning_requests == 0, "resuming must not ask for a new plan"
        started = [
            event.payload["task_id"] for event in seen if event.name is EventName.TASK_STARTED
        ]
        assert started == ["T02"], "the finished task was run again"

        record = await registry.snapshot(run_id)
        assert record is not None
        assert record.status is AgentStatus.COMPLETED

    asyncio.run(scenario())


def test_a_plan_interrupted_mid_task_is_not_resumed_on_a_guess(tmp_path: Path) -> None:
    """A task that was running when the process died has an unknown outcome.

    It may have written files and it may not, and the runtime cannot tell. Re-running it
    and skipping it are both decisions with consequences, so it makes neither: it says
    which task needs somebody to look, and stops.
    """

    async def scenario() -> None:
        workspace = _sandbox(tmp_path / "repo")
        run_id = "run-midtask"
        graphs = await _seeded(tmp_path, run_id, running=True)
        await _stopped(tmp_path, run_id, workspace)

        registry = _registry(tmp_path, _Scripted(), InMemoryEventBus(), graphs=graphs)
        try:
            with pytest.raises(ToolValidationError, match="T02"):
                await registry.resume(run_id, workspace)
        finally:
            await registry.shutdown()

    asyncio.run(scenario())


def test_a_task_gets_the_deployment_clock_and_keeps_its_other_limits(
    tmp_path: Path,
) -> None:
    """El reloj lo pone el despliegue; el resto del presupuesto, el perfil.

    Las iteraciones y las llamadas a herramienta acotan cuánto *hace* un delegado, y eso
    no cambia porque el modelo tarde. Subirlas junto con el tiempo dejaría que una tarea
    lenta hiciese además más cosas, que es una decisión distinta y que nadie pidió.
    """
    del tmp_path
    original = DEFAULT_PROFILES[SubagentRole.CODER]
    ampliado = _budgeted(original, 1800.0)

    assert ampliado.budget.timeout_seconds == 1800.0
    assert ampliado.budget.max_iterations == original.budget.max_iterations
    assert ampliado.budget.max_tool_calls == original.budget.max_tool_calls
    # Sin medida no se toca nada: el presupuesto del perfil es la respuesta correcta
    # cuando nadie ha medido este despliegue.
    assert _budgeted(original, None) is original


# ------------------------------------------------------ ¿merece la pena este plan?


def _plan_of(*tasks: tuple[str, str, tuple[str, ...]]) -> TaskGraph:
    """Un grafo a partir de (id, rol, dependencias), para juzgar formas de plan."""
    return TaskGraph.build(
        [
            TaskNode(
                id=task_id,
                goal=f"do {task_id}",
                expected_output=f"{task_id} done",
                acceptance_criteria=(f"{task_id} is checkable",),
                dependencies=dependencies,
                suggested_role=SubagentRole(role),
            )
            for task_id, role, dependencies in tasks
        ]
    )


def test_one_task_is_a_plan_that_buys_nothing() -> None:
    """Válido como grafo, y aun así trabajo para el bucle.

    `TaskGraph.build` ya dijo que es correcto, que es su pregunta. Si merece la pena
    ejecutarlo como grafo es otra, y la contesta la política.
    """
    verdict = DecompositionPolicy().assess_plan(_plan_of(("T01", "coder", ())))
    assert not verdict.decompose
    assert "one sequence for one specialist" in verdict.explanation


def test_a_chain_of_microtasks_is_a_to_do_list_not_a_graph() -> None:
    """El número de nodos no es la señal.

    Cinco tareas del mismo especialista, cada una esperando a la anterior, es la lista de
    pasos que el bucle ya recorre — con hand-offs añadidos y sin nada a cambio.
    """
    cadena = _plan_of(
        ("T01", "coder", ()),
        ("T02", "coder", ("T01",)),
        ("T03", "coder", ("T02",)),
        ("T04", "coder", ("T03",)),
        ("T05", "coder", ("T04",)),
    )
    verdict = DecompositionPolicy().assess_plan(cadena)
    assert not verdict.decompose
    assert "5 task(s)" in verdict.explanation


def test_work_that_can_happen_at_once_earns_the_graph() -> None:
    verdict = DecompositionPolicy().assess_plan(
        _plan_of(("T01", "coder", ()), ("T02", "coder", ()))
    )
    assert verdict.decompose
    assert "at the same time" in verdict.explanation


def test_a_chain_of_different_specialists_earns_it_too() -> None:
    """Sin concurrencia, pero con autoridades distintas.

    Un explorer que no puede escribir es una garantía, no una sugerencia, y el bucle no
    tiene forma de dársela a una parte del trabajo y no a otra.
    """
    verdict = DecompositionPolicy().assess_plan(
        _plan_of(("T01", "explorer", ()), ("T02", "coder", ("T01",)))
    )
    assert verdict.decompose
    assert "more than one specialist" in verdict.explanation


def test_a_dependency_two_steps_away_is_still_a_dependency() -> None:
    """Lo transitivo cuenta: si sólo se miraran las dependencias directas, una cadena
    de tres pasaría por concurrente porque la primera y la tercera no se nombran."""
    verdict = DecompositionPolicy().assess_plan(
        _plan_of(("T01", "coder", ()), ("T02", "coder", ("T01",)), ("T03", "coder", ("T02",)))
    )
    assert not verdict.decompose


# ------------------------------------------------------ los cuatro casos, de extremo a extremo


def _one_task_plan() -> str:
    return json.dumps(
        {
            "tasks": [
                {
                    "id": "solo",
                    "goal": "fix the addition",
                    "expected_output": "calc.add returns the sum",
                    "acceptance_criteria": ["the addition test passes"],
                    "suggested_role": "coder",
                }
            ]
        }
    )


def _shape_of(seen: list[RuntimeEvent]) -> JSONObject:
    decisions = [event for event in seen if event.name is EventName.PLAN_DECIDED]
    assert len(decisions) == 1, f"se anunció la forma {len(decisions)} veces"
    return decisions[0].payload


def test_auto_with_planning_off_runs_direct_and_says_why(tmp_path: Path) -> None:
    """Caso 1: la capa no está, `auto` degrada, y el motivo queda registrado."""

    async def scenario() -> None:
        workspace = _wide_sandbox(tmp_path / "wide")
        bus = InMemoryEventBus()
        seen: list[RuntimeEvent] = []
        bus.subscribe(seen.append)
        provider = _Scripted()
        registry = _registry(tmp_path, provider, bus, planning=False)
        run_id = await registry.start(
            "rework the parser across api and core", workspace, RunOptions(max_iterations=3)
        )
        try:
            await asyncio.wait_for(registry.wait(run_id), timeout=180)
        finally:
            await registry.shutdown()

        assert provider.planning_requests == 0, "degradar no debería costar una llamada"
        assert EventName.GRAPH_STARTED not in [event.name for event in seen]
        forma = _shape_of(seen)
        assert forma["executed_as"] == "direct"
        assert "planning switched off" in str(forma["reason"])
        # Y el veredicto de la política sigue siendo el suyo, que aquí dice lo contrario
        # de lo que se hizo. Informar sólo de uno de los dos dejaría a este run
        # explicándose con una conclusión sobre la que no actuó.
        assert "worth its overhead" in str(forma["policy_verdict"])

        # La forma se decide antes de que nadie pueda suscribirse, así que un cliente que
        # sólo escuchase el flujo no la vería nunca. El registro la guarda para el marco
        # de estado, que es lo que recibe cualquiera que se conecte, tarde o pronto.
        assert registry.shape_of(run_id) == forma

    asyncio.run(scenario())


def test_hierarchical_with_planning_off_is_refused_before_the_run_exists(
    tmp_path: Path,
) -> None:
    """Caso 2: exigirlo sin que exista es un 400, no un run que miente sobre su forma."""

    async def scenario() -> None:
        workspace = _sandbox(tmp_path / "repo")
        registry = _registry(tmp_path, _Scripted(), InMemoryEventBus(), planning=False)
        try:
            with pytest.raises(ToolValidationError, match="auto or direct"):
                await registry.start(
                    "investigate the addition path",
                    workspace,
                    RunOptions(execution_mode=ExecutionMode.HIERARCHICAL),
                )
            assert registry.live_ids() == (), "un run rechazado no debería quedar vivo"
        finally:
            await registry.shutdown()

    asyncio.run(scenario())


def test_auto_runs_a_single_task_plan_on_the_loop(tmp_path: Path) -> None:
    """Caso 3: el plan es válido y aun así se ejecuta directo.

    El plan se pidió porque las señales lo justificaban; lo que vino de vuelta no reparte
    trabajo. Ejecutarlo como grafo sería pagar los hand-offs por respeto al procedimiento.
    """

    async def scenario() -> None:
        workspace = _wide_sandbox(tmp_path / "wide")
        bus = InMemoryEventBus()
        seen: list[RuntimeEvent] = []
        bus.subscribe(seen.append)
        provider = _Scripted(plan=_one_task_plan())
        registry = _registry(tmp_path, provider, bus)
        run_id = await registry.start(
            "rework the parser across api and core", workspace, RunOptions(max_iterations=3)
        )
        try:
            await asyncio.wait_for(registry.wait(run_id), timeout=180)
        finally:
            await registry.shutdown()

        assert provider.planning_requests == 1, "auto sí debía intentar el plan"
        assert EventName.GRAPH_STARTED not in [event.name for event in seen]
        forma = _shape_of(seen)
        assert forma["executed_as"] == "direct"
        assert "one sequence for one specialist" in str(forma["reason"])

    asyncio.run(scenario())


def test_hierarchical_runs_a_single_task_plan_through_the_graph(tmp_path: Path) -> None:
    """Caso 4: el mismo plan, exigido como grafo, se ejecuta como grafo."""

    async def scenario() -> None:
        workspace = _sandbox(tmp_path / "repo")
        bus = InMemoryEventBus()
        seen: list[RuntimeEvent] = []
        bus.subscribe(seen.append)
        provider = _Scripted(plan=_one_task_plan())
        registry = _registry(tmp_path, provider, bus)
        run_id = await registry.start(
            "investigate the addition path",
            workspace,
            RunOptions(
                writes=CapabilityMode.ALLOW,
                execution=CapabilityMode.ALLOW,
                execution_mode=ExecutionMode.HIERARCHICAL,
            ),
        )
        try:
            await asyncio.wait_for(registry.wait(run_id), timeout=180)
        finally:
            await registry.shutdown()

        assert EventName.GRAPH_STARTED in [event.name for event in seen]
        started = [event for event in seen if event.name is EventName.TASK_STARTED]
        assert [event.payload["task_id"] for event in started] == ["solo"]
        assert _shape_of(seen)["executed_as"] == "hierarchical"

    asyncio.run(scenario())


def test_auto_runs_a_chain_of_microtasks_on_the_loop(tmp_path: Path) -> None:
    """Caso 5: varias tareas, ningún beneficio, y aun así el bucle.

    Es el caso que un recuento de nodos daría por bueno: hay cinco tareas. Ninguna puede
    empezar hasta que acabe la anterior y todas son para el mismo especialista.
    """

    async def scenario() -> None:
        workspace = _wide_sandbox(tmp_path / "wide")
        bus = InMemoryEventBus()
        seen: list[RuntimeEvent] = []
        bus.subscribe(seen.append)
        cadena = json.dumps(
            {
                "tasks": [
                    {
                        "id": f"T{index:02d}",
                        "goal": f"step {index}",
                        "expected_output": f"step {index} finished",
                        "acceptance_criteria": [f"step {index} is checkable"],
                        "dependencies": [] if index == 1 else [f"T{index - 1:02d}"],
                        "suggested_role": "coder",
                    }
                    for index in range(1, 6)
                ]
            }
        )
        provider = _Scripted(plan=cadena)
        registry = _registry(tmp_path, provider, bus)
        run_id = await registry.start(
            "rework the parser across api and core", workspace, RunOptions(max_iterations=3)
        )
        try:
            await asyncio.wait_for(registry.wait(run_id), timeout=180)
        finally:
            await registry.shutdown()

        assert provider.planning_requests == 1
        assert EventName.GRAPH_STARTED not in [event.name for event in seen]
        forma = _shape_of(seen)
        assert forma["executed_as"] == "direct"
        assert "5 task(s)" in str(forma["reason"])

    asyncio.run(scenario())
