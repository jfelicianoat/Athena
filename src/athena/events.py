"""Structured runtime events and the in-process event bus."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, TypeAlias, runtime_checkable
from uuid import uuid4

from athena.security import redact_sensitive
from athena.types import JSONObject


class EventName(StrEnum):
    AGENT_STARTED = "agent.started"
    AGENT_COMPLETED = "agent.completed"
    AGENT_FAILED = "agent.failed"
    AGENT_CANCELLED = "agent.cancelled"
    MODEL_STARTED = "model.started"
    MODEL_COMPLETED = "model.completed"
    MODEL_FAILED = "model.failed"
    TOOL_STARTED = "tool.started"
    TOOL_PROGRESS = "tool.progress"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    PERMISSION_REQUESTED = "permission.requested"
    PERMISSION_RESOLVED = "permission.resolved"
    VERIFICATION_STARTED = "verification.started"
    VERIFICATION_COMPLETED = "verification.completed"
    FILE_CHANGED = "file.changed"


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    name: EventName
    session_id: str
    payload: JSONObject = field(default_factory=dict)
    correlation_id: str | None = None
    event_id: str = field(default_factory=lambda: str(uuid4()))
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class AgentEvent(RuntimeEvent):
    """Event in the agent.* namespace."""


@dataclass(frozen=True, slots=True)
class ModelEvent(RuntimeEvent):
    """Event in the model.* namespace, including stream deltas in payload."""


@dataclass(frozen=True, slots=True)
class ToolEvent(RuntimeEvent):
    """Event in the tool.* namespace."""


@dataclass(frozen=True, slots=True)
class PermissionEvent(RuntimeEvent):
    """Event in the permission.* namespace."""


@dataclass(frozen=True, slots=True)
class VerificationEvent(RuntimeEvent):
    """Event in the verification.* namespace."""


EventHandler: TypeAlias = Callable[[RuntimeEvent], Awaitable[None] | None]
Unsubscribe: TypeAlias = Callable[[], None]


@runtime_checkable
class EventBus(Protocol):
    async def publish(self, event: RuntimeEvent) -> None: ...

    def subscribe(
        self, handler: EventHandler, event_names: Collection[EventName] | None = None
    ) -> Unsubscribe: ...


class InMemoryEventBus:
    """Small ordered in-process bus suitable for runtime/UI decoupling."""

    def __init__(self) -> None:
        self._subscribers: dict[int, tuple[frozenset[EventName] | None, EventHandler]] = {}
        self._next_id = 0

    def subscribe(
        self, handler: EventHandler, event_names: Collection[EventName] | None = None
    ) -> Unsubscribe:
        subscription_id = self._next_id
        self._next_id += 1
        names = None if event_names is None else frozenset(event_names)
        self._subscribers[subscription_id] = (names, handler)

        def unsubscribe() -> None:
            self._subscribers.pop(subscription_id, None)

        return unsubscribe

    async def publish(self, event: RuntimeEvent) -> None:
        redacted = redact_sensitive(event.payload)
        if not isinstance(redacted, dict):
            raise TypeError("Redacted event payload must remain an object")
        safe_event = replace(event, payload=redacted)
        for names, handler in tuple(self._subscribers.values()):
            if names is not None and safe_event.name not in names:
                continue
            result = handler(safe_event)
            if inspect.isawaitable(result):
                await result
