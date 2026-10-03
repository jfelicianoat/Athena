"""Reproducible control-flow measurements; judgments are scripted, not model evaluations.

Run: python tests/test_system1_benchmark.py --output docs/system1_benchmark.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from athena.agent_loop import AgentLoop, AgentLoopConfig, AgentRunStatus
from athena.cancellation import CancellationSource, CancellationToken
from athena.context import ContextBuilder
from athena.events import InMemoryEventBus
from athena.graph_executor import GraphExecutor
from athena.metrics import MetricsCollector
from athena.models import ModelResponse, ModelToolCall
from athena.mutation_tools import workspace_mutation_tools
from athena.permissions import PermissionPolicy, PolicyPermissionEngine
from athena.planning import TaskGraph, TaskNode
from athena.process_tools import BashTool
from athena.registry import ToolRegistry
from athena.state import ExecutionOutcome
from athena.stores import InMemoryToolResultStore
from athena.subagents import SubagentRole
from athena.system1 import Judgment, JudgmentRequest, System1, System1Config
from athena.tasks import TaskManager
from athena.testing import FakeModelProvider
from athena.tool_executor import ToolExecutor
from athena.types import JSONObject, JSONValue
from athena.workspace import Workspace
from test_system1 import FakeJudge, StaticVerification, accepted
from test_system1_integration import RecordingDelegator


def _summary(
    provider: FakeModelProvider,
    collector: MetricsCollector,
    run_id: str,
) -> JSONObject:
    metrics = collector.get(run_id)
    assert metrics is not None
    return {
        "iterations": len(provider.requests),
        "context_tokens_estimate": sum(
            (len(message.content) + 3) // 4
            for request in provider.requests
            for message in request.messages
        ),
        "reviewer_calls": 0,
        "fallbacks": metrics.system1.get("fallbacks", 0),
        "errors": 0,
    }


async def _goal(root: Path, *, enabled: bool) -> JSONObject:
    class EvidenceJudge(FakeJudge):
        async def judge(
            self,
            request: JudgmentRequest,
            cancellation: CancellationToken,
        ) -> Judgment:
            state = request.input["state"]
            assert isinstance(state, dict)
            files = state["files_modified"]
            assert isinstance(files, list)
            return accepted("test_regression.py" in files)

    root.mkdir()
    (root / "test_current.py").write_text(
        "from bug import answer\ndef test_current(): assert answer() == 42\n",
        encoding="utf-8",
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
                    {"path": "bug.py", "content": "def answer(): return 42\n"},
                ),
            ),
        ),
        ModelResponse(
            "",
            "fake",
            "tool_calls",
            tool_calls=(
                ModelToolCall(
                    "check1",
                    "bash",
                    {"command": "python -m pytest -q"},
                ),
            ),
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
            tool_calls=(
                ModelToolCall(
                    "check2",
                    "bash",
                    {"command": "python -m pytest -q"},
                ),
            ),
        ),
        ModelResponse("Fixed and regression-tested", "fake", "stop"),
    ]
    bus = InMemoryEventBus()
    collector = MetricsCollector()
    bus.subscribe(collector.observe)
    system = System1(
        EvidenceJudge(), System1Config(goal_completion=enabled, shadow_mode=False), bus
    )
    workspace = Workspace.from_path(root)
    registry = ToolRegistry((*workspace_mutation_tools(), BashTool()))
    provider = FakeModelProvider(responses)
    loop = AgentLoop(
        provider,
        registry,
        ToolExecutor(
            registry,
            PolicyPermissionEngine(
                PermissionPolicy(allow_workspace_writes=True, allow_local_execution=True),
            ),
            InMemoryToolResultStore(),
            bus,
        ),
        ContextBuilder(workspace),
        bus,
        system1=system,
        verification=StaticVerification(),
        config=AgentLoopConfig(
            capture_baseline=False,
            acceptance_criteria=("Fix bug", "Add regression test"),
        ),
    )
    result = await loop.run(
        "Corrige el bug y añade una prueba de regresión",
        workspace,
        CancellationSource().token,
        session_id="goal",
    )
    assert result.status is AgentRunStatus.COMPLETED
    assert (root / "test_regression.py").exists()
    return _summary(provider, collector, "goal")


async def _context(root: Path, *, enabled: bool, unavailable: bool = False) -> JSONObject:
    root.mkdir(exist_ok=True)
    bus = InMemoryEventBus()
    collector = MetricsCollector()
    bus.subscribe(collector.observe)
    client = (
        FakeJudge(Judgment(reason_code="SYSTEM1_UNAVAILABLE"))
        if unavailable
        else FakeJudge(
            accepted(1),
            accepted(0),
            accepted(2),
        )
    )
    system = System1(client, System1Config(context_filtering=enabled, shadow_mode=False), bus)
    workspace = Workspace.from_path(root)
    builder = ContextBuilder(
        workspace,
        system1=system,
        notes=(
            "What Athena remembers:\n"
            "- [constraint, confirmed by the user] PermissionEngine must remain authoritative.\n"
            "- [fact, unverified] Previous Agora implementation: preserve broker fallback.\n"
            "- [fact, unverified] Trading indicators: " + "unrelated market details. " * 80 + "\n"
            "- [fact, unverified] Athena has one AgentLoop and a graph executor.\n"
        ),
    )
    provider = FakeModelProvider(
        [ModelResponse("Use the Broker and preserve permissions", "fake", "stop")]
    )
    registry = ToolRegistry(())
    loop = AgentLoop(
        provider,
        registry,
        ToolExecutor(registry, PolicyPermissionEngine(), InMemoryToolResultStore(), bus),
        builder,
        bus,
        verification=StaticVerification(),
    )
    result = await loop.run(
        "Haz lo mismo con Athena", workspace, CancellationSource().token, session_id="context"
    )
    assert result.status is AgentRunStatus.COMPLETED
    context = provider.requests[0].messages[0].content
    assert "Previous Agora implementation" in context and "PermissionEngine" in context
    assert ("Trading indicators" in context) is (not enabled or unavailable)
    return _summary(provider, collector, "context")


async def _reviewer(root: Path, *, enabled: bool) -> JSONObject:
    root.mkdir(exist_ok=True)
    bus = InMemoryEventBus()
    collector = MetricsCollector()
    bus.subscribe(collector.observe)
    system = System1(
        FakeJudge(accepted()), System1Config(reviewer_gate=enabled, shadow_mode=False), bus
    )
    runner = RecordingDelegator()
    manager = TaskManager()
    executor = GraphExecutor(
        runner, manager, bus, system1=system, goal_verification=StaticVerification()
    )
    graph = TaskGraph.build(
        [
            TaskNode(
                "fix",
                "Fix bug",
                "code",
                acceptance_criteria=("regression test added",),
                suggested_role=SubagentRole.CODER,
            ),
            TaskNode(
                "verify",
                "Check output",
                "checked",
                acceptance_criteria=("all criteria",),
                suggested_role=SubagentRole.VERIFIER,
                dependencies=("fix",),
            ),
        ]
    )
    result = await executor.execute(
        graph, Workspace.from_path(root), CancellationSource().token, run_id="reviewer"
    )
    await manager.shutdown()
    assert result.outcome is ExecutionOutcome.COMPLETED
    summary = _summary(FakeModelProvider(()), collector, "reviewer")
    summary["reviewer_calls"] = runner.roles.count(SubagentRole.VERIFIER)
    return summary


async def measure(root: Path) -> JSONObject:
    rows: list[JSONValue] = [
        {
            "scenario": "goal_completion",
            "before": await _goal(root / "before", enabled=False),
            "after": await _goal(root / "after", enabled=True),
        },
        {
            "scenario": "optional_context",
            "before": await _context(root / "context", enabled=False),
            "after": await _context(root / "context", enabled=True),
        },
        {
            "scenario": "reviewer_gate",
            "before": await _reviewer(root / "review", enabled=False),
            "after": await _reviewer(root / "review", enabled=True),
        },
        {
            "scenario": "broker_unavailable",
            "before": await _context(root / "fallback", enabled=False),
            "after": await _context(root / "fallback", enabled=True, unavailable=True),
        },
    ]
    return {
        "method": "scripted judgments; actual AgentLoop, context, graph and tool execution",
        "tokens": "ceil(chars/4) across all messages sent to the main model; not billed tokens",
        "rows": rows,
    }


def test_benchmark_demonstrates_savings_and_safe_fallback(tmp_path: Path) -> None:
    report = asyncio.run(measure(tmp_path))
    rows = report["rows"]
    assert isinstance(rows, list)
    goal, context, reviewer, fallback = rows
    assert isinstance(goal, dict) and isinstance(context, dict)
    assert isinstance(reviewer, dict) and isinstance(fallback, dict)
    assert isinstance(goal["before"], dict) and isinstance(goal["after"], dict)
    assert goal["before"]["iterations"] == 5 and goal["after"]["iterations"] == 4
    assert isinstance(context["before"], dict) and isinstance(context["after"], dict)
    after_tokens = context["after"]["context_tokens_estimate"]
    before_tokens = context["before"]["context_tokens_estimate"]
    assert isinstance(after_tokens, int) and isinstance(before_tokens, int)
    assert after_tokens < before_tokens
    assert isinstance(reviewer["before"], dict) and isinstance(reviewer["after"], dict)
    assert reviewer["before"]["reviewer_calls"] == 1 and reviewer["after"]["reviewer_calls"] == 0
    assert isinstance(fallback["after"], dict)
    assert fallback["after"]["fallbacks"] == 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    workspace_root = Path.cwd().resolve()
    with tempfile.TemporaryDirectory(prefix=".system1-benchmark-", dir=workspace_root) as directory:
        benchmark_root = Path(directory).resolve()
        assert benchmark_root.is_relative_to(workspace_root)
        report = asyncio.run(measure(benchmark_root))
        options.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
