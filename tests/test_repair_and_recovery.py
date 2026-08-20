from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from athena.agent_loop import AgentLoop, AgentLoopConfig, AgentRunStatus
from athena.cancellation import CancellationSource, CancellationToken
from athena.context import ContextBuilder
from athena.errors import (
    AthenaRuntimeError,
    ContextOverflowError,
    ModelTransientError,
    PermissionDeniedError,
    ToolValidationError,
    WorkspaceBoundaryError,
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
from athena.mutation_tools import workspace_mutation_tools
from athena.permissions import PermissionDecision, PermissionPolicy, PolicyPermissionEngine
from athena.recovery import RecoveryAction, RecoveryLimits, RecoveryPolicy
from athena.registry import ToolRegistry
from athena.repository_tools import repository_read_tools
from athena.state import SessionState
from athena.stores import InMemoryToolResultStore
from athena.testing import FakeModelProvider, ScriptedPermissionPrompt
from athena.tool_executor import ToolExecutor
from athena.types import JSONObject
from athena.verification import (
    CommandVerificationPolicy,
    VerificationEvidence,
    VerificationPlanner,
    VerificationResult,
    VerificationStatus,
)
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


def _sandbox(root: Path, files: dict[str, str]) -> Path:
    command = f'"{sys.executable}" -m pytest -q'
    files = {**files, "AGENTS.md": f"# Sandbox\n\n## Verification\n\n```\n{command}\n```\n"}
    for name, content in files.items():
        (root / name).write_text(content, encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "athena@example.invalid")
    _git(root, "config", "user.name", "Athena Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    return root


def _call(call_id: str, name: str, arguments: JSONObject) -> ModelResponse:
    return ModelResponse(
        "", "scripted", "tool_calls", tool_calls=(ModelToolCall(call_id, name, arguments),)
    )


def _runtime(
    root: Path,
    provider: ModelProvider,
    *,
    approvals: int = 8,
    config: AgentLoopConfig | None = None,
    verification: object | None = None,
    policy: PermissionPolicy | None = None,
) -> tuple[AgentLoop, Workspace, CancellationSource, list[RuntimeEvent]]:
    bus = InMemoryEventBus()
    events: list[RuntimeEvent] = []
    bus.subscribe(events.append)
    workspace = Workspace.from_path(root)
    registry = ToolRegistry((*repository_read_tools(), *workspace_mutation_tools(bus)))
    executor = ToolExecutor(
        registry,
        PolicyPermissionEngine(
            policy if policy is not None else PermissionPolicy(allow_workspace_writes=True)
        ),
        InMemoryToolResultStore(),
        bus,
        prompt=ScriptedPermissionPrompt((PermissionDecision.ALLOW,) * approvals),
    )
    verification_policy = verification or CommandVerificationPolicy(
        VerificationPlanner(workspace), event_bus=bus
    )
    loop = AgentLoop(
        provider,
        registry,
        executor,
        ContextBuilder(workspace),
        bus,
        verification=verification_policy,  # type: ignore[arg-type]
        config=config or AgentLoopConfig(max_iterations=10, session_timeout_seconds=600.0),
    )
    return loop, workspace, CancellationSource(), events


class _AlwaysPassingVerification:
    async def verify(
        self, state: SessionState, workspace: Workspace, cancellation: CancellationToken
    ) -> VerificationResult:
        del state, workspace
        cancellation.raise_if_cancelled()
        return VerificationResult(
            VerificationStatus.PASSED,
            (VerificationEvidence(kind="stub", summary="stubbed pass"),),
            "stubbed",
        )


class _BlockingVerification:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def verify(
        self, state: SessionState, workspace: Workspace, cancellation: CancellationToken
    ) -> VerificationResult:
        del state, workspace, cancellation
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _ScriptedFailureProvider(ModelProvider):
    """Raises the scripted errors first, then answers normally."""

    def __init__(self, failures: Sequence[Exception], answer: str = "Done.") -> None:
        self._failures = list(failures)
        self._answer = answer
        self.attempts = 0
        self.request_sizes: list[int] = []

    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        cancellation.raise_if_cancelled()
        self.attempts += 1
        self.request_sizes.append(len(request.messages))
        if self._failures:
            raise self._failures.pop(0)
        return ModelResponse(self._answer, "scripted", "stop")

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


# ------------------------------------------------------------------ repair loop


def test_athena_breaks_the_suite_then_repairs_itself(tmp_path: Path) -> None:
    """Introduce a bug, let the verifier catch it, and repair until it passes."""
    root = _sandbox(tmp_path, {"calc.py": CALC_FIXED, "test_calc.py": CALC_TEST})

    async def scenario() -> None:
        provider = FakeModelProvider(
            [
                # First attempt: a wrong edit, then a confident "done".
                _call(
                    "c1",
                    "edit_file",
                    {"path": "calc.py", "old_string": "a + b", "new_string": "a * b"},
                ),
                ModelResponse("I updated calc.add.", "scripted", "stop"),
                # After the evidence comes back, fix it properly.
                _call(
                    "c2",
                    "edit_file",
                    {"path": "calc.py", "old_string": "a * b", "new_string": "a + b"},
                ),
                ModelResponse("Corrected calc.add; the suite passes.", "scripted", "stop"),
            ]
        )
        loop, workspace, source, events = _runtime(root, provider)

        result = await loop.run("Keep calc.add correct", workspace, source.token)

        assert result.status is AgentRunStatus.COMPLETED
        assert (root / "calc.py").read_text(encoding="utf-8") == CALC_FIXED
        assert result.verification is not None
        assert result.verification.status is VerificationStatus.PASSED
        names = [event.name for event in events]
        assert EventName.VERIFICATION_FAILED in names
        assert EventName.RECOVERY_ACTION in names
        assert EventName.VERIFICATION_CHECK_COMPLETED in names
        recovery = [
            event.payload
            for event in events
            if event.name is EventName.RECOVERY_ACTION
            and event.payload.get("action") == RecoveryAction.RETURN_EVIDENCE.value
        ]
        assert len(recovery) == 1
        assert result.working_state is not None
        assert "calc.py" in result.working_state.files_modified

    asyncio.run(scenario())


def test_repair_cycles_are_bounded_and_report_a_diagnosis(tmp_path: Path) -> None:
    root = _sandbox(tmp_path, {"calc.py": CALC_FIXED, "test_calc.py": CALC_TEST})

    async def scenario() -> None:
        provider = FakeModelProvider(
            [
                _call(
                    "c1",
                    "edit_file",
                    {"path": "calc.py", "old_string": "a + b", "new_string": "a * b"},
                ),
                ModelResponse("Done.", "scripted", "stop"),
                ModelResponse("Still done.", "scripted", "stop"),
                ModelResponse("Really done.", "scripted", "stop"),
            ]
        )
        loop, workspace, source, events = _runtime(
            root,
            provider,
            config=AgentLoopConfig(
                max_iterations=10, session_timeout_seconds=600.0, max_repair_cycles=1
            ),
        )

        result = await loop.run("Break it", workspace, source.token)

        assert result.status is AgentRunStatus.FAILED
        assert result.error is not None
        assert "repair cycle" in result.error.message
        assert any(event.name is EventName.RECOVERY_EXHAUSTED for event in events)

    asyncio.run(scenario())


def test_saying_done_without_a_verification_plan_does_not_complete(tmp_path: Path) -> None:
    """No plan means no evidence, and no evidence means no completion."""
    (tmp_path / "notes.txt").write_text("nothing to verify", encoding="utf-8")

    async def scenario() -> None:
        provider = FakeModelProvider([ModelResponse("All finished.", "scripted", "stop")])
        loop, workspace, source, _ = _runtime(tmp_path, provider)

        result = await loop.run("Claim success", workspace, source.token)

        assert result.status is AgentRunStatus.FAILED
        assert result.error is not None
        assert "inconclusive" in result.error.message.lower()

    asyncio.run(scenario())


def test_deleting_the_failing_test_is_refused_by_the_runtime(tmp_path: Path) -> None:
    root = _sandbox(tmp_path, {"calc.py": CALC_BROKEN, "test_calc.py": CALC_TEST})

    async def scenario() -> None:
        provider = FakeModelProvider(
            [
                _call(
                    "c1",
                    "write_file",
                    {
                        "path": "test_calc.py",
                        "content": "# tests removed\n",
                        "overwrite": True,
                    },
                ),
                ModelResponse("Removed the failing test; all green.", "scripted", "stop"),
                ModelResponse("Still claiming success.", "scripted", "stop"),
                ModelResponse("Once more.", "scripted", "stop"),
            ]
        )
        loop, workspace, source, events = _runtime(
            root,
            provider,
            config=AgentLoopConfig(
                max_iterations=10, session_timeout_seconds=600.0, max_repair_cycles=1
            ),
        )

        result = await loop.run("Make the suite pass", workspace, source.token)

        assert result.status is AgentRunStatus.FAILED
        failures = [
            event.payload for event in events if event.name is EventName.VERIFICATION_FAILED
        ]
        assert any(payload.get("reason") == "integrity" for payload in failures)

    asyncio.run(scenario())


def test_a_pre_existing_failure_does_not_block_completion(tmp_path: Path) -> None:
    root = _sandbox(
        tmp_path,
        {
            "calc.py": CALC_BROKEN,
            "test_calc.py": CALC_TEST,
            "notes.md": "untouched\n",
        },
    )

    async def scenario() -> None:
        provider = FakeModelProvider(
            [
                _call(
                    "c1",
                    "edit_file",
                    {"path": "notes.md", "old_string": "untouched", "new_string": "documented"},
                ),
                ModelResponse("Documented the module.", "scripted", "stop"),
            ]
        )
        loop, workspace, source, _ = _runtime(root, provider)

        result = await loop.run("Document the module", workspace, source.token)

        assert result.status is AgentRunStatus.COMPLETED
        assert result.verification is not None
        assert "already failing" in result.verification.summary

    asyncio.run(scenario())


def test_cancelling_during_verification_ends_the_run_as_cancelled(tmp_path: Path) -> None:
    async def scenario() -> None:
        verification = _BlockingVerification()
        provider = FakeModelProvider([ModelResponse("Done.", "scripted", "stop")])
        loop, workspace, source, events = _runtime(tmp_path, provider, verification=verification)
        task = asyncio.create_task(loop.run("Verify", workspace, source.token))
        await asyncio.wait_for(verification.started.wait(), timeout=5)

        source.cancel()
        result = await asyncio.wait_for(task, timeout=5)

        assert result.status is AgentRunStatus.CANCELLED
        assert result.session.agent.status == "cancelled"
        assert any(event.name is EventName.AGENT_CANCELLED for event in events)

    asyncio.run(scenario())


# ------------------------------------------------------------------ recovery


def test_transient_provider_errors_retry_a_bounded_number_of_times(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = _ScriptedFailureProvider(
            [ModelTransientError("busy"), ModelTransientError("busy")]
        )
        loop, workspace, source, events = _runtime(
            tmp_path,
            provider,
            verification=_AlwaysPassingVerification(),
            config=AgentLoopConfig(
                max_iterations=4,
                session_timeout_seconds=60.0,
                max_model_retries=2,
                retry_backoff_seconds=0.01,
            ),
        )

        result = await loop.run("Answer", workspace, source.token)

        assert result.status is AgentRunStatus.COMPLETED
        assert provider.attempts == 3
        actions = [
            event.payload["action"] for event in events if event.name is EventName.RECOVERY_ACTION
        ]
        assert actions == [
            RecoveryAction.RETRY_BACKOFF.value,
            RecoveryAction.RETRY_BACKOFF.value,
        ]

    asyncio.run(scenario())


def test_transient_errors_stop_retrying_once_the_limit_is_reached(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = _ScriptedFailureProvider([ModelTransientError("busy")] * 5)
        loop, workspace, source, events = _runtime(
            tmp_path,
            provider,
            verification=_AlwaysPassingVerification(),
            config=AgentLoopConfig(
                max_iterations=4,
                session_timeout_seconds=60.0,
                max_model_retries=1,
                retry_backoff_seconds=0.01,
            ),
        )

        result = await loop.run("Answer", workspace, source.token)

        assert result.status is AgentRunStatus.FAILED
        assert provider.attempts == 2
        assert any(event.name is EventName.RECOVERY_EXHAUSTED for event in events)

    asyncio.run(scenario())


def test_context_overflow_compacts_the_request_and_retries(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = _ScriptedFailureProvider([ContextOverflowError("too long")])
        loop, workspace, source, events = _runtime(
            tmp_path,
            provider,
            verification=_AlwaysPassingVerification(),
            config=AgentLoopConfig(max_iterations=4, session_timeout_seconds=60.0),
        )
        # A long history is what makes compaction observable.
        loop.context_builder = _PaddedContextBuilder(workspace)

        result = await loop.run("Answer", workspace, source.token)

        assert result.status is AgentRunStatus.COMPLETED
        actions = [
            event.payload["action"] for event in events if event.name is EventName.RECOVERY_ACTION
        ]
        assert actions == [RecoveryAction.COMPACT_CONTEXT.value]
        assert provider.request_sizes[1] < provider.request_sizes[0]

    asyncio.run(scenario())


class _PaddedContextBuilder(ContextBuilder):
    """Produces a deliberately long message list so compaction is measurable."""

    async def build_request(self, **keywords: object) -> ModelRequest:
        request = await super().build_request(**keywords)  # type: ignore[arg-type]
        padding = tuple(request.messages[-1] for _ in range(8))
        return ModelRequest(messages=(*request.messages, *padding), tools=request.tools)


def test_recovery_policy_maps_every_documented_error_to_one_action() -> None:
    policy = RecoveryPolicy(RecoveryLimits(model_retries=3, process_retries=2))

    assert policy.decide(ToolValidationError("bad")).action is RecoveryAction.INFORM_MODEL
    assert policy.decide(PermissionDeniedError("no")).action is RecoveryAction.NO_RETRY
    assert policy.decide(WorkspaceBoundaryError("out")).action is RecoveryAction.ABORT
    assert policy.decide(ContextOverflowError("big")).action is RecoveryAction.COMPACT_CONTEXT
    assert policy.decide(ModelTransientError("busy")).max_attempts == 3


def test_an_unclassified_error_is_never_retried() -> None:
    class _Unknown(AthenaRuntimeError):
        code = "unknown_error"

    directive = RecoveryPolicy().decide(_Unknown("mystery"))

    assert directive.action is RecoveryAction.ABORT
    assert "no recovery policy" in directive.reason


def test_a_refused_tool_call_is_not_recorded_as_work_done(tmp_path: Path) -> None:
    """The working state must record what happened, never what was attempted."""
    root = _sandbox(tmp_path, {"calc.py": CALC_FIXED, "test_calc.py": CALC_TEST})

    async def scenario() -> None:
        provider = FakeModelProvider(
            [
                _call(
                    "denied-1",
                    "write_file",
                    {"path": "unauthorised.txt", "content": "should never land"},
                ),
                ModelResponse("I could not write that file.", "scripted", "stop"),
            ]
        )
        # Writes are not granted and nothing approves the ASK, so the call is refused.
        loop, workspace, source, _ = _runtime(
            root, provider, approvals=0, policy=PermissionPolicy()
        )

        result = await loop.run("Try to write something", workspace, source.token)

        assert not (root / "unauthorised.txt").exists()
        assert result.working_state is not None
        assert result.working_state.files_modified == ()
        assert any(error.code == "permission_denied" for error in result.working_state.errors)

    asyncio.run(scenario())
