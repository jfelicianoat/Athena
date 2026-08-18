"""Explicit provider-neutral orchestration loop for read-only investigation."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from athena.async_utils import await_cancellable
from athena.budget import BudgetLimits, RuntimeBudget
from athena.cancellation import CancellationToken
from athena.context import ContextBuilder
from athena.errors import (
    AthenaRuntimeError,
    BudgetExceededError,
    CancellationError,
    FatalRuntimeError,
    ProcessCancelledError,
    ProcessTimeoutError,
    VerificationFailure,
)
from athena.events import (
    AgentEvent,
    EventBus,
    EventName,
    ModelEvent,
    RecoveryEvent,
    ToolEvent,
    VerificationEvent,
)
from athena.models import (
    ModelMessage,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ModelToolCall,
)
from athena.recovery import RecoveryAction, RecoveryLimits, RecoveryPolicy
from athena.registry import ToolRegistry
from athena.state import AgentState, AgentStatus, BudgetState, SessionState
from athena.tool_executor import ToolExecutor
from athena.types import JSONObject
from athena.verification import (
    LoopCompletionVerificationPolicy,
    VerificationPolicy,
    VerificationResult,
    VerificationStatus,
    evidence_digest,
)
from athena.working_state import RecordedError, WorkingState
from athena.workspace import Workspace


class AgentRunStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class AgentLoopConfig:
    max_iterations: int = 12
    session_timeout_seconds: float = 120.0
    max_model_retries: int = 2
    retry_backoff_seconds: float = 0.1
    max_tool_calls: int = 100
    max_repair_cycles: int = 2
    capture_baseline: bool = True


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    status: AgentRunStatus
    session: SessionState
    answer: str | None = None
    error: AthenaRuntimeError | None = None
    tool_call_ids: tuple[str, ...] = ()
    verification: VerificationResult | None = None
    working_state: WorkingState | None = None


@dataclass(slots=True)
class _RunData:
    session: SessionState
    working: WorkingState
    history: list[ModelMessage] = field(default_factory=list)
    seen_call_ids: set[str] = field(default_factory=set)
    discovered_paths: set[str] = field(default_factory=set)
    repair_cycles: int = 0
    compactions: int = 0
    last_verification: VerificationResult | None = None


class AgentLoop:
    def __init__(
        self,
        provider: ModelProvider,
        registry: ToolRegistry,
        executor: ToolExecutor,
        context_builder: ContextBuilder,
        event_bus: EventBus,
        *,
        verification: VerificationPolicy | None = None,
        recovery: RecoveryPolicy | None = None,
        config: AgentLoopConfig | None = None,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.executor = executor
        self.context_builder = context_builder
        self.event_bus = event_bus
        self.verification = verification or LoopCompletionVerificationPolicy()
        self.config = config or AgentLoopConfig()
        self.recovery = recovery or RecoveryPolicy(
            RecoveryLimits(
                model_retries=self.config.max_model_retries,
                model_backoff_seconds=self.config.retry_backoff_seconds,
            )
        )

    async def run(
        self,
        objective: str,
        workspace: Workspace,
        cancellation: CancellationToken,
    ) -> AgentRunResult:
        session_id = str(uuid4())
        initial_agent = AgentState(
            AgentStatus.RUNNING,
            budget=BudgetState(max_steps=self.config.max_iterations),
        )
        data = _RunData(
            SessionState(session_id, workspace.workspace_id, initial_agent),
            WorkingState(objective=objective),
        )
        await self.event_bus.publish(
            AgentEvent(EventName.AGENT_STARTED, session_id, {"objective": objective})
        )
        try:
            return await asyncio.wait_for(
                self._iterate(objective, workspace, cancellation, data),
                timeout=self.config.session_timeout_seconds,
            )
        except TimeoutError:
            timeout = ProcessTimeoutError("Agent session timed out")
            failed = self._set_status(data.session, AgentStatus.FAILED, timeout.code)
            await self.event_bus.publish(
                AgentEvent(
                    EventName.AGENT_FAILED,
                    session_id,
                    {"error_code": timeout.code, "message": timeout.message},
                )
            )
            return AgentRunResult(
                AgentRunStatus.FAILED,
                failed,
                error=timeout,
                tool_call_ids=tuple(data.seen_call_ids),
            )
        except (CancellationError, ProcessCancelledError) as exc:
            cancelled = self._set_status(data.session, AgentStatus.CANCELLED, exc.code)
            await self.event_bus.publish(
                AgentEvent(
                    EventName.AGENT_CANCELLED,
                    session_id,
                    {"error_code": exc.code, "message": exc.message},
                )
            )
            return AgentRunResult(
                AgentRunStatus.CANCELLED,
                cancelled,
                error=exc,
                tool_call_ids=tuple(data.seen_call_ids),
            )
        except AthenaRuntimeError as exc:
            failed = self._set_status(data.session, AgentStatus.FAILED, exc.code)
            await self.event_bus.publish(
                AgentEvent(
                    EventName.AGENT_FAILED,
                    session_id,
                    {"error_code": exc.code, "message": exc.message},
                )
            )
            return AgentRunResult(
                AgentRunStatus.FAILED,
                failed,
                error=exc,
                tool_call_ids=tuple(data.seen_call_ids),
            )
        except Exception as exc:
            fatal = FatalRuntimeError(f"Unexpected runtime failure: {type(exc).__name__}")
            failed = self._set_status(data.session, AgentStatus.FAILED, fatal.code)
            await self.event_bus.publish(
                AgentEvent(
                    EventName.AGENT_FAILED,
                    session_id,
                    {"error_code": fatal.code, "message": fatal.message},
                )
            )
            return AgentRunResult(
                AgentRunStatus.FAILED,
                failed,
                error=fatal,
                tool_call_ids=tuple(data.seen_call_ids),
            )

    async def _iterate(
        self,
        objective: str,
        workspace: Workspace,
        cancellation: CancellationToken,
        data: _RunData,
    ) -> AgentRunResult:
        budget = RuntimeBudget(
            BudgetLimits(
                max_iterations=self.config.max_iterations,
                max_model_calls=self.config.max_iterations * (self.config.max_model_retries + 1),
                max_tool_calls=self.config.max_tool_calls,
            )
        )
        await self._capture_baseline(workspace, cancellation)
        for iteration in range(1, self.config.max_iterations + 1):
            cancellation.raise_if_cancelled()
            budget.consume_iteration()
            data.session = self._with_budget(data.session, budget, AgentStatus.RUNNING)
            request = await self.context_builder.build_request(
                objective=objective,
                history=tuple(data.history),
                important_state={
                    "iteration": iteration,
                    "tool_calls": budget.usage.tool_calls,
                    "repair_cycle": data.repair_cycles,
                    "working_state": data.working.summary(),
                },
                tool_definitions=self.registry.definitions(),
                cancellation=cancellation,
                discovered_paths=tuple(sorted(data.discovered_paths)),
            )
            response = await self._complete(request, cancellation, data, budget)
            data.history.append(
                ModelMessage(
                    ModelRole.ASSISTANT,
                    response.content,
                    tool_calls=response.tool_calls,
                )
            )
            if response.tool_calls:
                await self._execute_calls(
                    response.tool_calls,
                    workspace,
                    cancellation,
                    data,
                    budget,
                )
                continue
            completed = await self._attempt_completion(
                response, data, workspace, cancellation, budget
            )
            if completed is not None:
                return completed
        raise BudgetExceededError("Maximum agent iterations reached without completion")

    async def _capture_baseline(
        self, workspace: Workspace, cancellation: CancellationToken
    ) -> None:
        """Record which checks were already failing, so blame lands where it belongs."""
        if not self.config.capture_baseline:
            return
        capture = getattr(self.verification, "capture_baseline", None)
        if capture is None:
            return
        await capture(workspace, cancellation)

    async def _complete(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
        data: _RunData,
        budget: RuntimeBudget,
    ) -> ModelResponse:
        request_id = str(uuid4())
        data.session = replace(
            data.session,
            agent=replace(data.session.agent, active_model_request_id=request_id),
        )
        attempt = 0
        while True:
            budget.consume_model_call()
            await self.event_bus.publish(
                ModelEvent(
                    EventName.MODEL_STARTED,
                    data.session.session_id,
                    {"attempt": attempt + 1},
                    request_id,
                )
            )
            try:
                response = await await_cancellable(
                    self.provider.complete(request, cancellation),
                    cancellation,
                )
            except AthenaRuntimeError as exc:
                directive = self.recovery.decide(exc)
                retrying = directive.retries and attempt < directive.max_attempts
                await self.event_bus.publish(
                    ModelEvent(
                        EventName.MODEL_FAILED,
                        data.session.session_id,
                        {"error_code": exc.code, "retrying": retrying},
                        request_id,
                    )
                )
                await self.event_bus.publish(
                    RecoveryEvent(
                        EventName.RECOVERY_STARTED,
                        data.session.session_id,
                        {
                            "error_code": exc.code,
                            "action": directive.action.value,
                            "reason": directive.reason,
                        },
                        request_id,
                    )
                )
                data.working = data.working.failing(
                    RecordedError(exc.code, exc.message, directive.action.value)
                )
                if not retrying:
                    if directive.retries:
                        await self.event_bus.publish(
                            RecoveryEvent(
                                EventName.RECOVERY_EXHAUSTED,
                                data.session.session_id,
                                {"error_code": exc.code, "attempts": attempt + 1},
                                request_id,
                            )
                        )
                    raise
                await self.event_bus.publish(
                    RecoveryEvent(
                        EventName.RECOVERY_ACTION,
                        data.session.session_id,
                        {"action": directive.action.value, "attempt": attempt + 1},
                        request_id,
                    )
                )
                if directive.action is RecoveryAction.COMPACT_CONTEXT:
                    request = self._compact(request)
                    data.compactions += 1
                if directive.backoff_seconds:
                    await await_cancellable(
                        asyncio.sleep(directive.backoff_seconds * (2**attempt)),
                        cancellation,
                    )
                attempt += 1
                continue
            data.session = replace(
                data.session,
                agent=replace(data.session.agent, active_model_request_id=None),
            )
            await self.event_bus.publish(
                ModelEvent(
                    EventName.MODEL_COMPLETED,
                    data.session.session_id,
                    {
                        "finish_reason": response.finish_reason,
                        "tool_call_count": len(response.tool_calls),
                    },
                    request_id,
                )
            )
            return response

    async def _execute_calls(
        self,
        calls: tuple[ModelToolCall, ...],
        workspace: Workspace,
        cancellation: CancellationToken,
        data: _RunData,
        budget: RuntimeBudget,
    ) -> None:
        for call in calls:
            cancellation.raise_if_cancelled()
            if not call.call_id or call.call_id in data.seen_call_ids:
                error: JSONObject = {
                    "ok": False,
                    "error": {
                        "code": "tool_validation_error",
                        "message": "Tool call ID is empty or duplicated",
                    },
                    "call_id": call.call_id,
                }
                await self.event_bus.publish(
                    ToolEvent(
                        EventName.TOOL_FAILED,
                        data.session.session_id,
                        {
                            "tool_name": call.name,
                            "error_code": "tool_validation_error",
                            "message": "Tool call ID is empty or duplicated",
                        },
                        call.call_id or None,
                    )
                )
                data.history.append(self._tool_message(call, error))
                continue
            data.seen_call_ids.add(call.call_id)
            budget.consume_tool_call()
            data.session = replace(
                data.session,
                agent=replace(data.session.agent, active_tool_call_ids=(call.call_id,)),
            )
            self._remember_paths(call, data)
            data.working = self._record_tool_use(data.working, call)
            try:
                result = await self.executor.execute(
                    call,
                    session_id=data.session.session_id,
                    workspace=workspace,
                    cancellation=cancellation,
                )
                payload: JSONObject = {
                    "ok": True,
                    "call_id": result.call_id,
                    "output": result.output,
                    "reference_uri": result.reference.uri if result.reference else None,
                }
            except (CancellationError, ProcessCancelledError):
                raise
            except AthenaRuntimeError as exc:
                directive = self.recovery.decide(exc)
                data.working = data.working.failing(
                    RecordedError(exc.code, exc.message, directive.action.value)
                )
                await self.event_bus.publish(
                    RecoveryEvent(
                        EventName.RECOVERY_ACTION,
                        data.session.session_id,
                        {
                            "error_code": exc.code,
                            "action": directive.action.value,
                            "reason": directive.reason,
                        },
                        call.call_id,
                    )
                )
                payload = {
                    "ok": False,
                    "call_id": call.call_id,
                    "recovery": directive.action.value,
                    "error": {"code": exc.code, "message": exc.message, "details": exc.details},
                }
            finally:
                data.session = replace(
                    data.session,
                    agent=replace(data.session.agent, active_tool_call_ids=()),
                )
            data.history.append(self._tool_message(call, payload))

    async def _attempt_completion(
        self,
        response: ModelResponse,
        data: _RunData,
        workspace: Workspace,
        cancellation: CancellationToken,
        budget: RuntimeBudget,
    ) -> AgentRunResult | None:
        """Verify the work. Returns None when a repair cycle should run instead."""
        if response.finish_reason not in ("stop", "done") or not response.content.strip():
            raise VerificationFailure("Model response did not satisfy terminal conditions")
        data.session = replace(
            data.session,
            agent=replace(data.session.agent, status=AgentStatus.VERIFYING),
            attributes={
                **data.session.attributes,
                "final_response": response.content,
                "finish_reason": response.finish_reason,
            },
            updated_at=datetime.now(UTC),
        )
        await self.event_bus.publish(
            VerificationEvent(EventName.VERIFICATION_STARTED, data.session.session_id)
        )
        verification = await await_cancellable(
            self.verification.verify(data.session, workspace, cancellation),
            cancellation,
        )
        data.last_verification = verification
        data.working = data.working.verified(
            {
                "status": verification.status.value,
                "summary": verification.summary,
                "evidence_count": len(verification.evidence),
            }
        )
        await self.event_bus.publish(
            VerificationEvent(
                EventName.VERIFICATION_COMPLETED,
                data.session.session_id,
                {"status": verification.status.value, "evidence_count": len(verification.evidence)},
            )
        )
        if verification.permits_completion:
            return await self._complete_run(response, data, budget, verification)
        if verification.status is VerificationStatus.INCONCLUSIVE:
            # Repairing cannot conjure a verification plan the project does not define.
            raise VerificationFailure(verification.summary)
        return await self._start_repair_cycle(data, verification)

    async def _start_repair_cycle(
        self, data: _RunData, verification: VerificationResult
    ) -> AgentRunResult | None:
        session_id = data.session.session_id
        directive = self.recovery.decide(VerificationFailure(verification.summary))
        await self.event_bus.publish(
            RecoveryEvent(
                EventName.RECOVERY_STARTED,
                session_id,
                {"error_code": "verification_failure", "action": directive.action.value},
            )
        )
        if data.repair_cycles >= self.config.max_repair_cycles:
            await self.event_bus.publish(
                RecoveryEvent(
                    EventName.RECOVERY_EXHAUSTED,
                    session_id,
                    {
                        "error_code": "verification_failure",
                        "repair_cycles": data.repair_cycles,
                    },
                )
            )
            raise VerificationFailure(
                f"Verification still failing after {data.repair_cycles} repair cycle(s): "
                f"{verification.summary}"
            )
        data.repair_cycles += 1
        data.working = data.working.noting(
            decisions=(f"Repair cycle {data.repair_cycles}: {verification.summary}",),
            remaining_work=("Make the failing verification checks pass.",),
        )
        await self.event_bus.publish(
            RecoveryEvent(
                EventName.RECOVERY_ACTION,
                session_id,
                {
                    "action": RecoveryAction.RETURN_EVIDENCE.value,
                    "repair_cycle": data.repair_cycles,
                },
            )
        )
        data.history.append(
            ModelMessage(
                ModelRole.USER,
                "Your change did not pass verification. Do not weaken, skip or delete "
                "any check. Fix the underlying problem and finish again.\n\n"
                + evidence_digest(verification),
            )
        )
        return None

    async def _complete_run(
        self,
        response: ModelResponse,
        data: _RunData,
        budget: RuntimeBudget,
        verification: VerificationResult,
    ) -> AgentRunResult:
        data.session = self._with_budget(data.session, budget, AgentStatus.COMPLETED)
        data.session = replace(
            data.session,
            attributes={
                **data.session.attributes,
                "working_state": data.working.to_json(),
            },
        )
        await self.event_bus.publish(
            AgentEvent(
                EventName.AGENT_COMPLETED,
                data.session.session_id,
                {
                    "iterations": budget.usage.iterations,
                    "tool_calls": budget.usage.tool_calls,
                    "repair_cycles": data.repair_cycles,
                    "verification": verification.summary,
                },
            )
        )
        return AgentRunResult(
            AgentRunStatus.COMPLETED,
            data.session,
            answer=response.content,
            tool_call_ids=tuple(data.seen_call_ids),
            verification=verification,
            working_state=data.working,
        )

    @staticmethod
    def _compact(request: ModelRequest) -> ModelRequest:
        """Drop the middle of the conversation, keeping the framing and the latest turns."""
        messages = request.messages
        if len(messages) <= 4:
            return request
        return replace(request, messages=(*messages[:2], *messages[-2:]))

    @staticmethod
    def _record_tool_use(working: WorkingState, call: ModelToolCall) -> WorkingState:
        """Operational state comes from the call itself, not from re-reading the chat."""
        arguments = call.arguments
        path = arguments.get("path")
        if call.name in ("write_file", "edit_file") and isinstance(path, str):
            return working.modifying(files_modified=(path,))
        if call.name == "bash":
            command = arguments.get("command")
            if isinstance(command, str):
                return working.ran(command)
        if isinstance(path, str):
            return working.observing(files_examined=(path,))
        return working

    @staticmethod
    def _tool_message(call: ModelToolCall, payload: JSONObject) -> ModelMessage:
        return ModelMessage(
            ModelRole.TOOL,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            name=call.name,
            tool_call_id=call.call_id,
        )

    @staticmethod
    def _remember_paths(call: ModelToolCall, data: _RunData) -> None:
        for key in ("path", "glob", "pattern"):
            value = call.arguments.get(key)
            if isinstance(value, str) and "*" not in value and "?" not in value:
                data.discovered_paths.add(value)

    @staticmethod
    def _with_budget(
        session: SessionState,
        budget: RuntimeBudget,
        status: AgentStatus,
    ) -> SessionState:
        return replace(
            session,
            agent=replace(
                session.agent,
                status=status,
                budget=BudgetState(
                    max_steps=budget.limits.max_iterations,
                    used_steps=budget.usage.iterations,
                ),
            ),
            updated_at=datetime.now(UTC),
        )

    @staticmethod
    def _set_status(
        session: SessionState,
        status: AgentStatus,
        error_code: str | None = None,
    ) -> SessionState:
        return replace(
            session,
            agent=replace(
                session.agent,
                status=status,
                active_model_request_id=None,
                active_tool_call_ids=(),
                last_error_code=error_code,
            ),
            updated_at=datetime.now(UTC),
        )
