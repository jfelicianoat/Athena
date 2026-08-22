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
    #: Una garantía que hacía falta y el proveedor no da. Se publica también
    #: cuando lo que falta era sólo preferible, con `required: false`: no impide
    #: nada, y saber que se trabajó sin ello explica resultados peores.
    CAPABILITY_MISSING = "capability.missing"
    TOOL_STARTED = "tool.started"
    TOOL_PROGRESS = "tool.progress"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    PERMISSION_REQUESTED = "permission.requested"
    PERMISSION_RESOLVED = "permission.resolved"
    VERIFICATION_STARTED = "verification.started"
    VERIFICATION_CHECK_STARTED = "verification.check.started"
    VERIFICATION_CHECK_COMPLETED = "verification.check.completed"
    VERIFICATION_FAILED = "verification.failed"
    VERIFICATION_COMPLETED = "verification.completed"
    #: Which shape a run will execute in, and why. Published once, when the answer is
    #: final — which for a goal that was planned is after the plan came back, because a
    #: plan can turn out not to be worth executing as one.
    PLAN_DECIDED = "plan.decided"
    #: The graph level, which is a real level and not a synonym for the run. A task uses a
    #: subagent; it is not one, and a view that conflated them could not draw the plan.
    GRAPH_STARTED = "graph.started"
    #: Qué puede empezar ahora. Se publica una vez por ola, antes de lanzarla, así que
    #: quien mira ve el ancho real del trabajo en vez de deducirlo de tareas sueltas.
    GRAPH_FRONTIER_READY = "graph.frontier.ready"
    GRAPH_COMPLETED = "graph.completed"
    GRAPH_FAILED = "graph.failed"
    GRAPH_CANCELLED = "graph.cancelled"
    #: Admitida en una ola y todavía sin empezar. Separado de `task.started` porque el
    #: hueco entre las dos es donde una tarea espera a su turno, y sin decirlo esa espera
    #: parece que no ocurre.
    TASK_SCHEDULED = "task.scheduled"
    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    #: No puede empezar porque algo de lo que depende falló. No es un fallo suyo, y
    #: contarlo como tal culparía a la tarea equivocada.
    TASK_BLOCKED = "task.blocked"
    SUBAGENT_STARTED = "subagent.started"
    SUBAGENT_COMPLETED = "subagent.completed"
    SUBAGENT_FAILED = "subagent.failed"
    SUBAGENT_CANCELLED = "subagent.cancelled"
    CONTEXT_COMPACTED = "context.compacted"
    SESSION_PERSISTED = "session.persisted"
    SESSION_RESUMED = "session.resumed"
    RECOVERY_STARTED = "recovery.started"
    RECOVERY_ACTION = "recovery.action"
    RECOVERY_EXHAUSTED = "recovery.exhausted"
    FILE_CHANGED = "file.changed"
    PROCESS_STARTED = "process.started"
    PROCESS_COMPLETED = "process.completed"
    PROCESS_FAILED = "process.failed"
    PROCESS_CANCELLED = "process.cancelled"


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


@dataclass(frozen=True, slots=True)
class FileEvent(RuntimeEvent):
    """Event in the file.* namespace, carrying the change evidence."""


@dataclass(frozen=True, slots=True)
class ProcessEvent(RuntimeEvent):
    """Event in the process.* namespace for child-process lifecycle."""


@dataclass(frozen=True, slots=True)
class SubagentEvent(RuntimeEvent):
    """Event in the subagent.* namespace. correlation_id carries the child session id."""


@dataclass(frozen=True, slots=True)
class RecoveryEvent(RuntimeEvent):
    """Event in the recovery.* namespace describing how a typed error was handled."""


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
