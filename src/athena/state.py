"""Structured operational state; conversation text is not runtime state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from athena.types import JSONObject


class ExecutionOutcome(StrEnum):
    """How any unit of work ended, at any level of the hierarchy.

    One vocabulary for a run, a task, a subagent and a tool, because a caller composing
    them should not have to learn four. `CANCELLED` is deliberately not a kind of
    `FAILED`: nothing went wrong when someone asked to stop, and reporting it as a failure
    sends people looking for a bug that is not there. `TIMED_OUT` is separated from both
    for the same reason — a limit that was reached is a fact about the limit.
    """

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"

    @property
    def is_success(self) -> bool:
        return self is ExecutionOutcome.COMPLETED

    @property
    def is_stopped_deliberately(self) -> bool:
        """True when nothing went wrong — the work was stopped, not broken."""
        return self in (ExecutionOutcome.CANCELLED, ExecutionOutcome.TIMED_OUT)


def classify_outcome(error: BaseException | None) -> ExecutionOutcome:
    """Turn whatever ended a piece of work into one outcome.

    This is the single place that decides. Before it existed the same
    `except (CancellationError, ProcessCancelledError)` pair appeared in the loop twice and
    in the recovery policy once, and a fourth site would have been added for every new
    level of the hierarchy — each an opportunity to get it subtly different.
    """
    from athena.errors import (
        CancellationError,
        ProcessCancelledError,
        ProcessTimeoutError,
    )

    if error is None:
        return ExecutionOutcome.COMPLETED
    if isinstance(error, ProcessTimeoutError):
        return ExecutionOutcome.TIMED_OUT
    if isinstance(error, (CancellationError, ProcessCancelledError)):
        # A cancellation raised because a deadline passed is a timeout wearing a
        # cancellation's clothes; the token knows which and the exception does not.
        details = getattr(error, "details", {}) or {}
        if details.get("reason") == "timed_out":
            return ExecutionOutcome.TIMED_OUT
        return ExecutionOutcome.CANCELLED
    return ExecutionOutcome.FAILED


class AgentStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING_PERMISSION = "waiting_permission"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    #: The process died while this session was live. It is not finished, and it is not
    #: running either; a human or a resume must decide what happens next.
    RECOVERY_PENDING = "recovery_pending"


@dataclass(frozen=True, slots=True)
class BudgetState:
    max_steps: int
    used_steps: int = 0
    max_model_tokens: int | None = None
    used_model_tokens: int = 0


@dataclass(frozen=True, slots=True)
class AgentState:
    status: AgentStatus = AgentStatus.IDLE
    active_model_request_id: str | None = None
    active_tool_call_ids: tuple[str, ...] = ()
    last_error_code: str | None = None
    budget: BudgetState = field(default_factory=lambda: BudgetState(max_steps=1))


@dataclass(frozen=True, slots=True)
class SessionState:
    session_id: str
    workspace_id: str
    agent: AgentState = field(default_factory=AgentState)
    attributes: JSONObject = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
