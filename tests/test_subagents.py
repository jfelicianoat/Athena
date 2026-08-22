from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from athena.agent_loop import AgentRunStatus
from athena.cancellation import CancellationSource, CancellationToken
from athena.events import EventName, InMemoryEventBus, ModelEvent, RuntimeEvent
from athena.git_tools import git_read_tools
from athena.models import (
    ModelCapabilities,
    ModelHealth,
    ModelHealthStatus,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
)
from athena.mutation_tools import workspace_mutation_tools
from athena.process_tools import BashTool
from athena.repository_tools import repository_read_tools
from athena.stores import InMemoryToolResultStore
from athena.subagents import (
    CODER_PROFILE,
    EXPLORER_PROFILE,
    VERIFIER_PROFILE,
    ExplorerReport,
    SubagentBrief,
    SubagentBudget,
    SubagentRole,
    SubagentRunner,
    VerifierReport,
)
from athena.tools import Tool
from athena.types import JSONObject
from athena.workspace import Workspace

CALC_BROKEN = "def add(a, b):\n    return a - b\n"
CALC_FIXED = "def add(a, b):\n    return a + b\n"
CALC_TEST = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def _git(root: Path, *arguments: str) -> None:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments], capture_output=True, check=False, text=True
    )
    if completed.returncode != 0:
        pytest.fail(f"git {' '.join(arguments)} failed: {completed.stderr}")


def _sandbox(root: Path, *, broken: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "calc.py").write_text(CALC_BROKEN if broken else CALC_FIXED, encoding="utf-8")
    (root / "test_calc.py").write_text(CALC_TEST, encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "athena@example.invalid")
    _git(root, "config", "user.name", "Athena Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    return root


def _catalog(bus: InMemoryEventBus) -> dict[str, Tool]:
    tools: list[Tool] = [
        *repository_read_tools(),
        *workspace_mutation_tools(bus),
        *git_read_tools(),
        BashTool(event_bus=bus),
    ]
    return {tool.spec.name: tool for tool in tools}


class _ScriptedProvider(ModelProvider):
    """Replays a script and records the exact context each delegate was given."""

    def __init__(self, responses: Sequence[ModelResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[ModelRequest] = []

    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        cancellation.raise_if_cancelled()
        self.requests.append(request)
        if not self._responses:
            return ModelResponse("Out of script.", "scripted", "stop")
        return self._responses.pop(0)

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


class _HangingProvider(ModelProvider):
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        del request
        cancellation.raise_if_cancelled()
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

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


def _call(call_id: str, name: str, arguments: JSONObject) -> ModelResponse:
    return ModelResponse(
        "", "scripted", "tool_calls", tool_calls=(ModelToolCall(call_id, name, arguments),)
    )


def _runner(
    bus: InMemoryEventBus, provider: ModelProvider
) -> tuple[SubagentRunner, list[RuntimeEvent]]:
    events: list[RuntimeEvent] = []
    bus.subscribe(events.append)
    runner = SubagentRunner(provider, _catalog(bus), bus, InMemoryToolResultStore())
    return runner, events


# ------------------------------------------------------------------ authority


def test_the_explorer_has_no_way_to_edit_anything() -> None:
    """Enforcement is structural: the tool is not in its registry at all."""
    bus = InMemoryEventBus()
    registry = EXPLORER_PROFILE.registry_for(_catalog(bus))

    names = set(registry.names())

    assert {"glob", "grep", "read_file", "git_diff"} <= names
    assert names.isdisjoint({"write_file", "edit_file", "bash"})
    assert EXPLORER_PROFILE.policy.allow_workspace_writes is False
    assert EXPLORER_PROFILE.policy.allow_local_execution is False


def test_an_explorer_asking_to_write_gets_an_unknown_tool_error(tmp_path: Path) -> None:
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        bus = InMemoryEventBus()
        provider = _ScriptedProvider(
            [
                _call("e1", "write_file", {"path": "sneaky.txt", "content": "x"}),
                ModelResponse('{"findings": ["blocked"]}', "scripted", "stop"),
            ]
        )
        runner, _ = _runner(bus, provider)

        result = await runner.delegate(
            SubagentRole.EXPLORER,
            SubagentBrief(objective="Look around"),
            Workspace.from_path(root),
            CancellationSource().token,
        )

        assert not (root / "sneaky.txt").exists()
        # The refusal reached the model as a structured error, not a crash.
        assert result.status is AgentRunStatus.COMPLETED
        assert result.files_modified == ()

    asyncio.run(scenario())


def test_the_verifier_can_run_checks_but_cannot_fix_anything() -> None:
    bus = InMemoryEventBus()
    names = set(VERIFIER_PROFILE.registry_for(_catalog(bus)).names())

    assert "bash" in names, "the verifier has to be able to run the suite"
    assert names.isdisjoint({"write_file", "edit_file"})
    assert VERIFIER_PROFILE.policy.allow_local_execution is True
    assert VERIFIER_PROFILE.policy.allow_workspace_writes is False


def test_a_verifier_cannot_silently_repair_what_it_found(tmp_path: Path) -> None:
    root = _sandbox(tmp_path / "repo")
    original = (root / "calc.py").read_text(encoding="utf-8")

    async def scenario() -> None:
        bus = InMemoryEventBus()
        provider = _ScriptedProvider(
            [
                _call(
                    "v1",
                    "edit_file",
                    {"path": "calc.py", "old_string": "a - b", "new_string": "a + b"},
                ),
                ModelResponse('{"passed": false, "failures": ["test_add"]}', "scripted", "stop"),
            ]
        )
        runner, _ = _runner(bus, provider)

        result = await runner.delegate(
            SubagentRole.VERIFIER,
            SubagentBrief(objective="Run the suite and report"),
            Workspace.from_path(root),
            CancellationSource().token,
        )

        assert (root / "calc.py").read_text(encoding="utf-8") == original
        report = result.verifier_report()
        assert report.passed is False
        assert report.failures == ("test_add",)

    asyncio.run(scenario())


def test_the_coder_carries_no_tool_it_does_not_need() -> None:
    bus = InMemoryEventBus()
    names = set(CODER_PROFILE.registry_for(_catalog(bus)).names())

    assert {"edit_file", "write_file", "read_file", "git_diff"} <= names
    # No history archaeology, no directory crawling, and above all no commit.
    assert names.isdisjoint({"git_commit", "git_log", "git_show", "list_directory"})


def test_no_profile_can_commit_or_delegate() -> None:
    bus = InMemoryEventBus()
    catalog = _catalog(bus)

    for profile in (EXPLORER_PROFILE, CODER_PROFILE, VERIFIER_PROFILE):
        names = set(profile.registry_for(catalog).names())
        assert "git_commit" not in names
        assert not any(name.startswith("delegate") or name.endswith("subagent") for name in names)


def test_a_profile_whose_tools_are_absent_fails_loudly() -> None:
    from athena.errors import ToolValidationError

    with pytest.raises(ToolValidationError):
        EXPLORER_PROFILE.registry_for({})


# ------------------------------------------------------------------ isolation


def test_a_delegate_receives_a_brief_and_never_the_parent_conversation(
    tmp_path: Path,
) -> None:
    root = _sandbox(tmp_path / "repo")
    secret = "PARENT-ONLY-CONVERSATION-SECRET"

    async def scenario() -> None:
        bus = InMemoryEventBus()
        provider = _ScriptedProvider([ModelResponse('{"findings": ["ok"]}', "scripted", "stop")])
        runner, _ = _runner(bus, provider)

        result = await runner.delegate(
            SubagentRole.EXPLORER,
            SubagentBrief(
                objective="Summarise calc.py",
                relevant_files=("calc.py",),
                findings=("add subtracts",),
            ),
            Workspace.from_path(root),
            CancellationSource().token,
            parent_session_id="parent-1",
        )

        assert result.succeeded
        context = "\n".join(
            message.content for request in provider.requests for message in request.messages
        )
        assert secret not in context, "the parent's conversation must not leak"
        assert "Summarise calc.py" in context
        assert "add subtracts" in context, "the brief's findings are the only inherited context"
        # A fresh session, not a continuation of the parent's.
        assert result.session_id != "parent-1"

    asyncio.run(scenario())


def test_a_delegate_starts_with_an_empty_conversation(tmp_path: Path) -> None:
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        bus = InMemoryEventBus()
        provider = _ScriptedProvider([ModelResponse('{"findings": []}', "scripted", "stop")])
        runner, _ = _runner(bus, provider)

        await runner.delegate(
            SubagentRole.EXPLORER,
            SubagentBrief(objective="Look"),
            Workspace.from_path(root),
            CancellationSource().token,
        )

        first = provider.requests[0]
        assert [message.role.value for message in first.messages] == ["system", "user"]

    asyncio.run(scenario())


def test_delegation_emits_its_own_lifecycle_events(tmp_path: Path) -> None:
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        bus = InMemoryEventBus()
        provider = _ScriptedProvider([ModelResponse('{"findings": []}', "scripted", "stop")])
        runner, events = _runner(bus, provider)

        result = await runner.delegate(
            SubagentRole.EXPLORER,
            SubagentBrief(objective="Look"),
            Workspace.from_path(root),
            CancellationSource().token,
            parent_session_id="parent-1",
        )

        started = [e for e in events if e.name is EventName.SUBAGENT_STARTED]
        completed = [e for e in events if e.name is EventName.SUBAGENT_COMPLETED]
        assert len(started) == 1 and len(completed) == 1
        assert started[0].payload["role"] == "explorer"
        assert started[0].session_id == "parent-1"
        assert completed[0].correlation_id == result.session_id

    asyncio.run(scenario())


def test_a_delegate_is_announced_by_name_before_it_works(tmp_path: Path) -> None:
    """El hijo dice como se llama al empezar, no al terminar.

    Sus eventos viajan por el mismo bus con su propia sesion, asi que todo lo que haga
    mientras trabaja llega sin dueno si su nombre solo se conoce al final: justo el rato
    en que alguien esta mirando, y justo lo que un registro duradero tiene que atribuir.
    """
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        bus = InMemoryEventBus()
        provider = _ScriptedProvider([ModelResponse('{"findings": []}', "scripted", "stop")])
        runner, events = _runner(bus, provider)

        result = await runner.delegate(
            SubagentRole.EXPLORER,
            SubagentBrief(objective="Look"),
            Workspace.from_path(root),
            CancellationSource().token,
            parent_session_id="parent-1",
        )

        started = next(e for e in events if e.name is EventName.SUBAGENT_STARTED)
        assert started.payload["session_id"] == result.session_id
        assert started.correlation_id == result.session_id

        propios = [e for e in events if e.session_id == result.session_id]
        assert propios, "el hijo no publico nada bajo el nombre anunciado"
        anuncio = events.index(started)
        assert all(events.index(e) > anuncio for e in propios), (
            "el hijo hablo antes de que se supiera quien era"
        )

    asyncio.run(scenario())


# ------------------------------------------------------------------ limits


def test_a_delegate_stops_at_its_own_iteration_budget(tmp_path: Path) -> None:
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        bus = InMemoryEventBus()
        # Never finishes: always asks for another tool call.
        provider = _ScriptedProvider(
            [_call(f"c{index}", "glob", {"pattern": "*.py"}) for index in range(20)]
        )
        runner, events = _runner(bus, provider)

        result = await runner.delegate(
            SubagentRole.EXPLORER,
            SubagentBrief(objective="Loop forever"),
            Workspace.from_path(root),
            CancellationSource().token,
            budget=SubagentBudget(max_iterations=3, max_tool_calls=10, timeout_seconds=120),
        )

        assert result.status is AgentRunStatus.FAILED
        assert result.error is not None
        assert result.error.code == "budget_exceeded"
        assert any(e.name is EventName.SUBAGENT_FAILED for e in events)

    asyncio.run(scenario())


def test_a_delegate_stops_at_its_own_tool_call_budget(tmp_path: Path) -> None:
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        bus = InMemoryEventBus()
        provider = _ScriptedProvider(
            [_call(f"c{index}", "glob", {"pattern": "*.py"}) for index in range(20)]
        )
        runner, _ = _runner(bus, provider)

        result = await runner.delegate(
            SubagentRole.EXPLORER,
            SubagentBrief(objective="Loop forever"),
            Workspace.from_path(root),
            CancellationSource().token,
            budget=SubagentBudget(max_iterations=15, max_tool_calls=2, timeout_seconds=120),
        )

        assert result.status is AgentRunStatus.FAILED
        assert result.error is not None
        assert result.error.code == "budget_exceeded"

    asyncio.run(scenario())


def test_a_delegate_stops_at_its_own_timeout(tmp_path: Path) -> None:
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        bus = InMemoryEventBus()
        runner, _ = _runner(bus, _HangingProvider())

        result = await runner.delegate(
            SubagentRole.EXPLORER,
            SubagentBrief(objective="Hang"),
            Workspace.from_path(root),
            CancellationSource().token,
            budget=SubagentBudget(max_iterations=4, max_tool_calls=4, timeout_seconds=0.4),
        )

        assert result.status is AgentRunStatus.FAILED
        assert result.error is not None
        assert result.error.code == "process_timeout"

    asyncio.run(scenario())


def test_cancelling_the_parent_cancels_the_child(tmp_path: Path) -> None:
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        bus = InMemoryEventBus()
        provider = _HangingProvider()
        runner, events = _runner(bus, provider)
        parent = CancellationSource()
        task = asyncio.create_task(
            runner.delegate(
                SubagentRole.EXPLORER,
                SubagentBrief(objective="Hang until the parent gives up"),
                Workspace.from_path(root),
                parent.token,
            )
        )
        await asyncio.wait_for(provider.started.wait(), timeout=5)

        parent.cancel()
        result = await asyncio.wait_for(task, timeout=10)

        assert result.status is AgentRunStatus.CANCELLED
        assert any(e.name is EventName.SUBAGENT_CANCELLED for e in events)

    asyncio.run(scenario())


def test_a_failing_child_reports_instead_of_taking_the_parent_down(tmp_path: Path) -> None:
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        from athena.errors import ModelPermanentError

        class _Broken(_ScriptedProvider):
            async def complete(
                self, request: ModelRequest, cancellation: CancellationToken
            ) -> ModelResponse:
                del request
                cancellation.raise_if_cancelled()
                raise ModelPermanentError("the provider is unusable")

        bus = InMemoryEventBus()
        runner, events = _runner(bus, _Broken([]))

        result = await runner.delegate(
            SubagentRole.EXPLORER,
            SubagentBrief(objective="Try"),
            Workspace.from_path(root),
            CancellationSource().token,
        )

        assert result.status is AgentRunStatus.FAILED
        assert result.error is not None
        assert result.error.code == "model_permanent_error"
        assert any(e.name is EventName.SUBAGENT_FAILED for e in events)

    asyncio.run(scenario())


# ------------------------------------------------------------------ reports


def test_the_explorer_report_is_structured() -> None:
    report = ExplorerReport.parse(
        '```json\n{"relevant_files": ["calc.py"], "findings": ["add subtracts"], '
        '"risks": ["no tests for edge cases"], "recommended_next_steps": ["fix the operator"]}\n```'
    )

    assert report.relevant_files == ("calc.py",)
    assert report.findings == ("add subtracts",)
    assert report.risks == ("no tests for edge cases",)
    assert report.recommended_next_steps == ("fix the operator",)
    assert report.unstructured is False


def test_an_unparseable_report_keeps_the_prose_instead_of_losing_it() -> None:
    report = ExplorerReport.parse("I looked at calc.py and the operator is wrong.")

    assert report.unstructured is True
    assert report.findings == ("I looked at calc.py and the operator is wrong.",)


def test_a_verifier_report_defaults_to_not_passed() -> None:
    assert VerifierReport.parse(None).passed is False
    assert VerifierReport.parse("it went fine, honestly").passed is False


# ------------------------------------------------------------------ end to end


def test_explorer_then_coder_then_verifier(tmp_path: Path) -> None:
    """The whole point: three bounded delegates, each doing only its own job."""
    root = _sandbox(tmp_path / "repo")
    workspace = Workspace.from_path(root)
    pytest_command = f'"{sys.executable}" -m pytest -q'

    async def scenario() -> None:
        bus = InMemoryEventBus()
        events: list[RuntimeEvent] = []
        bus.subscribe(events.append)
        catalog = _catalog(bus)
        store = InMemoryToolResultStore()
        parent = CancellationSource()

        # --- Explorer: read only, reports structure -----------------------
        explorer_provider = _ScriptedProvider(
            [
                _call("x1", "grep", {"query": "def add", "glob": "*.py"}),
                _call("x2", "read_file", {"path": "calc.py"}),
                ModelResponse(
                    '{"relevant_files": ["calc.py"], "findings": ["add subtracts"], '
                    '"risks": [], "recommended_next_steps": ["restore the + operator"]}',
                    "scripted",
                    "stop",
                ),
            ]
        )
        explorer = SubagentRunner(explorer_provider, catalog, bus, store)
        exploration = await explorer.delegate(
            SubagentRole.EXPLORER,
            SubagentBrief(objective="Find why test_add fails"),
            workspace,
            parent.token,
            parent_session_id="parent",
        )
        report = exploration.explorer_report()
        assert exploration.succeeded
        assert report.relevant_files == ("calc.py",)
        assert exploration.files_modified == (), "the explorer must change nothing"

        # --- Coder: acts on the explorer's findings, nothing else ---------
        coder_provider = _ScriptedProvider(
            [
                _call(
                    "c1",
                    "edit_file",
                    {"path": "calc.py", "old_string": "a - b", "new_string": "a + b"},
                ),
                ModelResponse("Restored the addition in calc.py.", "scripted", "stop"),
            ]
        )
        coder = SubagentRunner(coder_provider, catalog, bus, store)
        coding = await coder.delegate(
            SubagentRole.CODER,
            SubagentBrief(
                objective="Make test_add pass",
                acceptance_criteria=("The suite passes",),
                relevant_files=report.relevant_files,
                findings=report.findings,
            ),
            workspace,
            parent.token,
            parent_session_id="parent",
        )
        assert coding.succeeded
        assert coding.files_modified == ("calc.py",)
        assert (root / "calc.py").read_text(encoding="utf-8") == CALC_FIXED

        # --- Verifier: runs the suite, reports, fixes nothing -------------
        verifier_provider = _ScriptedProvider(
            [
                _call("v1", "bash", {"command": pytest_command, "timeout_seconds": 120}),
                ModelResponse(
                    '{"passed": true, "checks": ["pytest -q"], "failures": [], '
                    '"evidence": ["1 passed"]}',
                    "scripted",
                    "stop",
                ),
            ]
        )
        verifier = SubagentRunner(verifier_provider, catalog, bus, store)
        verification = await verifier.delegate(
            SubagentRole.VERIFIER,
            SubagentBrief(
                objective="Run the suite and report",
                acceptance_criteria=("The suite passes",),
            ),
            workspace,
            parent.token,
            parent_session_id="parent",
        )
        verdict = verification.verifier_report()
        assert verification.succeeded
        assert verdict.passed is True
        assert verification.files_modified == (), "the verifier must change nothing"

        # --- the parent sees three bounded delegations --------------------
        roles = [
            event.payload["role"] for event in events if event.name is EventName.SUBAGENT_COMPLETED
        ]
        assert roles == ["explorer", "coder", "verifier"]

        # Each delegate got its own session and its own brief, not a shared thread.
        sessions = {exploration.session_id, coding.session_id, verification.session_id}
        assert len(sessions) == 3

    asyncio.run(scenario())
