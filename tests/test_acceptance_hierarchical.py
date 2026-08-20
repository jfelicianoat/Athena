"""V0.2 acceptance: the whole hierarchy, end to end, on one workspace.

Every other suite tests a layer. This one tests that the layers are the same system —
which is the claim the whole integration effort was making, and the one that unit tests
cannot make on its own.

The scenario is the report's: analyse a repository, decompose the work, delegate
investigation, implement, verify, fail, diagnose, repair, verify again, and prove the goal
against the project's real checks. It runs on a real git repository with a real subprocess
running real pytest, because the failures worth catching here are the ones a fake would
agree with.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from athena.cancellation import CancellationScope, CancellationSource, CancellationToken
from athena.checkpoints import CheckpointStore
from athena.delegation import confine, narrow
from athena.diagnosis import FailureKind, diagnose_result
from athena.events import EventName, InMemoryEventBus, ModelEvent, RuntimeEvent
from athena.git_tools import git_read_tools
from athena.graph_executor import GraphExecutor
from athena.metrics import MetricsCollector, aggregate
from athena.models import (
    ModelCapabilities,
    ModelHealth,
    ModelHealthStatus,
    ModelProvider,
    ModelRequest,
    ModelResponse,
)
from athena.mutation_tools import workspace_mutation_tools
from athena.permissions import PermissionPolicy
from athena.planning import (
    DecompositionPolicy,
    DecompositionSignals,
    PlanBoard,
    PlanStatus,
    TaskGraph,
    TaskNode,
    describe_plan,
    parse_plan,
)
from athena.process_tools import BashTool
from athena.project_memory import MemoryKind, SqliteProjectMemory, VerificationState
from athena.provider_router import ProviderEntry, ProviderRegistry, ProviderRouter
from athena.repository_tools import repository_read_tools
from athena.rollback import RollbackLedger, RollbackScope
from athena.state import ExecutionOutcome
from athena.stores import InMemoryToolResultStore
from athena.subagents import DEFAULT_PROFILES, SubagentRole, SubagentRunner
from athena.tasks import TaskManager
from athena.tools import Tool
from athena.verification import (
    CommandVerificationPolicy,
    VerificationPlanner,
    VerificationStatus,
)
from athena.workspace import Workspace

BROKEN = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
TEST = "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"


def _git(root: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=root, check=True, capture_output=True, timeout=60)


def _repository(tmp_path: Path, *, source: str = BROKEN) -> Workspace:
    """A repository with a real failing test and a real verification command."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "calc.py").write_text(source, encoding="utf-8")
    (root / "test_calc.py").write_text(TEST, encoding="utf-8")
    command = f'"{sys.executable}" -m pytest -q'
    (root / "AGENTS.md").write_text(
        f"# Sandbox\n\n## Verification\n\n```\n{command}\n```\n", encoding="utf-8"
    )
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "athena@example.invalid")
    _git(root, "config", "user.name", "Athena Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    return Workspace.from_path(root)


def _catalog() -> dict[str, Tool]:
    tools: list[Tool] = [
        *repository_read_tools(),
        *git_read_tools(),
        *workspace_mutation_tools(),
        BashTool(),
    ]
    return {tool.spec.name: tool for tool in tools}


class _Scripted(ModelProvider):
    """A model with a plan of its own, so the run is deterministic.

    The point of the acceptance suite is the runtime's behaviour, not the model's. A real
    model would make these tests measure whether a small LLM had a good day.
    """

    def __init__(self, answers: Sequence[str] | None = None, *, fixes: Path | None = None) -> None:
        self._answers = list(answers or [])
        self._fixes = fixes
        self.calls = 0
        self.roles_seen: list[str] = []

    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        cancellation.raise_if_cancelled()
        self.calls += 1
        prompt = "\n".join(message.content for message in request.messages)
        for role in ("explorer", "coder", "verifier"):
            if f"Athena's {role}" in prompt:
                self.roles_seen.append(role)
        # A coder that is asked to fix the bug fixes it, once, on disk. That is what makes
        # the verification that follows a real verification.
        if self._fixes is not None and "coder" in prompt.lower():
            self._fixes.write_text(FIXED, encoding="utf-8")
        if self._answers:
            return ModelResponse(self._answers.pop(0), "scripted", "stop")
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


def _verification(workspace: Workspace) -> CommandVerificationPolicy:
    """The project's real checks, discovered the way a run discovers them.

    Built the same way `RunRegistry` builds it, so a mismatch between the acceptance suite
    and the runtime would show up here rather than being hidden behind a stub.
    """
    return CommandVerificationPolicy(VerificationPlanner(workspace))


def _executor(
    workspace: Workspace,
    provider: ModelProvider,
    bus: InMemoryEventBus,
    **kwargs: object,
) -> tuple[GraphExecutor, TaskManager]:
    manager = TaskManager()
    runner = SubagentRunner(provider, _catalog(), bus, InMemoryToolResultStore())
    return GraphExecutor(runner, manager, bus, **kwargs), manager  # type: ignore[arg-type]


def _plan() -> TaskGraph:
    return TaskGraph.build(
        [
            TaskNode(
                id="T01",
                goal="find why the addition test fails",
                expected_output="the failing function named",
                acceptance_criteria=("names a file and a function",),
                suggested_role=SubagentRole.EXPLORER,
            ),
            TaskNode(
                id="T02",
                goal="fix the failing function",
                expected_output="calc.add returns the sum",
                acceptance_criteria=("the addition test passes",),
                dependencies=("T01",),
                suggested_role=SubagentRole.CODER,
            ),
        ]
    )


# ------------------------------------------------------------------ the whole flow


def test_a_complex_goal_is_planned_delegated_executed_and_proved(tmp_path: Path) -> None:
    """The scenario from the report, on a repository where the test really fails.

    Every step is a different subsystem, and the only thing this asserts is that they add
    up to one system: the plan runs, the specialists are the ones the plan asked for, the
    fix lands on disk, and the goal is proved by the project's own checks rather than by
    anybody saying so.
    """

    async def scenario() -> None:
        workspace = _repository(tmp_path)
        bus = InMemoryEventBus()
        metrics = MetricsCollector()
        bus.subscribe(metrics.observe)
        board = PlanBoard()
        provider = _Scripted(fixes=workspace.root / "calc.py")
        policy = _verification(workspace)
        executor, manager = _executor(
            workspace, provider, bus, goal_verification=policy, board=board
        )
        graph = _plan()

        try:
            result = await executor.execute(
                graph, workspace, CancellationSource(CancellationScope.RUN).token, run_id="acc"
            )
        finally:
            await manager.shutdown()

        # The plan ran, in order, through the specialists it named.
        assert [item.task_id for item in result.evidence] == ["T01", "T02"]
        assert provider.roles_seen[:2] == ["explorer", "coder"]
        assert graph.get("T01").status is PlanStatus.COMPLETED
        assert graph.get("T02").status is PlanStatus.COMPLETED

        # The fix is on disk, and the project's own checks are what say so.
        assert (workspace.root / "calc.py").read_text() == FIXED
        assert result.goal_verification is not None
        assert result.goal_verification.status is VerificationStatus.PASSED
        assert result.outcome is ExecutionOutcome.COMPLETED

        # And the run is legible afterwards, from three different angles.
        assert board.plan_for("acc") is not None
        counted = metrics.get("acc")
        assert counted is not None
        assert counted.hierarchical
        assert counted.tasks_completed == 2

    asyncio.run(scenario())


def test_the_goal_is_not_granted_by_the_tasks_agreeing(tmp_path: Path) -> None:
    """The claim the layer would otherwise be quietly making.

    Every task reports success and the repository is still broken. Nothing about a sum of
    local claims implies the global property.
    """

    async def scenario() -> None:
        workspace = _repository(tmp_path)
        bus = InMemoryEventBus()
        policy = _verification(workspace)
        # A provider that never fixes anything: the tasks finish, the tests do not pass.
        executor, manager = _executor(workspace, _Scripted(), bus, goal_verification=policy)

        try:
            result = await executor.execute(_plan(), workspace, CancellationSource().token)
        finally:
            await manager.shutdown()

        assert all(item.succeeded for item in result.evidence), "every task said it was done"
        assert result.outcome is ExecutionOutcome.FAILED
        assert result.goal_verification is not None
        assert result.goal_verification.status is VerificationStatus.FAILED

    asyncio.run(scenario())


def test_a_simple_goal_never_reaches_the_graph(tmp_path: Path) -> None:
    """The path most goals take, and the one an integration effort most easily breaks."""
    del tmp_path
    decision = DecompositionPolicy().assess(
        DecompositionSignals(independently_verifiable_outputs=1, subsystems_touched=1)
    )

    assert not decision.decompose
    assert "AgentLoop" in decision.explanation


# ------------------------------------------------------- the failure path, in one piece


def test_a_failing_verification_is_diagnosed_before_anyone_is_asked_to_fix_it(
    tmp_path: Path,
) -> None:
    """Diagnosis, and the repair it directs, on real pytest output.

    The runtime reads the failure, decides what kind it is, and says what not to touch —
    which is the difference between a directed repair cycle and a second guess.
    """

    async def scenario() -> None:
        workspace = _repository(tmp_path)
        policy = _verification(workspace)
        from athena.state import AgentStatus, SessionState

        state = SessionState(session_id="acc", workspace_id=workspace.workspace_id)
        del AgentStatus

        result = await policy.verify(state, workspace, CancellationSource().token)
        diagnosis = diagnose_result(result)

        assert result.status is VerificationStatus.FAILED
        assert diagnosis.kind is FailureKind.CODE_ERROR
        assert diagnosis.is_worth_repairing
        assert "Do not change the test" in diagnosis.guidance
        assert "assert" in diagnosis.excerpt.lower()

    asyncio.run(scenario())


def test_a_broken_environment_is_not_blamed_on_the_change(tmp_path: Path) -> None:
    """The distinction that stops a run fixing somebody else's problem.

    A missing package leaves the same hole in the evidence as a bug, and reading it as a
    bug sends the model editing an import that was correct.
    """
    from athena.diagnosis import InconclusiveReason, diagnose, inconclusive_reason
    from athena.verification import (
        CheckKind,
        CheckOutcome,
        VerificationEvidence,
        VerificationResult,
    )

    del tmp_path
    failed = VerificationResult(
        status=VerificationStatus.FAILED,
        evidence=(VerificationEvidence("check", "failed"),),
        summary="failed",
    )
    outcome = CheckOutcome(
        name="tests",
        kind=CheckKind.TEST,
        command="pytest",
        passed=False,
        exit_code=1,
        duration_seconds=1.0,
        output_tail="ModuleNotFoundError: No module named 'requests'",
    )

    diagnosis = diagnose(failed, [outcome])

    assert diagnosis.kind is FailureKind.DEPENDENCY_ERROR
    assert not diagnosis.is_worth_repairing
    assert inconclusive_reason(diagnosis) is InconclusiveReason.DEPENDENCY_MISSING


# ------------------------------------------------------------------------ the invariants


def test_a_child_can_never_do_more_than_its_parent(tmp_path: Path) -> None:
    """Checked here as well as in isolation, because this is where it would be lost.

    An integration that assembled the pieces slightly wrong would produce a runtime where
    each unit test still passes and the guarantee no longer holds.
    """
    del tmp_path
    read_only = PermissionPolicy()

    profile = confine(DEFAULT_PROFILES[SubagentRole.CODER], read_only, frozenset(_catalog()))

    assert not profile.policy.allow_workspace_writes
    assert not profile.policy.allow_local_execution
    assert not narrow(read_only, profile.policy).allow_workspace_writes


def test_stopping_a_run_reaches_every_level(tmp_path: Path) -> None:
    """Run to graph to task to subagent to loop to provider, in one call."""

    async def scenario() -> None:
        workspace = _repository(tmp_path)
        bus = InMemoryEventBus()

        class _Stubborn(_Scripted):
            async def complete(
                self, request: ModelRequest, cancellation: CancellationToken
            ) -> ModelResponse:
                del request
                self.calls += 1
                while True:
                    cancellation.raise_if_cancelled()
                    await asyncio.sleep(0.01)

        provider = _Stubborn()
        executor, manager = _executor(workspace, provider, bus)
        source = CancellationSource(CancellationScope.RUN)

        async def stop_when_busy() -> None:
            while provider.calls == 0:
                await asyncio.sleep(0.01)
            source.cancel()

        stopper = asyncio.ensure_future(stop_when_busy())
        try:
            result = await executor.execute(_plan(), workspace, source.token)
        finally:
            await stopper
            await manager.shutdown()

        assert result.outcome.is_stopped_deliberately
        assert result.outcome is not ExecutionOutcome.FAILED, "stopped is not broken"
        assert all(record.terminal for record in manager.list()), "no orphans"

    asyncio.run(scenario())


def test_a_model_plan_with_a_cycle_never_becomes_a_run(tmp_path: Path) -> None:
    """The model proposes; the runtime disposes. There is no unchecked path to a graph."""
    del tmp_path
    from athena.planning import CyclicPlanError

    document = (
        '{"tasks": ['
        '{"id": "a", "goal": "one", "expected_output": "x", '
        '"acceptance_criteria": ["c"], "dependencies": ["b"]},'
        '{"id": "b", "goal": "two", "expected_output": "y", '
        '"acceptance_criteria": ["c"], "dependencies": ["a"]}]}'
    )

    with pytest.raises(CyclicPlanError):
        parse_plan(document)


def test_a_run_can_be_undone_without_touching_a_person_s_work(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = _repository(tmp_path)
        (workspace.root / "notes.md").write_text("a person wrote this\n", encoding="utf-8")
        ledger = RollbackLedger(CheckpointStore(tmp_path / "checkpoints"))
        await ledger.checkpoint("T02", workspace, ["calc.py", "notes.md"])
        ledger.record_written("T02", ["calc.py"])
        (workspace.root / "calc.py").write_text("a bad fix\n", encoding="utf-8")
        (workspace.root / "notes.md").write_text("edited meanwhile\n", encoding="utf-8")

        result = await ledger.roll_back(workspace, scope=RollbackScope.RUN)

        assert result.restored == ("calc.py",)
        assert result.protected == ("notes.md",)
        assert (workspace.root / "calc.py").read_text() == BROKEN
        assert "edited meanwhile" in (workspace.root / "notes.md").read_text()

    asyncio.run(scenario())


# -------------------------------------------------------------------- what it can show


def test_a_run_leaves_enough_behind_to_be_compared(tmp_path: Path) -> None:
    """The measurement the architecture has to be able to make about itself."""

    async def scenario() -> None:
        workspace = _repository(tmp_path)
        bus = InMemoryEventBus()
        metrics = MetricsCollector()
        bus.subscribe(metrics.observe)
        policy = _verification(workspace)
        executor, manager = _executor(
            workspace,
            _Scripted(fixes=workspace.root / "calc.py"),
            bus,
            goal_verification=policy,
        )

        try:
            await executor.execute(
                _plan(), workspace, CancellationSource().token, run_id="graph-run"
            )
        finally:
            await manager.shutdown()

        await bus.publish(RuntimeEvent(EventName.AGENT_STARTED, "flat-run"))
        await bus.publish(RuntimeEvent(EventName.AGENT_COMPLETED, "flat-run"))

        hierarchical = [run for run in metrics.all() if run.hierarchical]
        flat = [run for run in metrics.all() if not run.hierarchical]

        assert aggregate(hierarchical).runs == 1
        assert aggregate(flat).runs >= 1
        assert aggregate(hierarchical).subagent_usage == 1.0

    asyncio.run(scenario())


def test_the_plan_reads_as_a_plan_to_a_person(tmp_path: Path) -> None:
    del tmp_path
    rendered = describe_plan(_plan())

    assert "0 de 2" in rendered
    assert "explorer" in rendered
    assert "T02" in rendered


def test_what_a_run_learns_stays_a_proposal_until_something_checks_it(
    tmp_path: Path,
) -> None:
    """Memory closes the loop, and closes it carefully.

    A run that wrote its own conclusions in as facts would be a runtime that gets more
    confident every session regardless of whether it was right.
    """

    async def scenario() -> None:
        memory = SqliteProjectMemory(tmp_path / "memory.db")

        learned = await memory.propose(
            "acc",
            MemoryKind.VERIFIED_COMMAND,
            f'the checks run with "{sys.executable}" -m pytest -q',
            source="run:acc",
        )

        assert learned.verification_state is VerificationState.PROPOSED
        confirmed = await memory.approve(learned.id, state=VerificationState.VERIFIED)
        assert confirmed.verification_state is VerificationState.VERIFIED
        found = await memory.search("acc", "pytest", minimum_state=VerificationState.VERIFIED)
        assert [item.id for item in found] == [learned.id]

    asyncio.run(scenario())


def test_a_dead_provider_falls_through_to_the_next_one(tmp_path: Path) -> None:
    """The directive that had no consumer, exercised through the port the loop uses."""
    del tmp_path
    from athena.errors import ModelPermanentError

    class _Dead(_Scripted):
        async def complete(
            self, request: ModelRequest, cancellation: CancellationToken
        ) -> ModelResponse:
            del request, cancellation
            self.calls += 1
            raise ModelPermanentError("the endpoint is gone")

    async def scenario() -> None:
        dead = _Dead()
        alive = _Scripted(["second"])
        router = ProviderRouter(
            ProviderRegistry(ProviderEntry("primary", dead), [ProviderEntry("backup", alive)])
        )

        response = await router.complete(ModelRequest(messages=()), CancellationSource().token)

        assert response.content == "second"
        assert isinstance(router, ModelProvider), "the loop still talks to the port"

    asyncio.run(scenario())
