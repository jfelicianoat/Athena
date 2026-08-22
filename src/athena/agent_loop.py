"""Explicit provider-neutral orchestration loop for read-only investigation."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from athena.async_utils import await_cancellable
from athena.budget import BudgetLimits, RuntimeBudget
from athena.cancellation import CancellationToken
from athena.concurrency import ConcurrencyScheduler
from athena.context import ContextBuilder
from athena.diagnosis import diagnose_result
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
from athena.hooks import (
    HookBlockedError,
    HookContext,
    HookEvent,
    HookRegistry,
)
from athena.memory import CompactionReport, ContextWindowManager, ConversationContext
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
from athena.session_store import (
    EventCheckpoint,
    SessionRecord,
    SessionStore,
    SessionStoreError,
)
from athena.skills import SkillRegistry, SkillSelection, render_skills
from athena.state import (
    AgentState,
    AgentStatus,
    BudgetState,
    SessionState,
    classify_outcome,
)
from athena.tool_executor import ToolExecutor
from athena.tool_search import TOOL_SEARCH_NAME
from athena.tools import Tool, ToolResult, ToolResultReference
from athena.types import JSONObject, JSONValue
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
    require_workspace_change: bool = False


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
    references: list[object] = field(default_factory=list)
    checkpoints: list[EventCheckpoint] = field(default_factory=list)
    pending_compaction: CompactionReport | None = None
    revealed_tools: set[str] = field(default_factory=set)
    skills: tuple[SkillSelection, ...] = ()


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
        session_store: SessionStore | None = None,
        context_window: ContextWindowManager | None = None,
        hooks: HookRegistry | None = None,
        skills: SkillRegistry | None = None,
        config: AgentLoopConfig | None = None,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.scheduler = ConcurrencyScheduler()
        self.executor = executor
        self.context_builder = context_builder
        self.event_bus = event_bus
        self.verification = verification or LoopCompletionVerificationPolicy()
        self.session_store = session_store
        self.context_window = context_window or ContextWindowManager()
        self.hooks = hooks or HookRegistry()
        self.skills = skills or SkillRegistry()
        self.config = config or AgentLoopConfig()
        self.recovery = recovery or RecoveryPolicy(
            RecoveryLimits(
                model_retries=self.config.max_model_retries,
                model_backoff_seconds=self.config.retry_backoff_seconds,
            )
        )

    async def resume(
        self,
        session_id: str,
        workspace: Workspace,
        cancellation: CancellationToken,
    ) -> AgentRunResult:
        """Continue an interrupted session from stored working memory alone.

        No transcript is replayed. Everything the run needs was persisted as structured
        state, which is the point of keeping it out of the chat in the first place.
        """
        if self.session_store is None:
            raise SessionStoreError("Resuming requires a session store")
        record = await self.session_store.load(session_id)
        if record is None:
            raise SessionStoreError(f"Unknown session: {session_id}")
        if not record.resumable:
            raise SessionStoreError(
                f"Session {session_id} is {record.status.value}, not recovery_pending"
            )
        await self.event_bus.publish(
            AgentEvent(
                EventName.SESSION_RESUMED,
                session_id,
                {
                    "objective": record.working_memory.objective,
                    "degraded": record.degraded,
                    "files_modified": list(record.working_memory.files_modified),
                },
            )
        )
        return await self.run(
            record.working_memory.objective, workspace, cancellation, resume_from=record
        )

    async def run(
        self,
        objective: str,
        workspace: Workspace,
        cancellation: CancellationToken,
        *,
        resume_from: SessionRecord | None = None,
        session_id: str | None = None,
    ) -> AgentRunResult:
        # An external caller may name the run before it starts. Without this a service
        # cannot address a run until the loop has already emitted events about it.
        if resume_from is not None:
            session_id = resume_from.session_id
        elif session_id is None:
            session_id = str(uuid4())
        initial_agent = AgentState(
            AgentStatus.RUNNING,
            budget=BudgetState(max_steps=self.config.max_iterations),
        )
        working = (
            resume_from.working_memory
            if resume_from is not None
            else WorkingState(objective=objective)
        )
        data = _RunData(
            SessionState(session_id, workspace.workspace_id, initial_agent),
            working,
        )
        if resume_from is not None:
            data.references.extend(resume_from.tool_references)
            data.checkpoints.extend(resume_from.checkpoints)
        await self.event_bus.publish(
            AgentEvent(
                EventName.AGENT_STARTED,
                session_id,
                {"objective": objective, "resumed": resume_from is not None},
            )
        )
        await self._persist(data, workspace, AgentStatus.RUNNING, "started")
        self._select_skills(objective, data)
        await self._hook(
            HookEvent.SESSION_START,
            session_id,
            {
                "objective": objective,
                "resumed": resume_from is not None,
                "skills": [selection.skill.name for selection in data.skills],
            },
        )
        try:
            return await asyncio.wait_for(
                self._iterate(objective, workspace, cancellation, data),
                timeout=self.config.session_timeout_seconds,
            )
        except TimeoutError:
            timeout = ProcessTimeoutError("Agent session timed out")
            failed = self._set_status(data.session, AgentStatus.FAILED, timeout.code)
            await self._persist(
                data, workspace, AgentStatus.FAILED, "failed", {"error_code": timeout.code}
            )
            await self.event_bus.publish(
                AgentEvent(
                    EventName.AGENT_FAILED,
                    session_id,
                    {"error_code": timeout.code, "message": timeout.message},
                )
            )
            await self._finish(data, "failed", timeout.code, timeout.message)
            return AgentRunResult(
                AgentRunStatus.FAILED,
                failed,
                error=timeout,
                tool_call_ids=tuple(data.seen_call_ids),
            )
        except (CancellationError, ProcessCancelledError) as exc:
            # Classified rather than assumed: a cancellation raised because a deadline
            # passed is a timeout, and telling the person their work was abandoned when a
            # limit they set was reached is the wrong story.
            outcome = classify_outcome(exc)
            cancelled = self._set_status(data.session, AgentStatus.CANCELLED, exc.code)
            await self._persist(
                data, workspace, AgentStatus.CANCELLED, "cancelled", {"error_code": exc.code}
            )
            await self.event_bus.publish(
                AgentEvent(
                    EventName.AGENT_CANCELLED,
                    session_id,
                    {
                        "error_code": exc.code,
                        "message": exc.message,
                        "outcome": outcome.value,
                    },
                )
            )
            await self._finish(data, "cancelled", exc.code, exc.message)
            return AgentRunResult(
                AgentRunStatus.CANCELLED,
                cancelled,
                error=exc,
                tool_call_ids=tuple(data.seen_call_ids),
            )
        except AthenaRuntimeError as exc:
            failed = self._set_status(data.session, AgentStatus.FAILED, exc.code)
            await self._persist(
                data, workspace, AgentStatus.FAILED, "failed", {"error_code": exc.code}
            )
            await self.event_bus.publish(
                AgentEvent(
                    EventName.AGENT_FAILED,
                    session_id,
                    {"error_code": exc.code, "message": exc.message},
                )
            )
            await self._finish(data, "failed", exc.code, exc.message)
            return AgentRunResult(
                AgentRunStatus.FAILED,
                failed,
                error=exc,
                tool_call_ids=tuple(data.seen_call_ids),
            )
        except Exception as exc:
            # Keep the original: an unclassified failure is hard enough to diagnose
            # without the runtime throwing away what actually went wrong.
            fatal = FatalRuntimeError(
                f"Unexpected runtime failure: {type(exc).__name__}: {exc}",
                details={"exception_type": type(exc).__name__, "detail": str(exc)},
            )
            fatal.__cause__ = exc
            failed = self._set_status(data.session, AgentStatus.FAILED, fatal.code)
            await self._persist(
                data, workspace, AgentStatus.FAILED, "failed", {"error_code": fatal.code}
            )
            await self.event_bus.publish(
                AgentEvent(
                    EventName.AGENT_FAILED,
                    session_id,
                    {"error_code": fatal.code, "message": fatal.message},
                )
            )
            await self._finish(data, "failed", fatal.code, fatal.message)
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
            history = self._select_context(data)
            if data.pending_compaction is not None:
                report = data.pending_compaction
                data.pending_compaction = None
                await self.event_bus.publish(
                    AgentEvent(
                        EventName.CONTEXT_COMPACTED,
                        data.session.session_id,
                        {
                            "messages_before": report.messages_before,
                            "messages_after": report.messages_after,
                            "chars_before": report.chars_before,
                            "chars_after": report.chars_after,
                            "reasons": list(report.reasons),
                        },
                    )
                )
            request = await self.context_builder.build_request(
                objective=objective,
                history=history,
                important_state={
                    "iteration": iteration,
                    "tool_calls": budget.usage.tool_calls,
                    "repair_cycle": data.repair_cycles,
                    "working_state": data.working.summary(),
                    "skills": render_skills(data.skills),
                    "deferred_tools_available": len(self.registry.deferred_names()),
                },
                tool_definitions=self.registry.definitions(data.revealed_tools),
                cancellation=cancellation,
                discovered_paths=tuple(sorted(data.discovered_paths)),
            )
            if self.config.require_workspace_change and not data.working.files_modified:
                request = replace(
                    request,
                    options={**dict(request.options), "tool_choice": "required"},
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
                # Checkpoint here: the agent has just changed the world, and a crash
                # before the next turn must not lose the record of what it changed.
                await self._persist(
                    data,
                    workspace,
                    AgentStatus.RUNNING,
                    "tool_calls",
                    {
                        "iteration": iteration,
                        "tool_calls": [call.name for call in response.tool_calls],
                    },
                )
                continue
            completed = await self._attempt_completion(
                response, data, workspace, cancellation, budget
            )
            if completed is not None:
                return completed
            # Only checkpoint an ongoing run: a terminal state was already written.
            await self._persist(
                data,
                workspace,
                AgentStatus.RUNNING,
                "iteration",
                {"iteration": iteration, "repair_cycles": data.repair_cycles},
            )
        raise BudgetExceededError("Maximum agent iterations reached without completion")

    async def _hook(self, event: HookEvent, session_id: str, payload: JSONObject) -> None:
        """Extensions may refuse an action. They can never authorize one."""
        report = await self.hooks.run(HookContext(event, session_id, payload))
        if report.blocked:
            raise HookBlockedError(
                f"{event.value} blocked by {report.blocked_by}: {report.reason}",
                details={"event": event.value, "hook": report.blocked_by},
            )

    async def _finish(
        self,
        data: _RunData,
        outcome: str,
        error_code: str | None = None,
        message: str = "",
    ) -> None:
        """Announce the end of the run, and any typed error that ended it."""
        session_id = data.session.session_id
        if error_code is not None:
            await self._hook_quietly(
                HookEvent.ON_ERROR,
                session_id,
                {"error_code": error_code, "message": message, "outcome": outcome},
            )
        await self._hook_quietly(
            HookEvent.SESSION_END,
            session_id,
            {
                "outcome": outcome,
                "error_code": error_code,
                "files_modified": list(data.working.files_modified),
                "repair_cycles": data.repair_cycles,
            },
        )

    async def _hook_quietly(self, event: HookEvent, session_id: str, payload: JSONObject) -> None:
        """Terminal notifications: a refusal here has nothing left to refuse."""
        await self.hooks.run(HookContext(event, session_id, payload))

    def _select_skills(self, objective: str, data: _RunData) -> None:
        """Skills describe how to work. They never add a tool or widen a permission."""
        data.skills = self.skills.select(objective, self.registry.names())
        if data.skills:
            data.working = data.working.noting(
                decisions=tuple(
                    f"Following skill {selection.skill.name} v{selection.skill.version}"
                    for selection in data.skills
                )
            )

    async def _persist(
        self,
        data: _RunData,
        workspace: Workspace,
        status: AgentStatus,
        checkpoint: str,
        payload: JSONObject | None = None,
    ) -> None:
        """Write the session out. Losing power must not lose what Athena learned."""
        if self.session_store is None:
            return
        data.checkpoints.append(EventCheckpoint(checkpoint, payload or {}))
        record = SessionRecord(
            session_id=data.session.session_id,
            workspace_id=workspace.workspace_id,
            status=status,
            working_memory=data.working,
            tool_references=tuple(
                reference
                for reference in data.references
                if isinstance(reference, ToolResultReference)
            ),
            verification=dict(data.working.verification),
            checkpoints=tuple(data.checkpoints[-50:]),
            created_at=data.session.created_at,
        )
        await self.session_store.save(record)
        await self.event_bus.publish(
            AgentEvent(
                EventName.SESSION_PERSISTED,
                data.session.session_id,
                {"status": status.value, "checkpoint": checkpoint},
            )
        )

    def _select_context(self, data: _RunData) -> tuple[ModelMessage, ...]:
        """Choose what to send. The durable facts are re-rendered from working memory."""
        selected, report = self.context_window.select(
            ConversationContext(tuple(data.history)), data.working
        )
        if report is not None and report.changed:
            data.history = list(selected.messages)
            data.compactions += 1
            data.pending_compaction = report
        return selected.messages

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
                        # El proveedor los cuenta y hasta aquí se perdían: el adaptador
                        # los ponía en la respuesta y el evento no los llevaba, así que
                        # toda medición de tokens salía a cero pareciendo un dato.
                        "input_tokens": response.usage.input_tokens,
                        "output_tokens": response.usage.output_tokens,
                        # El modelo que contestó de verdad, que con un router por delante
                        # no tiene por qué ser el que se pidió.
                        "model": response.model,
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
        """Run a turn's calls, overlapping only the ones that said they may overlap.

        Three passes, and the split is what makes overlapping safe. Admission is
        sequential because it mutates the run — budget, seen ids, discovered paths — and
        two coroutines doing that concurrently would lose updates silently. Execution is
        the only part that overlaps. Recording is sequential again, and the transcript is
        assembled in the order the model asked, so it reads the same whatever order the
        work finished in.

        `ConcurrencyScheduler` decides the waves and is deliberately unpersuadable: a wave
        holds more than one call only when *both* tools declared themselves safe to run
        alongside another and their resources do not intersect. Anything unknown gets a
        wave of its own, which is the behaviour this loop had when it could not overlap
        anything at all.

        Results are held by position rather than by call id. Ids arrive from the model and
        can be empty or repeated — that is the first thing admission checks — so a map
        keyed by id would let a refused duplicate overwrite the answer of the call it
        duplicated.
        """
        payloads: list[JSONObject | None] = [None] * len(calls)
        admitted: list[tuple[int, ModelToolCall, Tool | None]] = []
        for index, call in enumerate(calls):
            cancellation.raise_if_cancelled()
            refusal = await self._admit(call, data, budget)
            if refusal is not None:
                payloads[index] = refusal
                continue
            try:
                tool: Tool | None = self.registry.get(call.name)
            except AthenaRuntimeError:
                # An unknown name is the executor's error to report, with its own code and
                # its own event. Claiming it here would duplicate that in a worse form.
                tool = None
            admitted.append((index, call, tool))

        for wave in self._waves(admitted):
            cancellation.raise_if_cancelled()
            data.session = replace(
                data.session,
                agent=replace(
                    data.session.agent,
                    active_tool_call_ids=tuple(call.call_id for _, call, _ in wave),
                ),
            )
            try:
                results = await asyncio.gather(
                    *(
                        self.executor.execute(
                            call,
                            session_id=data.session.session_id,
                            workspace=workspace,
                            cancellation=cancellation,
                        )
                        for _, call, _ in wave
                    ),
                    return_exceptions=True,
                )
            finally:
                data.session = replace(
                    data.session,
                    agent=replace(data.session.agent, active_tool_call_ids=()),
                )
            for (index, call, _), outcome in zip(wave, results, strict=True):
                payloads[index] = await self._record(call, outcome, data)

        for call, payload in zip(calls, payloads, strict=True):
            if payload is not None:
                data.history.append(self._tool_message(call, payload))

    def _waves(
        self, admitted: Sequence[tuple[int, ModelToolCall, Tool | None]]
    ) -> tuple[tuple[tuple[int, ModelToolCall, Tool | None], ...], ...]:
        """Group admitted calls into waves that may run together.

        A call whose tool could not be resolved runs alone, first: nothing is known about
        what it touches, and the cautious reading of "unknown" is "everything".
        """
        alone = [(entry,) for entry in admitted if entry[2] is None]
        schedulable = [entry for entry in admitted if entry[2] is not None]
        if not schedulable:
            return tuple(alone)
        by_id = {call.call_id: entry for entry in schedulable for _, call, _ in (entry,)}
        planned = self.scheduler.plan_calls(
            [(call.call_id, tool, call.arguments) for _, call, tool in schedulable if tool]
        )
        waves = [tuple(by_id[call_id] for call_id in batch.call_ids) for batch in planned]
        return tuple(alone) + tuple(waves)

    async def _admit(
        self, call: ModelToolCall, data: _RunData, budget: RuntimeBudget
    ) -> JSONObject | None:
        """Take a call into the turn, or refuse it. Returns the refusal payload."""
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
            return error
        data.seen_call_ids.add(call.call_id)
        budget.consume_tool_call()
        self._remember_paths(call, data)
        return None

    async def _record(
        self, call: ModelToolCall, outcome: ToolResult | BaseException, data: _RunData
    ) -> JSONObject:
        """Turn one finished call into what the model will be told about it.

        Sequential by construction: it mutates the run's working state, and the whole
        point of separating it from execution is that two of these never interleave.
        """
        try:
            if isinstance(outcome, BaseException):
                raise outcome
            # Record what happened, never what was merely attempted: a refused write that
            # still showed up in files_modified would make the working state lie to
            # verification, to recovery and to whoever reads the session later.
            data.working = self._record_tool_use(data.working, call)
            if outcome.reference is not None:
                data.references.append(outcome.reference)
            if call.name == TOOL_SEARCH_NAME:
                self._reveal(outcome.output, data)
            return {
                "ok": True,
                "call_id": outcome.call_id,
                "output": outcome.output,
                "reference_uri": outcome.reference.uri if outcome.reference else None,
            }
        except (CancellationError, ProcessCancelledError):
            # Being stopped is not a tool failure, so it does not go to the recovery
            # policy and does not become a recorded error against the task.
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
            return {
                "ok": False,
                "call_id": call.call_id,
                "recovery": directive.action.value,
                "error": {"code": exc.code, "message": exc.message, "details": exc.details},
            }

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
        if self.config.require_workspace_change and not data.working.files_modified:
            data.working = data.working.noting(
                decisions=(
                    "A final response was refused because the objective requires a workspace "
                    "change and no file has been modified.",
                ),
                remaining_work=(
                    "Use an offered workspace mutation tool and confirm its successful result.",
                ),
            )
            data.history.append(
                ModelMessage(
                    ModelRole.USER,
                    "The objective explicitly requires creating or modifying a file, but no "
                    "workspace file has changed. Do not return a final answer yet. Use one of "
                    "Athena's offered write or edit tools with the required content, wait for "
                    "its result, and only then finish.",
                )
            )
            await self.event_bus.publish(
                RecoveryEvent(
                    EventName.RECOVERY_ACTION,
                    data.session.session_id,
                    {
                        "action": "require_workspace_change",
                        "reason": "No successful file mutation was observed.",
                    },
                )
            )
            return None
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
        await self._hook(
            HookEvent.PRE_VERIFY,
            data.session.session_id,
            {"files_modified": list(data.working.files_modified)},
        )
        verification = await await_cancellable(
            self.verification.verify(data.session, workspace, cancellation),
            cancellation,
        )
        await self._hook_quietly(
            HookEvent.POST_VERIFY,
            data.session.session_id,
            {
                "status": verification.status.value,
                "summary": verification.summary,
                "evidence_count": len(verification.evidence),
            },
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
            return await self._complete_run(response, data, workspace, budget, verification)
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
        # Read the failure before asking anyone to fix it. A wall of pytest output is a
        # lot to hand a small model; telling it which *kind* of problem this is turns the
        # next cycle from a guess into a direction.
        diagnosis = diagnose_result(verification)
        if not diagnosis.is_worth_repairing:
            # A missing package or a full disk will not be fixed by editing code, and
            # spending a cycle letting the model try is how a run burns its budget looking
            # busy. Stop and say what is actually wrong.
            await self.event_bus.publish(
                RecoveryEvent(
                    EventName.RECOVERY_EXHAUSTED,
                    session_id,
                    {
                        "error_code": "verification_failure",
                        "repair_cycles": data.repair_cycles,
                        "diagnosis": diagnosis.kind.value,
                    },
                )
            )
            raise VerificationFailure(
                f"{diagnosis.summary} No repair cycle can address this: {diagnosis.guidance}"
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
                    "diagnosis": diagnosis.kind.value,
                },
            )
        )
        data.history.append(
            ModelMessage(
                ModelRole.USER,
                "Your change did not pass verification. Do not weaken, skip or delete "
                "any check. Fix the underlying problem and finish again.\n\n"
                + diagnosis.render()
                + "\n\n"
                + evidence_digest(verification),
            )
        )
        return None

    async def _complete_run(
        self,
        response: ModelResponse,
        data: _RunData,
        workspace: Workspace,
        budget: RuntimeBudget,
        verification: VerificationResult,
    ) -> AgentRunResult:
        data.session = self._with_budget(data.session, budget, AgentStatus.COMPLETED)
        await self._persist(
            data,
            workspace,
            AgentStatus.COMPLETED,
            "completed",
            {"verification": verification.status.value},
        )
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
        await self._finish(data, "completed")
        return AgentRunResult(
            AgentRunStatus.COMPLETED,
            data.session,
            answer=response.content,
            tool_call_ids=tuple(data.seen_call_ids),
            verification=verification,
            working_state=data.working,
        )

    @staticmethod
    def _reveal(output: JSONValue, data: _RunData) -> None:
        """A searched-for tool becomes visible on the next turn, and only then."""
        if not isinstance(output, dict):
            return
        revealed = output.get("revealed")
        if not isinstance(revealed, list):
            return
        data.revealed_tools.update(name for name in revealed if isinstance(name, str))

    @staticmethod
    def _compact(request: ModelRequest) -> ModelRequest:
        """Drop the middle of the conversation, keeping the framing and the latest turns."""
        messages = request.messages
        if len(messages) <= 4:
            return request
        return replace(request, messages=(*messages[:2], *messages[-2:]))

    @staticmethod
    def _record_tool_use(working: WorkingState, call: ModelToolCall) -> WorkingState:
        """Operational state comes from the call itself, not from re-reading the chat.

        Only ever called after the call succeeded, so the record is of fact, not intent.
        """
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
