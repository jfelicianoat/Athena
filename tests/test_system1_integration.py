from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from athena.adapters.service import RunOptions
from athena.adapters.service.runs.suscripcion import LiveRun
from athena.agent_loop import AgentRunStatus
from athena.cancellation import CancellationSource, CancellationToken
from athena.delegation import DelegateTaskTool
from athena.errors import PermissionDeniedError, ToolValidationError
from athena.events import EventName, InMemoryEventBus, RuntimeEvent
from athena.graph_executor import GraphExecutor
from athena.models import ModelToolCall
from athena.permissions import (
    PermissionPolicy,
    PermissionRequest,
    PolicyPermissionEngine,
    ReadOnlyPermissionEngine,
    RiskLevel,
    RiskTier,
)
from athena.planning import TaskGraph, TaskNode
from athena.process_tools import BashTool
from athena.registry import ToolRegistry
from athena.state import ExecutionOutcome
from athena.stores import InMemoryToolResultStore
from athena.subagents import SubagentBrief, SubagentBudget, SubagentResult, SubagentRole
from athena.system1 import System1, System1Config
from athena.tasks import TaskManager
from athena.tool_executor import ToolExecutor
from athena.tools import ToolContext, ToolResult, ToolSpec
from athena.types import JSONObject
from athena.workspace import Workspace
from athena_service import ServiceSettings, build_service
from test_system1 import FakeJudge, StaticVerification, accepted, proof
from test_system1_broker import broker


class RecordingDelegator:
    def __init__(self, *, partial: bool = False, sensitive: bool = False) -> None:
        self.roles: list[SubagentRole] = []
        self.partial = partial
        self.sensitive = sensitive

    async def delegate(
        self,
        role: SubagentRole,
        brief: SubagentBrief,
        workspace: Workspace,
        cancellation: CancellationToken,
        *,
        parent_session_id: str = "",
        budget: SubagentBudget | None = None,
    ) -> SubagentResult:
        self.roles.append(role)
        return SubagentResult(
            role,
            AgentRunStatus.COMPLETED,
            parent_session_id,
            answer="Partial output" if self.partial else "Complete output with regression test",
            files_modified=("bug.py", "test_regression.py") if role is SubagentRole.CODER else (),
            review_required=self.sensitive,
        )


@pytest.mark.parametrize(
    "case,expected",
    [
        ("good", False),
        ("partial", True),
        ("low_confidence", True),
        ("failed_tests", True),
        ("mandatory", True),
        ("sensitive", True),
        ("shadow", True),
        ("off", True),
    ],
)
def test_graph_gate_preserves_workflow_and_mandatory_reviews(
    tmp_path: Path,
    case: str,
    expected: bool,
) -> None:
    async def scenario() -> None:
        bus = InMemoryEventBus()
        events: list[RuntimeEvent] = []
        bus.subscribe(events.append)
        client = FakeJudge(accepted(case != "partial", 0.96 if case == "low_confidence" else 0.99))
        config = System1Config(reviewer_gate=case != "off", shadow_mode=case == "shadow")
        system = System1(client, config, bus)
        delegator = RecordingDelegator(partial=case == "partial", sensitive=case == "sensitive")
        manager = TaskManager()
        executor = GraphExecutor(
            delegator,
            manager,
            bus,
            system1=system,
            mandatory_review=case == "mandatory",
            goal_verification=StaticVerification(proof(passed=case != "failed_tests")),
            objective="User goal: fix bug and add regression test",
            acceptance_criteria=("user-required deliverable",),
        )
        graph = TaskGraph.build(
            [
                TaskNode(
                    "fix",
                    "Fix bug and add regression test",
                    "working code",
                    acceptance_criteria=("test added",),
                    suggested_role=SubagentRole.CODER,
                ),
                TaskNode(
                    "verify",
                    "Check the executor output",
                    "verified result",
                    acceptance_criteria=("all requirements met",),
                    dependencies=("fix",),
                    suggested_role=SubagentRole.VERIFIER,
                ),
            ]
        )
        result = await executor.execute(
            graph, Workspace.from_path(tmp_path), CancellationSource().token, run_id="run"
        )
        await manager.shutdown()
        assert (SubagentRole.VERIFIER in delegator.roles) is expected
        assert result.outcome is (
            ExecutionOutcome.FAILED if case == "failed_tests" else ExecutionOutcome.COMPLETED
        )
        if case != "off":
            audit = next(event for event in events if event.name is EventName.SYSTEM1_REVIEW)
            assert audit.payload["reviewer_skipped"] is not expected
        if case in {"sensitive", "mandatory", "failed_tests", "off"}:
            assert not client.requests
        else:
            assert (
                client.requests[0].input["objective"]
                == "User goal: fix bug and add regression test"
            )
            criteria = client.requests[0].input["acceptance_criteria"]
            assert isinstance(criteria, list)
            assert "user-required deliverable" in criteria

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "case,reviewer_runs",
    [
        ("good", False),
        ("partial", True),
        ("low_confidence", True),
        ("failed_tests", True),
        ("mandatory", True),
        ("explicit", True),
        ("shadow", True),
        ("off", True),
    ],
)
def test_direct_gate_runs_after_permissions_and_before_the_reviewer(
    tmp_path: Path,
    case: str,
    reviewer_runs: bool,
) -> None:
    async def scenario() -> None:
        bus = InMemoryEventBus()
        client = FakeJudge(accepted(case != "partial", 0.96 if case == "low_confidence" else 0.99))
        system = System1(
            client,
            System1Config(
                reviewer_gate=case != "off",
                shadow_mode=case == "shadow",
            ),
            bus,
        )
        delegator = RecordingDelegator()
        policy = PermissionPolicy(allow_local_execution=True)
        tool = DelegateTaskTool(
            delegator,
            {"bash": BashTool()},
            policy,
            system1=system,
            verification=StaticVerification(proof(passed=case != "failed_tests")),
        )
        executor = ToolExecutor(
            ToolRegistry((tool,)), PolicyPermissionEngine(policy), InMemoryToolResultStore(), bus
        )
        result = await executor.execute(
            ModelToolCall(
                "review",
                "delegate_task",
                {
                    "goal": "Check the executor output",
                    "role": "verifier",
                    "acceptance_criteria": ["regression test exists"],
                },
            ),
            session_id="r",
            workspace=Workspace.from_path(tmp_path),
            cancellation=CancellationSource().token,
            context_metadata={
                "system1_review": {
                    "objective": "Revisa el código" if case == "explicit" else "Fix the bug",
                    "output": "Fixed code and added regression test",
                    "state": {},
                    "mandatory_review": case == "mandatory",
                }
            },
        )
        assert (SubagentRole.VERIFIER in delegator.roles) is reviewer_runs
        assert isinstance(result.output, dict)
        assert result.output["status"] == "completed"
        if case in {"mandatory", "explicit", "failed_tests", "off"}:
            assert not client.requests

    asyncio.run(scenario())


def test_system1_cannot_bypass_a_denied_delegation(tmp_path: Path) -> None:
    async def scenario() -> None:
        bus = InMemoryEventBus()
        client = FakeJudge(accepted())
        system = System1(client, System1Config(reviewer_gate=True, shadow_mode=False), bus)
        delegator = RecordingDelegator()
        tool = DelegateTaskTool(
            delegator,
            {"bash": BashTool()},
            PermissionPolicy(allow_local_execution=True),
            system1=system,
            verification=StaticVerification(),
        )
        executor = ToolExecutor(
            ToolRegistry((tool,)), ReadOnlyPermissionEngine(), InMemoryToolResultStore(), bus
        )
        with pytest.raises(PermissionDeniedError):
            await executor.execute(
                ModelToolCall(
                    "review",
                    "delegate_task",
                    {
                        "goal": "Check output",
                        "role": "verifier",
                        "acceptance_criteria": ["done"],
                    },
                ),
                session_id="r",
                workspace=Workspace.from_path(tmp_path),
                cancellation=CancellationSource().token,
            )
        assert not client.requests and not delegator.roles

    asyncio.run(scenario())


def test_shadow_comparison_records_a_disagreement_without_changing_behavior(tmp_path: Path) -> None:
    class RejectingReviewer(RecordingDelegator):
        async def delegate(
            self,
            role: SubagentRole,
            brief: SubagentBrief,
            workspace: Workspace,
            cancellation: CancellationToken,
            *,
            parent_session_id: str = "",
            budget: SubagentBudget | None = None,
        ) -> SubagentResult:
            result = await super().delegate(
                role,
                brief,
                workspace,
                cancellation,
                parent_session_id=parent_session_id,
                budget=budget,
            )
            if role is SubagentRole.VERIFIER:
                from dataclasses import replace

                return replace(
                    result, answer='{"passed": false, "failures": ["missing requirement"]}'
                )
            return result

    async def scenario() -> None:
        bus = InMemoryEventBus()
        events: list[RuntimeEvent] = []
        bus.subscribe(events.append)
        system = System1(FakeJudge(accepted()), System1Config(reviewer_gate=True), bus)
        runner = RejectingReviewer()
        manager = TaskManager()
        executor = GraphExecutor(
            runner, manager, bus, system1=system, goal_verification=StaticVerification()
        )
        graph = TaskGraph.build(
            [
                TaskNode(
                    "fix",
                    "Fix bug",
                    "fix",
                    acceptance_criteria=("fixed",),
                    suggested_role=SubagentRole.CODER,
                ),
                TaskNode(
                    "check",
                    "Check output",
                    "checked",
                    acceptance_criteria=("done",),
                    suggested_role=SubagentRole.VERIFIER,
                    dependencies=("fix",),
                ),
            ]
        )
        await executor.execute(
            graph, Workspace.from_path(tmp_path), CancellationSource().token, run_id="r"
        )
        await manager.shutdown()
        comparison = next(event for event in events if event.name is EventName.SYSTEM1_COMPARISON)
        assert comparison.payload["disagreement"] is True
        assert SubagentRole.VERIFIER in runner.roles

    asyncio.run(scenario())


def test_service_wires_the_same_system1_and_verification_into_direct_delegation(
    tmp_path: Path,
) -> None:
    config = System1Config(goal_completion=True, context_filtering=True, reviewer_gate=True)
    service = build_service(
        ServiceSettings(
            broker_base_url="http://127.0.0.1:9",
            broker_token="fixture",
            service_token="fixture",
            state_dir=tmp_path / "state",
            system1=config,
        )
    )
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    options = RunOptions.from_json(
        {"acceptance_criteria": ["regression test"], "mandatory_review": True}
    )
    assert RunOptions.from_json(options.to_json()) == options
    workspace = Workspace.from_path(workspace_path)
    service.registry._runs["fixture"] = LiveRun("fixture", workspace, options, CancellationSource())
    loop = service.registry._build("fixture", workspace, options)
    assert loop.system1 is service.registry.system1
    assert loop.context_builder.system1 is loop.system1
    tool = loop.registry.get("delegate_task")
    assert isinstance(tool, DelegateTaskTool)
    assert tool.system1 is loop.system1 and tool.verification is loop.verification
    assert loop.config.acceptance_criteria == ("regression test",) and loop.config.mandatory_review


@pytest.mark.parametrize("enabled", [True, False])
def test_service_discovers_capabilities_only_when_a_feature_is_enabled(
    tmp_path: Path,
    enabled: bool,
) -> None:
    with broker({}) as (url, calls):

        async def scenario() -> None:
            service = build_service(
                ServiceSettings(
                    broker_base_url=url,
                    broker_token="fixture",
                    service_token="fixture",
                    state_dir=tmp_path,
                    port=0,
                    system1=System1Config(goal_completion=enabled),
                )
            )
            await service.start()
            await service.stop()

        asyncio.run(scenario())
        assert [call[0] for call in calls] == (["/api/v1/capabilities"] if enabled else [])


@pytest.mark.parametrize(
    "declared,judged",
    [
        # Agora-style objective: the word comes from a template, and the client says so.
        (False, True),
        # Nobody declared anything: the wording still counts as a request for review.
        (None, False),
        (True, False),
    ],
)
def test_a_declared_review_intent_overrides_the_objective_wording(
    tmp_path: Path, declared: bool | None, judged: bool
) -> None:
    async def scenario() -> None:
        bus = InMemoryEventBus()
        client = FakeJudge(accepted())
        system = System1(client, System1Config(reviewer_gate=True, shadow_mode=False), bus)
        delegator = RecordingDelegator()
        policy = PermissionPolicy(allow_local_execution=True)
        tool = DelegateTaskTool(
            delegator,
            {"bash": BashTool()},
            policy,
            system1=system,
            verification=StaticVerification(proof()),
        )
        executor = ToolExecutor(
            ToolRegistry((tool,)), PolicyPermissionEngine(policy), InMemoryToolResultStore(), bus
        )
        await executor.execute(
            ModelToolCall(
                "review",
                "delegate_task",
                {
                    "goal": "Check the executor output",
                    "role": "verifier",
                    "acceptance_criteria": ["regression test exists"],
                },
            ),
            session_id="r",
            workspace=Workspace.from_path(tmp_path),
            cancellation=CancellationSource().token,
            context_metadata={
                "system1_review": {
                    "objective": "# PROFILE\nReview your own output before finishing.\n"
                    "# CARD\nFix the bug",
                    "output": "Fixed code and added regression test",
                    "state": {},
                    "mandatory_review": bool(declared),
                },
                "system1_review_declared": declared,
            },
        )
        assert bool(client.requests) is judged
        assert (SubagentRole.VERIFIER in delegator.roles) is not judged

    asyncio.run(scenario())


def test_run_options_keep_review_declaration_three_valued() -> None:
    assert RunOptions.from_json({}).mandatory_review is None
    for value in (True, False):
        options = RunOptions.from_json({"mandatory_review": value})
        assert options.mandatory_review is value
        assert RunOptions.from_json(options.to_json()).mandatory_review is value
    assert RunOptions.from_json(RunOptions().to_json()).mandatory_review is None
    with pytest.raises(ToolValidationError):
        RunOptions.from_json({"mandatory_review": "false"})


class _LargeReadTool:
    spec = ToolSpec(
        name="large_read",
        description="Returns more text than the inline limit.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        output_schema={"type": "string"},
        risk=RiskLevel.LOW,
        max_result_size_chars=1_000,
    )

    def validate(self, arguments: JSONObject) -> JSONObject:
        return arguments

    def permission(self, context: ToolContext, arguments: JSONObject) -> PermissionRequest:
        return PermissionRequest(
            self.spec.name,
            self.spec.name,
            context.workspace,
            RiskLevel.LOW,
            RiskTier.R0_READ_ONLY,
            True,
            False,
            arguments=arguments,
        )

    async def execute(
        self, context: ToolContext, arguments: JSONObject, cancellation: CancellationToken
    ) -> ToolResult:
        return ToolResult("x" * 5_000)

    def is_read_only(self, arguments: JSONObject) -> bool:
        return True

    def is_destructive(self, arguments: JSONObject) -> bool:
        return False

    def is_concurrency_safe(self, arguments: JSONObject) -> bool:
        return True


def test_an_externalized_read_is_not_a_sensitive_operation(tmp_path: Path) -> None:
    async def scenario() -> None:
        events: list[RuntimeEvent] = []
        bus = InMemoryEventBus()
        bus.subscribe(events.append)
        executor = ToolExecutor(
            ToolRegistry((_LargeReadTool(),)),
            PolicyPermissionEngine(),
            InMemoryToolResultStore(),
            bus,
        )
        result = await executor.execute(
            ModelToolCall("read", "large_read", {}),
            session_id="r",
            workspace=Workspace.from_path(tmp_path),
            cancellation=CancellationSource().token,
        )
        assert result.reference is not None
        assert result.metadata.get("review_required") is not True
        completed = [event for event in events if event.name is EventName.TOOL_COMPLETED]
        assert completed[-1].payload["externalized"] is True
        assert completed[-1].payload["review_required"] is False

    asyncio.run(scenario())
