"""Explicit provider-neutral orchestration loop for read-only investigation."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import NoReturn
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
    ModelPermanentError,
    ModelTransientError,
    ProcessTimeoutError,
    VerificationFailure,
)
from athena.events import (
    AgentEvent,
    EventBus,
    EventName,
    ModelEvent,
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
from athena.registry import ToolRegistry
from athena.state import AgentState, AgentStatus, BudgetState, SessionState
from athena.tool_executor import ToolExecutor
from athena.types import JSONObject
from athena.verification import LoopCompletionVerificationPolicy, VerificationPolicy
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


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    status: AgentRunStatus
    session: SessionState
    answer: str | None = None
    error: AthenaRuntimeError | None = None
    tool_call_ids: tuple[str, ...] = ()


@dataclass(slots=True)
class _RunData:
    session: SessionState
    history: list[ModelMessage] = field(default_factory=list)
    seen_call_ids: set[str] = field(default_factory=set)
    discovered_paths: set[str] = field(default_factory=set)


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
        config: AgentLoopConfig | None = None,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.executor = executor
        self.context_builder = context_builder
        self.event_bus = event_bus
        self.verification = verification or LoopCompletionVerificationPolicy()
        self.config = config or AgentLoopConfig()

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
        data = _RunData(SessionState(session_id, workspace.workspace_id, initial_agent))
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
        except CancellationError as exc:
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
                    "discovered_paths": sorted(data.discovered_paths)[-20:],
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
            return await self._complete_run(response, data, cancellation, budget)
        raise BudgetExceededError("Maximum agent iterations reached without completion")

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
        for attempt in range(self.config.max_model_retries + 1):
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
            except ModelTransientError as exc:
                await self.event_bus.publish(
                    ModelEvent(
                        EventName.MODEL_FAILED,
                        data.session.session_id,
                        {
                            "error_code": exc.code,
                            "retrying": attempt < self.config.max_model_retries,
                        },
                        request_id,
                    )
                )
                if attempt >= self.config.max_model_retries:
                    raise
                await await_cancellable(
                    asyncio.sleep(self.config.retry_backoff_seconds * (2**attempt)),
                    cancellation,
                )
                continue
            except ModelPermanentError as exc:
                await self.event_bus.publish(
                    ModelEvent(
                        EventName.MODEL_FAILED,
                        data.session.session_id,
                        {"error_code": exc.code, "retrying": False},
                        request_id,
                    )
                )
                raise
            except CancellationError as exc:
                await self.event_bus.publish(
                    ModelEvent(
                        EventName.MODEL_FAILED,
                        data.session.session_id,
                        {"error_code": exc.code, "retrying": False},
                        request_id,
                    )
                )
                raise
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
        self._unreachable()

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
            except AthenaRuntimeError as exc:
                payload = {
                    "ok": False,
                    "call_id": call.call_id,
                    "error": {"code": exc.code, "message": exc.message, "details": exc.details},
                }
            finally:
                data.session = replace(
                    data.session,
                    agent=replace(data.session.agent, active_tool_call_ids=()),
                )
            data.history.append(self._tool_message(call, payload))

    async def _complete_run(
        self,
        response: ModelResponse,
        data: _RunData,
        cancellation: CancellationToken,
        budget: RuntimeBudget,
    ) -> AgentRunResult:
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
            self.verification.verify(data.session, cancellation),
            cancellation,
        )
        await self.event_bus.publish(
            VerificationEvent(
                EventName.VERIFICATION_COMPLETED,
                data.session.session_id,
                {"status": verification.status.value, "evidence_count": len(verification.evidence)},
            )
        )
        if not verification.permits_completion:
            raise VerificationFailure(verification.summary)
        data.session = self._with_budget(data.session, budget, AgentStatus.COMPLETED)
        await self.event_bus.publish(
            AgentEvent(
                EventName.AGENT_COMPLETED,
                data.session.session_id,
                {"iterations": budget.usage.iterations, "tool_calls": budget.usage.tool_calls},
            )
        )
        return AgentRunResult(
            AgentRunStatus.COMPLETED,
            data.session,
            answer=response.content,
            tool_call_ids=tuple(data.seen_call_ids),
        )

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

    @staticmethod
    def _unreachable() -> NoReturn:
        raise FatalRuntimeError("Model retry loop ended unexpectedly")
