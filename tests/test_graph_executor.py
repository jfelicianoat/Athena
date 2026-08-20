"""The executor: three libraries becoming one runtime.

What matters here is not that a graph runs. It is that it runs through the machinery that
already existed — one `AgentLoop`, one `TaskManager`, one `PermissionEngine` — and that the
rules the report insists on hold under it: readers overlap, writers do not, a task passing
is not the goal passing, and a stop reaches the bottom.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from athena.cancellation import CancellationScope, CancellationSource, CancellationToken
from athena.events import EventName, InMemoryEventBus, ModelEvent, RuntimeEvent
from athena.git_tools import git_read_tools
from athena.graph_executor import GraphExecutor, TaskEvidence
from athena.models import (
    ModelCapabilities,
    ModelHealth,
    ModelHealthStatus,
    ModelProvider,
    ModelRequest,
    ModelResponse,
)
from athena.mutation_tools import workspace_mutation_tools
from athena.planning import PlanStatus, TaskGraph, TaskNode
from athena.process_tools import BashTool
from athena.repository_tools import repository_read_tools
from athena.state import ExecutionOutcome, SessionState
from athena.stores import InMemoryToolResultStore
from athena.subagents import SubagentRole, SubagentRunner
from athena.tasks import TaskManager
from athena.tools import Tool
from athena.verification import (
    VerificationEvidence,
    VerificationResult,
    VerificationStatus,
)
from athena.workspace import Workspace


def node(
    task_id: str,
    *,
    depends: Sequence[str] = (),
    role: SubagentRole = SubagentRole.EXPLORER,
    output: str | None = None,
) -> TaskNode:
    return TaskNode(
        id=task_id,
        goal=f"do {task_id}",
        expected_output=output or f"{task_id} exists",
        acceptance_criteria=("it can be checked",),
        dependencies=tuple(depends),
        suggested_role=role,
    )


class _AnsweringProvider(ModelProvider):
    """Answers immediately, and records how many calls overlapped.

    The overlap counter is the point: it is how the read/write rule is observed rather than
    asserted. A provider that simply returned would prove nothing about concurrency.
    """

    def __init__(self, *, delay: float = 0.02, answer: str = "done") -> None:
        self.delay = delay
        self.answer = answer
        self.in_flight = 0
        self.peak_in_flight = 0
        self.calls = 0
        self.objectives: list[str] = []

    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        cancellation.raise_if_cancelled()
        self.calls += 1
        self.objectives.append(request.messages[-1].content)
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.in_flight -= 1
        cancellation.raise_if_cancelled()
        return ModelResponse(self.answer, "scripted", "stop")

    async def stream(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> AsyncIterator[ModelEvent]:
        del request, cancellation
        if False:
            yield ModelEvent(EventName.MODEL_COMPLETED, "never")

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(False, True, False)

    async def health(self, cancellation: CancellationToken) -> ModelHealth:
        cancellation.raise_if_cancelled()
        return ModelHealth(ModelHealthStatus.HEALTHY)


class _StubbornProvider(_AnsweringProvider):
    """Never answers, so a cancellation has something real to interrupt."""

    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        del request
        self.calls += 1
        while True:
            cancellation.raise_if_cancelled()
            await asyncio.sleep(0.01)


class _FixedVerification:
    """A verification policy with a predetermined mind, so a test can state the case."""

    def __init__(self, status: VerificationStatus) -> None:
        self.status = status
        self.calls = 0

    async def verify(
        self, state: SessionState, workspace: Workspace, cancellation: CancellationToken
    ) -> VerificationResult:
        del state, workspace
        cancellation.raise_if_cancelled()
        self.calls += 1
        return VerificationResult(
            status=self.status,
            summary=f"stubbed {self.status.value}",
            evidence=(VerificationEvidence("stub", "a stated result"),),
        )


def _catalog(workspace: Workspace) -> dict[str, Tool]:
    """Everything the three profiles ask for.

    A profile refuses to run with a partial toolset rather than quietly doing less, so a
    catalog missing one git tool fails the task before the model is ever called.
    """
    del workspace
    tools: list[Tool] = [*repository_read_tools(), *git_read_tools(), *workspace_mutation_tools()]
    tools.append(BashTool())
    return {tool.spec.name: tool for tool in tools}


def _executor(
    workspace: Workspace,
    provider: ModelProvider,
    bus: InMemoryEventBus,
    **kwargs: object,
) -> tuple[GraphExecutor, TaskManager]:
    manager = TaskManager()
    runner = SubagentRunner(provider, _catalog(workspace), bus, InMemoryToolResultStore())
    executor = GraphExecutor(runner, manager, bus, **kwargs)  # type: ignore[arg-type]
    return executor, manager


def _workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    return Workspace.from_path(root)


# ------------------------------------------------------------------------ it actually runs


def test_a_graph_runs_through_the_loop_that_already_existed(tmp_path: Path) -> None:
    """No second agent loop. The delegate is an `AgentLoop`, built where it always was."""

    async def scenario() -> None:
        workspace = _workspace(tmp_path)
        bus = InMemoryEventBus()
        provider = _AnsweringProvider()
        executor, manager = _executor(workspace, provider, bus)
        graph = TaskGraph.build([node("survey"), node("report", depends=["survey"])])

        result = await executor.execute(
            graph, workspace, CancellationSource(CancellationScope.RUN).token, run_id="r1"
        )
        await manager.shutdown()

        assert result.outcome is ExecutionOutcome.COMPLETED
        assert provider.calls >= 2, "each task went to the model through its own loop"
        assert {item.task_id for item in result.evidence} == {"survey", "report"}
        assert all(item.succeeded for item in result.evidence)

    asyncio.run(scenario())


def test_dependencies_decide_the_order(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = _workspace(tmp_path)
        bus = InMemoryEventBus()
        provider = _AnsweringProvider()
        executor, manager = _executor(workspace, provider, bus)
        graph = TaskGraph.build(
            [node("first"), node("second", depends=["first"]), node("third", depends=["second"])]
        )

        result = await executor.execute(graph, workspace, CancellationSource().token)
        await manager.shutdown()

        order = [item.task_id for item in result.evidence]
        assert order == ["first", "second", "third"]

    asyncio.run(scenario())


def test_fan_out_lets_independent_tasks_overlap(tmp_path: Path) -> None:
    """Reads run together. This is the parallelism the graph existed to expose."""

    async def scenario() -> None:
        workspace = _workspace(tmp_path)
        bus = InMemoryEventBus()
        provider = _AnsweringProvider(delay=0.08)
        executor, manager = _executor(workspace, provider, bus)
        graph = TaskGraph.build(
            [
                node("survey"),
                node("a", depends=["survey"], output="a"),
                node("b", depends=["survey"], output="b"),
                node("c", depends=["survey"], output="c"),
            ]
        )

        await executor.execute(graph, workspace, CancellationSource().token)
        await manager.shutdown()

        assert provider.peak_in_flight > 1, "three readers should not have queued up"

    asyncio.run(scenario())


def test_writers_never_overlap(tmp_path: Path) -> None:
    """Two coders editing one repository is the failure that is painful to reproduce.

    Until worktrees exist the lock is the answer, and this is what says the lock is real.
    """

    async def scenario() -> None:
        workspace = _workspace(tmp_path)
        bus = InMemoryEventBus()
        provider = _AnsweringProvider(delay=0.05)
        executor, manager = _executor(workspace, provider, bus)
        graph = TaskGraph.build(
            [
                node("survey"),
                node("edit_a", depends=["survey"], role=SubagentRole.CODER, output="a"),
                node("edit_b", depends=["survey"], role=SubagentRole.CODER, output="b"),
            ]
        )

        await executor.execute(graph, workspace, CancellationSource().token)
        await manager.shutdown()

        assert provider.peak_in_flight == 1, "writers must be serialised"

    asyncio.run(scenario())


def test_fan_in_waits_for_every_dependency(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = _workspace(tmp_path)
        bus = InMemoryEventBus()
        executor, manager = _executor(workspace, _AnsweringProvider(), bus)
        graph = TaskGraph.build(
            [
                node("a", output="a"),
                node("b", output="b"),
                node("check", depends=["a", "b"], role=SubagentRole.VERIFIER),
            ]
        )

        result = await executor.execute(graph, workspace, CancellationSource().token)
        await manager.shutdown()

        order = [item.task_id for item in result.evidence]
        assert order[-1] == "check"
        assert graph.get("check").status is PlanStatus.COMPLETED

    asyncio.run(scenario())


# --------------------------------------------------------------------------- verification


def test_a_task_passing_is_not_the_goal_passing(tmp_path: Path) -> None:
    """The claim the whole layer would otherwise be quietly making.

    Every part reporting success and the whole not working is exactly the case goal
    verification exists to catch, and a runtime that summed local claims would miss it.
    """

    async def scenario() -> None:
        workspace = _workspace(tmp_path)
        bus = InMemoryEventBus()
        goal = _FixedVerification(VerificationStatus.FAILED)
        executor, manager = _executor(workspace, _AnsweringProvider(), bus, goal_verification=goal)
        graph = TaskGraph.build([node("a", output="a"), node("b", output="b")])

        result = await executor.execute(graph, workspace, CancellationSource().token)
        await manager.shutdown()

        assert all(item.succeeded for item in result.evidence), "every task passed"
        assert result.outcome is ExecutionOutcome.FAILED, "and the goal did not"
        assert goal.calls == 1, "the goal is proved once, over the whole result"

    asyncio.run(scenario())


def test_the_goal_verification_is_what_grants_completion(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = _workspace(tmp_path)
        bus = InMemoryEventBus()
        goal = _FixedVerification(VerificationStatus.PASSED)
        executor, manager = _executor(workspace, _AnsweringProvider(), bus, goal_verification=goal)
        graph = TaskGraph.build([node("a")])

        result = await executor.execute(graph, workspace, CancellationSource().token)
        await manager.shutdown()

        assert result.outcome is ExecutionOutcome.COMPLETED
        assert result.goal_verification is not None
        assert result.goal_verification.permits_completion

    asyncio.run(scenario())


def test_an_inconclusive_goal_does_not_complete(tmp_path: Path) -> None:
    # A run that proved nothing is not a run that succeeded, which is ADR-012 applied to
    # the graph rather than to a single loop.
    async def scenario() -> None:
        workspace = _workspace(tmp_path)
        bus = InMemoryEventBus()
        goal = _FixedVerification(VerificationStatus.INCONCLUSIVE)
        executor, manager = _executor(workspace, _AnsweringProvider(), bus, goal_verification=goal)
        graph = TaskGraph.build([node("a")])

        result = await executor.execute(graph, workspace, CancellationSource().token)
        await manager.shutdown()

        assert result.outcome is ExecutionOutcome.FAILED

    asyncio.run(scenario())


# ------------------------------------------------------------------------------ evidence


def test_the_parent_receives_evidence_and_not_a_transcript(tmp_path: Path) -> None:
    """What keeps the parent's context from growing with every delegate it uses."""

    async def scenario() -> None:
        workspace = _workspace(tmp_path)
        bus = InMemoryEventBus()
        provider = _AnsweringProvider(answer="I looked at things and here is what I found")
        executor, manager = _executor(workspace, provider, bus)
        graph = TaskGraph.build([node("survey")])

        result = await executor.execute(graph, workspace, CancellationSource().token)
        await manager.shutdown()

        item = result.evidence_for("survey")
        assert item is not None
        assert isinstance(item, TaskEvidence)
        assert item.role is SubagentRole.EXPLORER
        # A fixed set of fields, none of which is the child's conversation.
        assert set(item.to_json()) == {
            "task_id",
            "role",
            "outcome",
            "summary",
            "files_changed",
            "commands_run",
            "facts",
            "risks",
            "unresolved",
            "verification",
            "error_code",
        }

    asyncio.run(scenario())


def test_a_task_is_told_what_its_dependencies_found_and_nothing_else(tmp_path: Path) -> None:
    # The brief carries upstream summaries. It does not carry the graph, the parent's
    # conversation, or the other branch's work — a task that needed all of that would not
    # have been worth separating.
    async def scenario() -> None:
        workspace = _workspace(tmp_path)
        bus = InMemoryEventBus()
        provider = _AnsweringProvider(answer="the login handler is in auth.py")
        executor, manager = _executor(workspace, provider, bus)
        graph = TaskGraph.build([node("survey"), node("fix", depends=["survey"])])

        await executor.execute(graph, workspace, CancellationSource().token)
        await manager.shutdown()

        downstream = provider.objectives[-1]
        assert "survey" in downstream, "the dependency's finding was passed on"
        assert "do fix" in downstream, "along with its own goal"

    asyncio.run(scenario())


# ------------------------------------------------------------------------ failure handling


def test_a_failed_task_blocks_what_was_waiting_on_it(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = _workspace(tmp_path)
        bus = InMemoryEventBus()
        provider = _StubbornProvider()
        executor, manager = _executor(workspace, provider, bus)
        graph = TaskGraph.build([node("a"), node("b", depends=["a"])])
        source = CancellationSource(CancellationScope.RUN)

        async def stop_soon() -> None:
            await asyncio.sleep(0.2)
            source.cancel()

        stopper = asyncio.ensure_future(stop_soon())
        result = await executor.execute(graph, workspace, source.token)
        await stopper
        await manager.shutdown()

        assert result.outcome.is_stopped_deliberately
        assert graph.get("b").status in (PlanStatus.PENDING, PlanStatus.BLOCKED)
        assert graph.get("b").attempts == 0, "downstream work was never started"

    asyncio.run(scenario())


def test_a_graph_that_cannot_proceed_does_not_report_success(tmp_path: Path) -> None:
    """Declaring victory over work nobody did is the failure mode worth ruling out."""

    async def scenario() -> None:
        workspace = _workspace(tmp_path)
        bus = InMemoryEventBus()
        executor, manager = _executor(workspace, _AnsweringProvider(), bus)
        graph = TaskGraph.build([node("a"), node("b", depends=["a"])])
        # Fail `a` by hand, so the frontier is empty while `b` is still unfinished.
        graph.transition("a", PlanStatus.READY)
        graph.transition("a", PlanStatus.RUNNING)
        graph.transition("a", PlanStatus.FAILED)

        result = await executor.execute(graph, workspace, CancellationSource().token)
        await manager.shutdown()

        assert result.outcome is ExecutionOutcome.FAILED
        assert not graph.is_complete()

    asyncio.run(scenario())


def test_cancelling_the_run_reaches_the_model_call(tmp_path: Path) -> None:
    """The bottom of the chain from the report: run → task → subagent → loop → provider."""

    async def scenario() -> None:
        workspace = _workspace(tmp_path)
        bus = InMemoryEventBus()
        provider = _StubbornProvider()
        executor, manager = _executor(workspace, provider, bus)
        graph = TaskGraph.build([node("a")])
        source = CancellationSource(CancellationScope.RUN)

        async def stop_soon() -> None:
            while provider.calls == 0:
                await asyncio.sleep(0.01)
            source.cancel()

        stopper = asyncio.ensure_future(stop_soon())
        result = await executor.execute(graph, workspace, source.token)
        await stopper
        await manager.shutdown()

        assert result.outcome.is_stopped_deliberately
        assert manager.list() and all(record.terminal for record in manager.list())

    asyncio.run(scenario())


# -------------------------------------------------------------------------------- events


def test_a_watcher_can_follow_the_graph_from_events(tmp_path: Path) -> None:
    # The UI and the channels both consume events, so a graph that ran silently would be a
    # graph nobody could watch.
    async def scenario() -> None:
        workspace = _workspace(tmp_path)
        bus = InMemoryEventBus()
        seen: list[RuntimeEvent] = []
        bus.subscribe(seen.append)
        executor, manager = _executor(workspace, _AnsweringProvider(), bus)
        graph = TaskGraph.build([node("a", output="a"), node("b", output="b")])

        await executor.execute(graph, workspace, CancellationSource().token, run_id="r1")
        await manager.shutdown()

        names = [event.name for event in seen]
        assert EventName.SUBAGENT_STARTED in names, "the runner still publishes its own"
        assert EventName.GRAPH_STARTED in names
        assert names.count(EventName.TASK_STARTED) == 2
        assert names.count(EventName.TASK_COMPLETED) == 2
        assert EventName.GRAPH_COMPLETED in names
        correlated = {
            event.correlation_id for event in seen if event.name is EventName.TASK_COMPLETED
        }
        assert correlated == {"a", "b"}, "each task's events name their task"

    asyncio.run(scenario())


def test_the_executor_holds_no_agent_logic() -> None:
    """It joins things up. It does not decide anything they would have decided."""
    import ast

    import athena

    module = Path(athena.__file__).parent / "graph_executor.py"
    source = module.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(module))
    imported = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("athena")
    }

    assert "athena.agent_loop" not in imported, "it uses SubagentRunner's loop, not its own"
    assert "athena.adapters.openai_compatible" not in imported
    assert "athena.subagents" in imported
    assert "athena.tasks" in imported
    assert "class AgentLoop" not in source
