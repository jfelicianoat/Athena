"""Deterministic fakes for contract and downstream integration tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable

from athena.cancellation import CancellationToken
from athena.errors import ModelStreamingUnsupportedError
from athena.events import EventName, ModelEvent
from athena.models import (
    ModelCapabilities,
    ModelHealth,
    ModelHealthStatus,
    ModelProvider,
    ModelRequest,
    ModelResponse,
)
from athena.permissions import PermissionDecision, PermissionRequest


class FakeModelProvider(ModelProvider):
    def __init__(
        self,
        responses: Iterable[ModelResponse],
        *,
        supports_streaming: bool = False,
    ) -> None:
        self._responses = iter(responses)
        self._supports_streaming = supports_streaming
        self.requests: list[ModelRequest] = []

    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        cancellation.raise_if_cancelled()
        self.requests.append(request)
        return next(self._responses)

    async def stream(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> AsyncIterator[ModelEvent]:
        if not self._supports_streaming:
            raise ModelStreamingUnsupportedError("Fake provider does not support streaming")
        response = await self.complete(request, cancellation)
        yield ModelEvent(
            name=EventName.MODEL_COMPLETED,
            session_id="fake",
            payload={"delta": response.content, "finish_reason": response.finish_reason},
        )

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            streaming=self._supports_streaming,
            tool_calls=True,
            structured_output=True,
        )

    async def health(self, cancellation: CancellationToken) -> ModelHealth:
        cancellation.raise_if_cancelled()
        return ModelHealth(ModelHealthStatus.HEALTHY)


class ScriptedPermissionPrompt:
    """Answers ASK prompts from a fixed script and records what it was asked.

    Anything beyond the script is denied: an exhausted script must never become an
    implicit approval.
    """

    def __init__(self, answers: Iterable[PermissionDecision] = ()) -> None:
        self._answers = iter(answers)
        self.requests: list[PermissionRequest] = []

    async def confirm(self, request: PermissionRequest) -> PermissionDecision:
        self.requests.append(request)
        return next(self._answers, PermissionDecision.DENY)
