"""Deterministic fakes for contract and downstream integration tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable

from athena.cancellation import CancellationToken
from athena.channels import (
    ChannelIdentity,
    ChannelMessage,
    ChannelResponse,
    ResponseKind,
)
from athena.errors import AthenaRuntimeError, ModelPermanentError, ModelStreamingUnsupportedError
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
        try:
            return next(self._responses)
        except StopIteration:
            # Inside a coroutine a bare StopIteration becomes an opaque
            # "coroutine raised StopIteration"; say what actually ran out.
            raise ModelPermanentError(
                f"FakeModelProvider ran out of scripted responses after "
                f"{len(self.requests)} request(s)"
            ) from None

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


class FakeChannelAdapter:
    """A channel with no network, for testing everything that sits above one.

    It satisfies `ChannelAdapter` structurally rather than by inheritance, which is the
    point of the protocol: a real adapter will be a third-party thing that cannot subclass
    anything of Athena's, and if the fake could only work by subclassing, it would be
    testing a shape no real adapter has.

    Messages are scripted; `receive` hands them out in order and then reports the channel
    closed, so a gateway loop terminates instead of hanging a test. Everything sent back is
    kept in `delivered`.
    """

    def __init__(
        self,
        *,
        channel: str = "fake",
        inbound: Iterable[ChannelMessage] = (),
        fail_delivery: bool = False,
    ) -> None:
        self._channel = channel
        self._inbound: list[ChannelMessage] = list(inbound)
        self._fail_delivery = fail_delivery
        self.delivered: list[ChannelResponse] = []
        self.started = False
        self.stopped = False
        #: Set when the script runs out, so a test can tell "closed" from "still waiting".
        self.exhausted = asyncio.Event()

    @property
    def channel(self) -> str:
        return self._channel

    def push(self, text: str, identity: ChannelIdentity) -> None:
        """Queue one more inbound message, as if someone had just typed it."""
        self._inbound.append(ChannelMessage(identity, text))

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def receive(self) -> ChannelMessage | None:
        if not self._inbound:
            self.exhausted.set()
            return None
        return self._inbound.pop(0)

    async def deliver(self, response: ChannelResponse) -> None:
        if self._fail_delivery:
            # A channel that is down is an ordinary event, and it must not be able to take
            # the runtime with it. This is the failure a gateway has to survive.
            raise AthenaRuntimeError("The fake channel refused the delivery")
        self.delivered.append(response)

    def texts(self) -> list[str]:
        return [response.text for response in self.delivered]

    def of_kind(self, kind: ResponseKind) -> list[ChannelResponse]:
        return [response for response in self.delivered if response.kind is kind]
