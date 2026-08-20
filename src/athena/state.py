"""Structured operational state; conversation text is not runtime state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from athena.types import JSONObject


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
