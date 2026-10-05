from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from athena.agent_loop import AgentLoop, AgentLoopConfig, AgentRunStatus
from athena.cancellation import CancellationSource, CancellationToken
from athena.context import ContextBuilder
from athena.errors import CancellationError, ModelTransientError
from athena.events import EventName, InMemoryEventBus, RuntimeEvent
from athena.goals import GoalBoard
from athena.metrics import MetricsCollector, SqliteMetricsStore
from athena.models import ModelMessage, ModelResponse, ModelRole, ModelToolCall
from athena.mutation_tools import workspace_mutation_tools
from athena.permissions import PermissionPolicy, PolicyPermissionEngine
from athena.process_tools import BashTool
from athena.registry import ToolRegistry
from athena.state import SessionState
from athena.stores import InMemoryToolResultStore
from athena.system1 import (
    Judgment,
    JudgmentRequest,
    System1,
    System1Config,
    deterministic_ready,
)
from athena.testing import FakeModelProvider
from athena.tool_executor import ToolExecutor
from athena.verification import VerificationEvidence, VerificationResult, VerificationStatus
from athena.workspace import Workspace


class FakeJudge:
    def __init__(self, *results: Judgment | Exception) -> None:
        self.results = list(results)
        self.requests: list[JudgmentRequest] = []

    async def judge(self, request: JudgmentRequest, cancellation: CancellationToken) -> Judgment:
        cancellation.raise_if_cancelled()
        self.requests.append(request)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def accepted(decision: bool | int = True, confidence: float = 0.99) -> Judgment:
    return Judgment(True, decision, confidence, "ollama_system1", "nimble", fallback_used=False)


def proof(*, passed: bool = True) -> VerificationResult:
    return VerificationResult(
        VerificationStatus.PASSED if passed else VerificationStatus.FAILED,
        (VerificationEvidence("tests", "Relevant suite", metadata={"passed": passed}),),
        "Project checks pass" if passed else "Project checks fail",
    )


class StaticVerification:
    def __init__(self, result: VerificationResult | None = None) -> None:
        self.result = result or proof()
        self.calls = 0

    async def verify(
        self,
        state: SessionState,
        workspace: Workspace,
        cancellation: CancellationToken,
    ) -> VerificationResult:
        cancellation.raise_if_cancelled()
        self.calls += 1
        return self.result


@pytest.mark.parametrize(
    "result,expected",
    [
        (accepted(), True),
        (accepted(False), False),
        (accepted(confidence=0.96), None),
        (Judgment(reason_code="LOW_CONFIDENCE"), None),
        (ModelTransientError("offline"), None),
        (accepted(confidence=float("nan")), None),
    ],
)
def test_completion_contract_and_fallback(
    result: Judgment | Exception, expected: bool | None
) -> None:
    async def scenario() -> None:
        client = FakeJudge(result)
        system = System1(
            client, System1Config(goal_completion=True, shadow_mode=False), InMemoryEventBus()
        )
        assert (
            await system.completed(
                {"objective": "fix"}, CancellationSource().token, session_id="run"
            )
            is expected
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("shadow,enabled", [(True, True), (False, False)])
def test_goal_shadow_and_off_preserve_behavior(shadow: bool, enabled: bool) -> None:
    async def scenario() -> None:
        client = FakeJudge(accepted())
        system = System1(
            client, System1Config(goal_completion=enabled, shadow_mode=shadow), InMemoryEventBus()
        )
        assert (
            await system.completed(
                {"objective": "fix"}, CancellationSource().token, session_id="run"
            )
            is None
        )
        assert len(client.requests) == int(enabled)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "judgment,mandatory,checks,expected",
    [
        (accepted(), False, proof(), True),
        (accepted(False), False, proof(), False),
        (accepted(confidence=0.96), False, proof(), False),
        (accepted(), True, proof(), False),
        (accepted(), False, proof(passed=False), False),
        (accepted(), False, None, False),
        (ModelTransientError("offline"), False, proof(), False),
        (Judgment(True, True, 0.99, "laya_mcp", fallback_used=True), False, proof(), True),
    ],
)
def test_reviewer_conservative_policy(
    judgment: Judgment | Exception,
    mandatory: bool,
    checks: VerificationResult | None,
    expected: bool,
) -> None:
    async def scenario() -> None:
        bus = InMemoryEventBus()
        events: list[RuntimeEvent] = []
        bus.subscribe(events.append)
        client = FakeJudge(judgment)
        system = System1(client, System1Config(reviewer_gate=True, shadow_mode=False), bus)
        assert (
            await system.skip_reviewer(
                {"objective": "fix", "output": "done"},
                CancellationSource().token,
                session_id="run",
                verification=checks,
                mandatory=mandatory,
            )
            is expected
        )
        audit = next(event for event in events if event.name is EventName.SYSTEM1_REVIEW)
        assert audit.payload["reviewer_skipped"] is expected
        assert "output" not in audit.payload
        if mandatory or not deterministic_ready(checks):
            assert not client.requests

    asyncio.run(scenario())


def test_review_shadow_calibration_and_off() -> None:
    async def scenario() -> None:
        for config in (
            System1Config(reviewer_gate=True),
            System1Config(),
            System1Config(reviewer_gate=True, shadow_mode=False, require_calibrated_review=True),
        ):
            system = System1(FakeJudge(accepted()), config, InMemoryEventBus())
            assert not await system.skip_reviewer(
                {"output": "good"},
                CancellationSource().token,
                session_id="r",
                verification=proof(),
            )

    asyncio.run(scenario())


def test_optional_context_and_implicit_reference_under_budget() -> None:
    async def scenario() -> None:
        bus = InMemoryEventBus()
        events: list[RuntimeEvent] = []
        bus.subscribe(events.append)
        client = FakeJudge(accepted(1), accepted(0), accepted(2))
        system = System1(
            client,
            System1Config(
                context_filtering=True,
                shadow_mode=False,
                context_budget_chars=1,
            ),
            bus,
        )
        candidates = (
            "mandatory safety",
            "previous Agora implementation",
            "irrelevant trading signal",
            "Athena architecture",
        )
        selected = await system.filter_context(
            "haz lo mismo con Athena",
            candidates,
            CancellationSource().token,
            session_id="run",
            mandatory=frozenset({0}),
        )
        assert selected == (candidates[0], candidates[1], candidates[3])
        event = next(event for event in events if event.name is EventName.SYSTEM1_CONTEXT)
        after, before = (
            event.payload["estimated_tokens_after"],
            event.payload["estimated_tokens_before"],
        )
        assert isinstance(after, int) and isinstance(before, int)
        assert after < before
        assert event.payload["excluded"] == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "result",
    [
        ModelTransientError("offline"),
        Judgment(reason_code="INVALID_OUTPUT"),
        accepted(0, 0.80),
        accepted(True),
    ],
)
def test_context_failure_preserves_previous_assembly(result: Judgment | Exception) -> None:
    async def scenario() -> None:
        system = System1(
            FakeJudge(result),
            System1Config(context_filtering=True, shadow_mode=False),
            InMemoryEventBus(),
        )
        candidates = ("previous notes", "more context")
        assert (
            await system.filter_context(
                "work",
                candidates,
                CancellationSource().token,
                session_id="r",
            )
            == candidates
        )

    asyncio.run(scenario())


def test_context_builder_protects_instructions_state_and_tools(tmp_path: Path) -> None:
    async def scenario() -> None:
        (tmp_path / "AGENTS.md").write_text("Safety instructions", encoding="utf-8")
        system = System1(
            FakeJudge(accepted(0)),
            System1Config(
                context_filtering=True,
                shadow_mode=False,
            ),
            InMemoryEventBus(),
        )
        builder = ContextBuilder(
            Workspace.from_path(tmp_path), notes="irrelevant trading notes", system1=system
        )
        history = (ModelMessage(ModelRole.TOOL, "unresolved error", tool_call_id="tool"),)
        request = await builder.build_request(
            objective="fix Athena",
            history=history,
            important_state={"acceptance_criteria": ["regression test"]},
            tool_definitions=(),
            cancellation=CancellationSource().token,
            session_id="r",
        )
        assert "Safety instructions" in request.messages[0].content
        assert "regression test" in request.messages[0].content
        assert "trading" not in request.messages[0].content
        assert request.messages[1].content == "fix Athena"
        assert request.messages[-1] == history[0]

    asyncio.run(scenario())


def test_cancellation_and_timeout() -> None:
    class WaitingJudge:
        async def judge(
            self, request: JudgmentRequest, cancellation: CancellationToken
        ) -> Judgment:
            await asyncio.Event().wait()
            return accepted()

    async def scenario() -> None:
        system = System1(
            WaitingJudge(),
            System1Config(
                goal_completion=True,
                shadow_mode=False,
                timeout_seconds=0.01,
            ),
            InMemoryEventBus(),
        )
        assert (
            await system.completed({"objective": "x"}, CancellationSource().token, session_id="r")
            is None
        )
        source = CancellationSource()
        source.cancel()
        with pytest.raises(CancellationError):
            await system.completed({"objective": "x"}, source.token, session_id="r")

    asyncio.run(scenario())


def test_missing_regression_test_does_not_close_and_complete_work_does(tmp_path: Path) -> None:
    class EvidenceJudge(FakeJudge):
        async def judge(
            self, request: JudgmentRequest, cancellation: CancellationToken
        ) -> Judgment:
            self.requests.append(request)
            state = request.input["state"]
            assert isinstance(state, dict)
            files = state["files_modified"]
            assert isinstance(files, list)
            return accepted("test_regression.py" in files)

    async def scenario() -> None:
        bus = InMemoryEventBus()
        client = EvidenceJudge()
        system = System1(client, System1Config(goal_completion=True, shadow_mode=False), bus)
        workspace = Workspace.from_path(tmp_path)
        (tmp_path / "test_current.py").write_text(
            "from bug import answer\ndef test_current(): assert answer() == 42\n",
            encoding="utf-8",
        )
        registry = ToolRegistry((*workspace_mutation_tools(), BashTool()))
        executor = ToolExecutor(
            registry,
            PolicyPermissionEngine(
                PermissionPolicy(
                    allow_workspace_writes=True,
                    allow_local_execution=True,
                )
            ),
            InMemoryToolResultStore(),
            bus,
        )
        responses = [
            ModelResponse(
                "",
                "fake",
                "tool_calls",
                tool_calls=(
                    ModelToolCall(
                        "fix",
                        "write_file",
                        {
                            "path": "bug.py",
                            "content": "def answer(): return 42\n",
                        },
                    ),
                ),
            ),
            ModelResponse(
                "",
                "fake",
                "tool_calls",
                tool_calls=(ModelToolCall("check1", "bash", {"command": "python -m pytest -q"}),),
            ),
            ModelResponse(
                "",
                "fake",
                "tool_calls",
                tool_calls=(
                    ModelToolCall(
                        "test",
                        "write_file",
                        {
                            "path": "test_regression.py",
                            "content": "from bug import answer\n"
                            "def test_fixed(): assert answer() == 42\n",
                        },
                    ),
                ),
            ),
            ModelResponse(
                "",
                "fake",
                "tool_calls",
                tool_calls=(ModelToolCall("check2", "bash", {"command": "python -m pytest -q"}),),
            ),
        ]
        provider = FakeModelProvider(responses)
        loop = AgentLoop(
            provider,
            registry,
            executor,
            ContextBuilder(workspace),
            bus,
            verification=StaticVerification(),
            system1=system,
            config=AgentLoopConfig(
                capture_baseline=False,
                max_iterations=5,
                acceptance_criteria=("Fix bug", "Add regression test"),
            ),
        )
        result = await loop.run(
            "Corrige el bug y añade una prueba de regresión", workspace, CancellationSource().token
        )
        assert result.status is AgentRunStatus.COMPLETED, (
            result.error,
            client.requests,
            provider.requests[-1].messages,
        )
        assert len(provider.requests) == 4
        assert len(client.requests) == 2
        assert client.requests[0].input["acceptance_criteria"] == ["Fix bug", "Add regression test"]
        assert (tmp_path / "test_regression.py").exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("enabled,shadow", [(False, False), (True, True)])
def test_terminal_flow_off_and_shadow(tmp_path: Path, enabled: bool, shadow: bool) -> None:
    async def scenario() -> None:
        bus = InMemoryEventBus()
        workspace = Workspace.from_path(tmp_path)
        registry = ToolRegistry(())
        provider = FakeModelProvider([ModelResponse("done", "fake", "stop")])
        client = FakeJudge(accepted(False))
        system = System1(client, System1Config(goal_completion=enabled, shadow_mode=shadow), bus)
        loop = AgentLoop(
            provider,
            registry,
            ToolExecutor(
                registry, PolicyPermissionEngine(PermissionPolicy()), InMemoryToolResultStore(), bus
            ),
            ContextBuilder(workspace),
            bus,
            system1=system,
            verification=StaticVerification(),
        )
        result = await loop.run("fix", workspace, CancellationSource().token)
        assert result.status is AgentRunStatus.COMPLETED
        assert len(client.requests) == int(enabled)

    asyncio.run(scenario())


def test_metrics_survive_restart_without_inputs(tmp_path: Path) -> None:
    async def scenario() -> None:
        bus = InMemoryEventBus()
        collector = MetricsCollector()
        bus.subscribe(collector.observe)
        system = System1(
            FakeJudge(accepted()),
            System1Config(
                reviewer_gate=True,
                shadow_mode=False,
            ),
            bus,
        )
        await system.skip_reviewer(
            {"secret_input": "private"},
            CancellationSource().token,
            session_id="run",
            verification=proof(),
        )
        metrics = collector.get("run")
        assert metrics is not None
        store = SqliteMetricsStore(tmp_path / "metrics.db")
        await store.save(metrics)
        reread = await SqliteMetricsStore(tmp_path / "metrics.db").load()
        assert reread[0].system1 == {
            "judgments": 1,
            "reviewable_outputs": 1,
            "reviewers_skipped": 1,
        }
        assert "private" not in json.dumps(reread[0].to_json())

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "settings",
    [
        {"ATHENA_SYSTEM1_GOAL_THRESHOLD": "nan"},
        {"ATHENA_SYSTEM1_TIMEOUT_SECONDS": "inf"},
        {"ATHENA_SYSTEM1_MAX_CANDIDATES": "0"},
        {"ATHENA_SYSTEM1_REVIEWER_GATE": "perhaps"},
    ],
)
def test_configuration_rejects_invalid_values(settings: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        System1Config.from_environment(settings)


def test_environment_flags_and_thresholds() -> None:
    config = System1Config.from_environment(
        {
            "ATHENA_SYSTEM1_GOAL_COMPLETION": "1",
            "ATHENA_SYSTEM1_CONTEXT_FILTERING": "1",
            "ATHENA_SYSTEM1_REVIEWER_GATE": "1",
            "ATHENA_SYSTEM1_SHADOW_MODE": "0",
            "ATHENA_SYSTEM1_GOAL_THRESHOLD": "0.98",
        }
    )
    assert config == replace(
        System1Config(),
        goal_completion=True,
        context_filtering=True,
        reviewer_gate=True,
        shadow_mode=False,
        goal_threshold=0.98,
    )


def test_completion_of_a_revised_objective_requires_another_iteration(tmp_path: Path) -> None:
    async def scenario() -> None:
        goal = GoalBoard("Original objective")

        class RevisingJudge(FakeJudge):
            async def judge(
                self,
                request: JudgmentRequest,
                cancellation: CancellationToken,
            ) -> Judgment:
                self.requests.append(request)
                if len(self.requests) == 1:
                    goal.revise("Updated objective", base_revision=1)
                return accepted()

        bus = InMemoryEventBus()
        workspace = Workspace.from_path(tmp_path)
        registry = ToolRegistry(())
        provider = FakeModelProvider(
            [
                ModelResponse("Original work done", "fake", "stop"),
                ModelResponse("Updated work done", "fake", "stop"),
            ]
        )
        client = RevisingJudge()
        loop = AgentLoop(
            provider,
            registry,
            ToolExecutor(registry, PolicyPermissionEngine(), InMemoryToolResultStore(), bus),
            ContextBuilder(workspace),
            bus,
            verification=StaticVerification(),
            system1=System1(client, System1Config(goal_completion=True, shadow_mode=False), bus),
        )
        result = await loop.run(
            "Original objective", workspace, CancellationSource().token, goal=goal
        )
        assert result.status is AgentRunStatus.COMPLETED
        assert result.answer == "Updated work done"
        assert len(provider.requests) == 2
        assert client.requests[-1].input["objective"] == "Updated objective"

    asyncio.run(scenario())


def test_an_accepted_negative_completion_requires_repair(tmp_path: Path) -> None:
    async def scenario() -> None:
        bus = InMemoryEventBus()
        registry = ToolRegistry(())
        workspace = Workspace.from_path(tmp_path)
        client = FakeJudge(accepted(False), accepted(False))
        provider = FakeModelProvider([ModelResponse("done", "fake", "stop") for _ in range(2)])
        loop = AgentLoop(
            provider,
            registry,
            ToolExecutor(registry, PolicyPermissionEngine(), InMemoryToolResultStore(), bus),
            ContextBuilder(workspace),
            bus,
            verification=StaticVerification(),
            system1=System1(client, System1Config(goal_completion=True, shadow_mode=False), bus),
            config=AgentLoopConfig(max_repair_cycles=1),
        )
        result = await loop.run(
            "Fix and add regression test", workspace, CancellationSource().token
        )
        assert result.status is AgentRunStatus.FAILED
        assert result.error is not None and result.error.code == "verification_failure"
        assert len(provider.requests) == 2
        assert "semantic completion checkpoint" in provider.requests[-1].messages[-1].content

    asyncio.run(scenario())


def test_memory_filter_protects_complete_user_confirmed_items_and_limits_calls(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        client = FakeJudge(accepted(0))
        system = System1(
            client,
            System1Config(context_filtering=True, shadow_mode=False, max_candidates=1),
            InMemoryEventBus(),
        )
        builder = ContextBuilder(
            Workspace.from_path(tmp_path),
            system1=system,
            notes=(
                "What Athena remembers:\n"
                "- [fact, confirmed by the user] Required module\n  its continuation\n"
                "- [fact, unverified] Unrelated trading\n  its irrelevant continuation\n"
                "- [fact, unverified] Unscored note\n"
            ),
        )
        for _ in range(2):
            request = await builder.build_request(
                objective="Athena",
                history=(),
                important_state={},
                tool_definitions=(),
                cancellation=CancellationSource().token,
            )
            context = request.messages[0].content
            assert "its continuation" in context and "Unscored note" in context
            assert "trading" not in context and "irrelevant continuation" not in context
        assert len(client.requests) == 1

    asyncio.run(scenario())


def test_contract_uses_operator_profiles_and_supports_custom_default_requests() -> None:
    async def scenario() -> None:
        client = FakeJudge(accepted(), accepted(), accepted(2))
        system = System1(
            client,
            System1Config(
                goal_completion=True, reviewer_gate=True, context_filtering=True, shadow_mode=False
            ),
            InMemoryEventBus(),
        )
        token = CancellationSource().token
        await system.completed({"goal": "all parts"}, token, session_id="r")
        await system.skip_reviewer(
            {"goal": "all parts"}, token, session_id="r", verification=proof()
        )
        await system.filter_context("goal", ("context",), token, session_id="r")
        # Athena's own use cases, each with the operator's profile for that decision: its
        # metrics stay apart from Agora's without lowering any threshold.
        assert [
            (request.use_case, request.to_json().get("threshold_profile"))
            for request in client.requests
        ] == [
            ("athena_goal_completion", "goal_completion"),
            ("athena_reviewer_gate", "agora_review_gate"),
            ("athena_context_ranking", "ranking"),
        ]
        custom = JudgmentRequest("custom_case", {"goal": "x"}, "Done?", threshold_profile="default")
        assert custom.to_json()["threshold_profile"] == "default"

    asyncio.run(scenario())


def test_threshold_profiles_come_from_the_environment_and_never_default_for_the_gate() -> None:
    config = System1Config.from_environment(
        {
            "ATHENA_SYSTEM1_REVIEWER_USE_CASE": "athena_gate",
            "ATHENA_SYSTEM1_REVIEWER_THRESHOLD_PROFILE": "athena_gate_profile",
            "ATHENA_SYSTEM1_GOAL_USE_CASE": "goal_completion",
            "ATHENA_SYSTEM1_GOAL_THRESHOLD_PROFILE": "",
        }
    )
    assert config.reviewer_threshold_profile == "athena_gate_profile"
    # Empty sends no profile: the use case has one of its own at the operator.
    assert config.goal_threshold_profile is None
    assert config.context_threshold_profile == "ranking"
    request = JudgmentRequest(config.goal_use_case, {"goal": "x"}, "Done?")
    assert "threshold_profile" not in request.to_json()
    # The live injection probe scores 0.90: the default threshold (0.85) would pass it.
    with pytest.raises(ValueError):
        System1Config.from_environment({"ATHENA_SYSTEM1_REVIEWER_THRESHOLD_PROFILE": "default"})
    with pytest.raises(ValueError):
        System1Config(goal_threshold_profile="not valid")
