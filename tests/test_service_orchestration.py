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
from athena.adapters.service.orchestration import OrchestrationSettings, Orchestrator
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
from athena.planning import DecompositionSignals, PlanStatus, TaskGraph, TaskNode
from athena.session_store import SessionRecord, SqliteSessionStore
from athena.state import AgentStatus
from athena.stores import SqliteToolResultStore
from athena.subagents import SubagentRole
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


def test_planning_stays_off_however_loudly_it_is_asked_for(tmp_path: Path) -> None:
    """A client's preference cannot switch on a layer the deployment did not configure.

    The distinction matters because "the operator has not enabled planning" and "this goal
    does not need planning" are different answers, and only one of them is the client's to
    overrule.
    """
    workspace = _wide_sandbox(tmp_path / "wide")
    shape = _orchestrator(tmp_path, planning=False).decide(
        workspace, "rework the parser", STRONG, requested=True
    )
    assert not shape.hierarchical


def test_a_client_may_decline_the_graph_it_would_have_got(tmp_path: Path) -> None:
    workspace = _wide_sandbox(tmp_path / "wide")
    shape = _orchestrator(tmp_path, planning=True).decide(
        workspace, "rework the parser", STRONG, requested=False
    )
    assert not shape.hierarchical
    # The evidence is still reported: the run was not decomposed because it was declined,
    # which is not the same as the policy having said no.
    assert shape.decision.decompose


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
                hierarchical=True,
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
            "investigate the addition path", workspace, RunOptions(hierarchical=True)
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


def test_a_refused_plan_runs_directly_instead_of_failing_the_run(tmp_path: Path) -> None:
    """A plan that cannot be validated is a reason to work directly, not to give up.

    The alternative — failing the run because a model returned malformed JSON — would make
    the planning layer a new way for runs to die, which is a poor trade for a layer whose
    whole purpose is to make hard goals more likely to succeed.
    """

    async def scenario() -> None:
        workspace = _sandbox(tmp_path / "repo")
        bus = InMemoryEventBus()
        seen: list[RuntimeEvent] = []
        bus.subscribe(seen.append)
        provider = _Scripted(plan="not a plan at all")
        registry = _registry(tmp_path, provider, bus)
        run_id = await registry.start(
            "investigate the addition path",
            workspace,
            RunOptions(hierarchical=True, max_iterations=3),
        )
        try:
            result = await asyncio.wait_for(registry.wait(run_id), timeout=180)
        finally:
            await registry.shutdown()

        assert provider.planning_requests == 1
        assert result.status.value in ("completed", "failed")
        recovery = [
            event
            for event in seen
            if event.name is EventName.RECOVERY_ACTION
            and event.payload.get("action") == "run_directly"
        ]
        assert recovery, "the fall back to the loop was never announced"
        assert EventName.GRAPH_STARTED not in [event.name for event in seen]

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
            RunOptions(writes=CapabilityMode.ASK, hierarchical=True),
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
