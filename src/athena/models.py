"""Provider-neutral model inference contracts."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from athena.cancellation import CancellationToken
from athena.events import ModelEvent
from athena.types import JSONObject, JSONValue


class ModelRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class ModelMessage:
    role: ModelRole
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[ModelToolCall, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelToolCall:
    call_id: str
    name: str
    arguments: JSONObject


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    streaming: bool
    tool_calls: bool
    structured_output: bool
    max_context_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ModelRequest:
    messages: tuple[ModelMessage, ...]
    model: str | None = None
    tools: tuple[JSONObject, ...] = ()
    response_schema: JSONObject | None = None
    options: Mapping[str, JSONValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class ModelResponse:
    content: str
    model: str
    finish_reason: str
    tool_calls: tuple[ModelToolCall, ...] = ()
    usage: ModelUsage = field(default_factory=ModelUsage)
    raw_metadata: JSONObject = field(default_factory=dict)


class ModelHealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ModelHealth:
    status: ModelHealthStatus
    detail: str | None = None


@runtime_checkable
class ModelProvider(Protocol):
    """The only inference boundary visible to Athena's runtime."""

    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse: ...

    def stream(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> AsyncIterator[ModelEvent]: ...

    def capabilities(self) -> ModelCapabilities: ...

    async def health(self, cancellation: CancellationToken) -> ModelHealth: ...
