from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

from athena.agent_loop import AgentLoop, AgentLoopConfig, AgentRunStatus
from athena.cancellation import CancellationSource, CancellationToken
from athena.context import ContextBuilder
from athena.errors import (
    BudgetExceededError,
    ModelPermanentError,
    ModelTransientError,
    ProcessCancelledError,
)
from athena.events import EventName, InMemoryEventBus, ModelEvent, RuntimeEvent
from athena.models import (
    ModelCapabilities,
    ModelHealth,
    ModelHealthStatus,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
)
from athena.permissions import (
    PermissionRequest,
    ReadOnlyPermissionEngine,
    RiskLevel,
    RiskTier,
)
from athena.registry import ToolRegistry
from athena.repository_tools import repository_read_tools
from athena.stores import InMemoryToolResultStore
from athena.testing import FakeModelProvider
from athena.tool_executor import ToolExecutor
from athena.tools import ToolContext, ToolResult, ToolSpec
from athena.types import JSONObject
from athena.workspace import Workspace


def _response_with_call(call_id: str, name: str, arguments: JSONObject) -> ModelResponse:
    return ModelResponse(
        "",
        "fake",
        "tool_calls",
        tool_calls=(ModelToolCall(call_id, name, arguments),),
    )


def _runtime(
    root: Path,
    provider: ModelProvider,
    *,
    tools: tuple[object, ...] | None = None,
    config: AgentLoopConfig | None = None,
) -> tuple[
    AgentLoop,
    Workspace,
    CancellationSource,
    InMemoryEventBus,
    InMemoryToolResultStore,
]:
    workspace = Workspace.from_path(root, "test-workspace")
    registry = ToolRegistry(tools or repository_read_tools())  # type: ignore[arg-type]
    bus = InMemoryEventBus()
    store = InMemoryToolResultStore()
    executor = ToolExecutor(registry, ReadOnlyPermissionEngine(), store, bus)
    loop = AgentLoop(
        provider,
        registry,
        executor,
        ContextBuilder(workspace),
        bus,
        config=config or AgentLoopConfig(retry_backoff_seconds=0),
    )
    return loop, workspace, CancellationSource(), bus, store


def test_model_read_result_model_done(tmp_path: Path) -> None:
    async def scenario() -> None:
        (tmp_path / "README.md").write_text("Athena repository", encoding="utf-8")
        provider = FakeModelProvider(
            [
                _response_with_call("read-1", "read_file", {"path": "README.md"}),
                ModelResponse("Repository identified.", "fake", "stop"),
            ]
        )
        loop, workspace, source, _, _ = _runtime(tmp_path, provider)

        result = await loop.run("Identify this repository", workspace, source.token)

        assert result.status is AgentRunStatus.COMPLETED
        assert result.answer == "Repository identified."
        assert result.tool_call_ids == ("read-1",)
        tool_messages = [
            message
            for message in provider.requests[-1].messages
            if message.tool_call_id == "read-1"
        ]
        assert len(tool_messages) == 1
        assert "Athena repository" in tool_messages[0].content

    asyncio.run(scenario())


def test_model_glob_grep_read_done(tmp_path: Path) -> None:
    async def scenario() -> None:
        source_file = tmp_path / "src" / "runtime.py"
        source_file.parent.mkdir()
        source_file.write_text("class AgentLoop:\n    pass\n", encoding="utf-8")
        before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
        provider = FakeModelProvider(
            [
                _response_with_call("glob-1", "glob", {"pattern": "src/**/*.py"}),
                _response_with_call(
                    "grep-1",
                    "grep",
                    {"query": "AgentLoop", "glob": "src/**/*.py"},
                ),
                _response_with_call(
                    "range-1",
                    "read_range",
                    {"path": "src/runtime.py", "start_line": 1, "end_line": 2},
                ),
                ModelResponse("AgentLoop is declared in src/runtime.py.", "fake", "done"),
            ]
        )
        loop, workspace, source, _, _ = _runtime(tmp_path, provider)

        result = await loop.run("Locate AgentLoop", workspace, source.token)

        assert result.status is AgentRunStatus.COMPLETED
        assert set(result.tool_call_ids) == {"glob-1", "grep-1", "range-1"}
        assert len(provider.requests) == 4
        after = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
        assert after == before

    asyncio.run(scenario())


def test_unknown_tool_and_invalid_input_return_structured_errors(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = FakeModelProvider(
            [
                _response_with_call("bad-1", "missing", {}),
                _response_with_call("bad-2", "read_range", {"path": "x", "start_line": 5}),
                ModelResponse("Handled tool errors.", "fake", "stop"),
            ]
        )
        loop, workspace, source, _, _ = _runtime(tmp_path, provider)

        result = await loop.run("Exercise errors", workspace, source.token)

        assert result.status is AgentRunStatus.COMPLETED
        messages = [
            json.loads(message.content)
            for request in provider.requests[1:]
            for message in request.messages
            if message.tool_call_id in {"bad-1", "bad-2"}
        ]
        assert any(item["error"]["code"] == "tool_validation_error" for item in messages)

    asyncio.run(scenario())


class _FlakyProvider(FakeModelProvider):
    def __init__(self, failures: int, final: ModelResponse) -> None:
        super().__init__([final])
        self.failures = failures

    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        if self.failures:
            self.failures -= 1
            raise ModelTransientError("retry")
        return await super().complete(request, cancellation)


class _PermanentFailureProvider(FakeModelProvider):
    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        del request, cancellation
        raise ModelPermanentError("invalid configuration")


def test_transient_provider_failure_retries_with_limit(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = _FlakyProvider(2, ModelResponse("Recovered.", "fake", "stop"))
        loop, workspace, source, bus, _ = _runtime(
            tmp_path,
            provider,
            config=AgentLoopConfig(max_model_retries=2, retry_backoff_seconds=0),
        )
        events: list[RuntimeEvent] = []
        bus.subscribe(events.append)

        result = await loop.run("Recover", workspace, source.token)

        assert result.status is AgentRunStatus.COMPLETED
        assert len([event for event in events if event.name is EventName.MODEL_FAILED]) == 2

    asyncio.run(scenario())


def test_permanent_provider_failure_aborts(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = _PermanentFailureProvider([])
        loop, workspace, source, _, _ = _runtime(tmp_path, provider)

        result = await loop.run("Fail", workspace, source.token)

        assert result.status is AgentRunStatus.FAILED
        assert isinstance(result.error, ModelPermanentError)

    asyncio.run(scenario())


class _BlockingProvider(FakeModelProvider):
    def __init__(self) -> None:
        super().__init__([])
        self.started = asyncio.Event()

    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        del request, cancellation
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def test_cancel_while_provider_is_processing(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = _BlockingProvider()
        loop, workspace, source, bus, _ = _runtime(tmp_path, provider)
        events: list[RuntimeEvent] = []
        bus.subscribe(events.append)
        task = asyncio.create_task(loop.run("Wait", workspace, source.token))
        await asyncio.wait_for(provider.started.wait(), timeout=1)

        source.cancel()
        result = await asyncio.wait_for(task, timeout=1)

        assert result.status is AgentRunStatus.CANCELLED
        assert result.session.agent.status == "cancelled"
        assert result.session.agent.active_model_request_id is None
        assert EventName.MODEL_FAILED in {event.name for event in events}
        assert EventName.AGENT_CANCELLED in {event.name for event in events}

    asyncio.run(scenario())


def test_session_timeout_does_not_leave_agent_running(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = _BlockingProvider()
        loop, workspace, source, _, _ = _runtime(
            tmp_path,
            provider,
            config=AgentLoopConfig(session_timeout_seconds=0.02, retry_backoff_seconds=0),
        )

        result = await loop.run("Timeout", workspace, source.token)

        assert result.status is AgentRunStatus.FAILED
        assert result.error is not None and result.error.code == "process_timeout"
        assert result.session.agent.status == "failed"

    asyncio.run(scenario())


class _LongTool:
    spec = ToolSpec(
        "long_read",
        "A cancellable long read for contract testing.",
        {"type": "object"},
        {"type": "object"},
        RiskLevel.LOW,
        1000,
    )

    def __init__(self) -> None:
        self.started = asyncio.Event()

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
        self,
        context: ToolContext,
        arguments: JSONObject,
        cancellation: CancellationToken,
    ) -> ToolResult:
        del context, arguments, cancellation
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    def is_read_only(self, arguments: JSONObject) -> bool:
        return True

    def is_destructive(self, arguments: JSONObject) -> bool:
        return False

    def is_concurrency_safe(self, arguments: JSONObject) -> bool:
        return True


def test_cancel_while_tool_is_processing(tmp_path: Path) -> None:
    async def scenario() -> None:
        tool = _LongTool()
        provider = FakeModelProvider([_response_with_call("long-1", "long_read", {})])
        loop, workspace, source, bus, _ = _runtime(tmp_path, provider, tools=(tool,))
        events: list[RuntimeEvent] = []
        bus.subscribe(events.append)
        task = asyncio.create_task(loop.run("Wait", workspace, source.token))
        await asyncio.wait_for(tool.started.wait(), timeout=1)

        source.cancel()
        result = await asyncio.wait_for(task, timeout=1)

        assert result.status is AgentRunStatus.CANCELLED
        assert result.session.agent.active_tool_call_ids == ()
        failed_tools = [event for event in events if event.name is EventName.TOOL_FAILED]
        assert [event.correlation_id for event in failed_tools] == ["long-1"]
        assert EventName.AGENT_CANCELLED in {event.name for event in events}

    asyncio.run(scenario())


def test_max_iterations_fails_with_budget_error(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = FakeModelProvider(
            [
                _response_with_call("list-1", "list_directory", {}),
                _response_with_call("list-2", "list_directory", {}),
            ]
        )
        loop, workspace, source, _, _ = _runtime(
            tmp_path,
            provider,
            config=AgentLoopConfig(max_iterations=2, retry_backoff_seconds=0),
        )

        result = await loop.run("Never finish", workspace, source.token)

        assert result.status is AgentRunStatus.FAILED
        assert isinstance(result.error, BudgetExceededError)

    asyncio.run(scenario())


class _AlternateProvider(ModelProvider):
    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        del request
        cancellation.raise_if_cancelled()
        return ModelResponse("Alternate provider works.", "alternate", "stop")

    async def stream(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> AsyncIterator[ModelEvent]:
        del request, cancellation
        if False:
            yield ModelEvent(EventName.MODEL_COMPLETED, "never")

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(False, False, False)

    async def health(self, cancellation: CancellationToken) -> ModelHealth:
        cancellation.raise_if_cancelled()
        return ModelHealth(ModelHealthStatus.HEALTHY)


def test_same_loop_accepts_another_model_provider(tmp_path: Path) -> None:
    async def scenario() -> None:
        loop, workspace, source, _, _ = _runtime(tmp_path, _AlternateProvider())

        result = await loop.run("Answer", workspace, source.token)

        assert result.status is AgentRunStatus.COMPLETED
        assert result.answer == "Alternate provider works."

    asyncio.run(scenario())


class _CancelledProcessTool:
    spec = ToolSpec(
        name="long_process",
        description="Simulates a command that is killed because the session was cancelled.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        output_schema={"type": "object"},
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
        self,
        context: ToolContext,
        arguments: JSONObject,
        cancellation: CancellationToken,
    ) -> ToolResult:
        del context, arguments, cancellation
        raise ProcessCancelledError("Command cancelled and child process terminated")

    def is_read_only(self, arguments: JSONObject) -> bool:
        return True

    def is_destructive(self, arguments: JSONObject) -> bool:
        return False

    def is_concurrency_safe(self, arguments: JSONObject) -> bool:
        return True


def test_a_killed_child_process_ends_the_run_as_cancelled(tmp_path: Path) -> None:
    """A cancelled process is not a tool failure to report back to the model."""

    async def scenario() -> None:
        provider = FakeModelProvider(
            [
                _response_with_call("p-1", "long_process", {}),
                ModelResponse("Should never be reached.", "fake", "stop"),
            ]
        )
        loop, workspace, source, _, _ = _runtime(
            tmp_path, provider, tools=(_CancelledProcessTool(),)
        )

        result = await loop.run("Run it", workspace, source.token)

        assert result.status is AgentRunStatus.CANCELLED
        assert result.session.agent.status == "cancelled"
        assert result.error is not None
        assert result.error.code == "process_cancelled"
        assert len(provider.requests) == 1, "the model must not be asked to continue"

    asyncio.run(scenario())
