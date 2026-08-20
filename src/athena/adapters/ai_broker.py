"""AI_Broker as a `ModelProvider`, without Athena learning that it is one.

The broker is asynchronous by design: a task is submitted, queued, dispatched to whichever
model it decides on, and collected later. `ModelProvider.complete` is synchronous from the
loop's point of view. Bridging the two is this adapter's whole job — submit, poll, return —
and doing it here rather than in the loop is what keeps ADR-002 true: Athena runs perfectly
well without a broker, and the loop cannot tell whether it has one.

Two things this deliberately does not do.

**It does not choose models.** The broker routes; that is what it is for. Passing a
`preferred_model` through when the caller asked for one is the extent of it, and a second
router inside Athena would give two components an opinion about one decision.

**It does not retry.** `RecoveryPolicy` owns that, and it owns it for every provider at
once. An adapter with its own retry loop would produce a run whose total attempts nobody
can account for.

Polling is bounded and cancellable. A broker that stops answering ends the wait rather than
holding the run open until something else times out.
"""

from __future__ import annotations

import asyncio
import http.client
import json
from collections.abc import AsyncIterator, Mapping
from urllib.parse import urlsplit
from uuid import uuid4

from athena.cancellation import CancellationToken
from athena.errors import (
    ModelPermanentError,
    ModelStreamingUnsupportedError,
    ModelTransientError,
)
from athena.events import ModelEvent
from athena.models import (
    ModelCapabilities,
    ModelHealth,
    ModelHealthStatus,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ModelUsage,
)
from athena.types import JSONObject, JSONValue

#: How often to ask whether a task is done. Fast enough that a short answer feels
#: immediate, slow enough that a long one does not cost hundreds of requests.
_POLL_INTERVAL_SECONDS = 1.0

#: How long a single HTTP call may take. Not how long a task may take — that is the
#: caller's timeout, and conflating them would make a slow model look like a dead broker.
_REQUEST_TIMEOUT_SECONDS = 30.0

#: States the broker reports. Anything not listed is treated as still working, because a
#: state this adapter does not recognise is not evidence that the task ended.
_SUCCEEDED = frozenset({"succeeded", "completed", "done"})
_FAILED = frozenset({"failed", "error", "rejected"})
_CANCELLED = frozenset({"cancelled", "canceled", "expired", "timeout"})


class AiBrokerModelProvider(ModelProvider):
    """Submits one task per completion and waits for it, cancellably."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        preferred_model: str | None = None,
        poll_interval_seconds: float = _POLL_INTERVAL_SECONDS,
        max_wait_seconds: float = 600.0,
    ) -> None:
        parsed = urlsplit(base_url.rstrip("/"))
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("base_url must be an HTTP(S) URL")
        self._url = parsed
        self._host = parsed.hostname
        self._token = token
        self._preferred_model = preferred_model
        self._poll = max(0.1, poll_interval_seconds)
        self._max_wait = max_wait_seconds

    # -- the port ----------------------------------------------------------

    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        cancellation.raise_if_cancelled()
        task_id = await self._submit(request, cancellation)
        try:
            return await self._await_result(task_id, cancellation)
        except BaseException:
            # A task nobody is waiting for is a task the broker will still dispatch, and
            # pay for. Cancelling on the way out is the difference between abandoning a
            # request and leaking one.
            await self._cancel_task(task_id)
            raise

    async def stream(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> AsyncIterator[ModelEvent]:
        """Not offered. The broker's contract is submit-and-collect, not a token stream."""
        del request, cancellation
        raise ModelStreamingUnsupportedError("AI_Broker does not stream tokens")
        yield  # pragma: no cover - unreachable, and required to keep this a generator

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(streaming=False, tool_calls=False, structured_output=True)

    async def health(self, cancellation: CancellationToken) -> ModelHealth:
        cancellation.raise_if_cancelled()
        try:
            status, payload = await self._call("GET", "/health", None, cancellation)
        except ModelTransientError as error:
            return ModelHealth(ModelHealthStatus.UNAVAILABLE, str(error))
        if status != 200:
            return ModelHealth(ModelHealthStatus.UNAVAILABLE, f"HTTP {status}")
        reported = payload.get("status") if isinstance(payload, dict) else None
        if reported == "healthy":
            return ModelHealth(ModelHealthStatus.HEALTHY)
        return ModelHealth(ModelHealthStatus.DEGRADED, str(reported))

    # -- the bridge --------------------------------------------------------

    async def _submit(self, request: ModelRequest, cancellation: CancellationToken) -> str:
        """One task, with an idempotency key so a retried submission is not a second task."""
        body: dict[str, JSONValue] = {
            "idempotency_key": str(uuid4()),
            "content": {"prompt": _flatten(request)},
        }
        if request.response_schema is not None:
            body["output"] = {"format": "json", "json_schema": dict(request.response_schema)}
        model = request.model or self._preferred_model
        if model:
            # Named, not imposed: the broker may still route elsewhere, and it is the
            # component entitled to make that call.
            body["model_requirements"] = {"preferred_model": model, "fallback_allowed": True}
        status, payload = await self._call("POST", "/api/v1/tasks", body, cancellation)
        if status >= 500 or status in (408, 429):
            raise ModelTransientError(f"AI_Broker refused the task with HTTP {status}")
        if status >= 400:
            raise ModelPermanentError(
                f"AI_Broker rejected the task with HTTP {status}",
                details={"detail": _detail(payload)},
            )
        task_id = payload.get("task_id") or payload.get("id")
        if not isinstance(task_id, str):
            raise ModelPermanentError("AI_Broker accepted the task without naming it")
        return task_id

    async def _await_result(self, task_id: str, cancellation: CancellationToken) -> ModelResponse:
        """Poll until the task ends, the caller stops, or the wait runs out."""
        waited = 0.0
        while True:
            cancellation.raise_if_cancelled()
            status, payload = await self._call(
                "GET", f"/api/v1/tasks/{task_id}", None, cancellation
            )
            if status == 404:
                raise ModelPermanentError("AI_Broker lost the task", details={"task": task_id})
            if status >= 500:
                raise ModelTransientError(f"AI_Broker returned HTTP {status} while polling")
            state = str(payload.get("status", "")).lower()
            if state in _SUCCEEDED:
                return _response_from(payload)
            if state in _FAILED:
                raise ModelPermanentError(
                    "AI_Broker could not complete the task",
                    details={"task": task_id, "detail": _detail(payload)},
                )
            if state in _CANCELLED:
                # The broker gave up. Transient rather than permanent: the same request
                # submitted again may well be dispatched to a model that is free.
                raise ModelTransientError(
                    f"AI_Broker ended the task as {state}", details={"task": task_id}
                )
            if waited >= self._max_wait:
                raise ModelTransientError(
                    f"AI_Broker did not answer within {self._max_wait:g}s",
                    details={"task": task_id, "last_status": state},
                )
            await asyncio.sleep(self._poll)
            waited += self._poll

    async def _cancel_task(self, task_id: str) -> None:
        """Best effort. A failure to tidy up must not replace the reason we are leaving."""
        try:
            await self._call("DELETE", f"/api/v1/tasks/{task_id}", None, None)
        except (ModelTransientError, ModelPermanentError):
            return

    # -- transport ---------------------------------------------------------

    async def _call(
        self,
        method: str,
        path: str,
        body: Mapping[str, JSONValue] | None,
        cancellation: CancellationToken | None,
    ) -> tuple[int, JSONObject]:
        return await asyncio.to_thread(self._blocking_call, method, path, body, cancellation)

    def _blocking_call(
        self,
        method: str,
        path: str,
        body: Mapping[str, JSONValue] | None,
        cancellation: CancellationToken | None,
    ) -> tuple[int, JSONObject]:
        connection_type = (
            http.client.HTTPSConnection
            if self._url.scheme == "https"
            else http.client.HTTPConnection
        )
        connection = connection_type(self._host, self._url.port, timeout=_REQUEST_TIMEOUT_SECONDS)
        unsubscribe = cancellation.register(connection.close) if cancellation is not None else None
        # `x-admin-token`, not `Authorization`. The broker's own clients use it, and a
        # bearer header sails past its health endpoint while every write returns 403 —
        # which reads as a permissions problem rather than as the wrong header.
        headers = {"Content-Type": "application/json", "x-admin-token": self._token}
        encoded = json.dumps(body).encode("utf-8") if body is not None else None
        try:
            connection.request(method, path, body=encoded, headers=headers)
            response = connection.getresponse()
            raw = response.read().decode("utf-8", errors="replace")
        except (OSError, http.client.HTTPException) as exc:
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            raise ModelTransientError(f"AI_Broker is unreachable: {type(exc).__name__}") from exc
        finally:
            if unsubscribe is not None:
                unsubscribe()
            connection.close()
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"detail": raw[:400]}
        return response.status, payload if isinstance(payload, dict) else {"detail": payload}


def _flatten(request: ModelRequest) -> str:
    """The conversation as one prompt.

    The broker takes a prompt, not a message list. Roles are kept as labels rather than
    dropped: a system instruction that reads as if the user wrote it is a system
    instruction the model will weigh differently.
    """
    parts: list[str] = []
    for message in request.messages:
        label = {
            ModelRole.SYSTEM: "System",
            ModelRole.USER: "User",
            ModelRole.ASSISTANT: "Assistant",
            ModelRole.TOOL: "Tool",
        }.get(message.role, "User")
        if message.content.strip():
            parts.append(f"{label}: {message.content.strip()}")
    return "\n\n".join(parts)


def _response_from(payload: JSONObject) -> ModelResponse:
    """Read the answer out of a completed task.

    Everything lives under `result`: `assistant_content` is the text, `model_used.model`
    is which model the broker actually chose. Reporting that rather than a constant is
    what makes a run's metrics say something — "ai-broker" would name the router, not the
    thing that did the work.

    Several field names are accepted because they have moved between broker versions, and
    an adapter that understood only one would break on an upgrade with an empty string
    instead of an error.
    """
    result = payload.get("result")
    result = result if isinstance(result, dict) else {}
    content = ""
    for key in ("assistant_content", "result_markdown", "output_text", "content"):
        value = result.get(key) or payload.get(key)
        if isinstance(value, str) and value.strip():
            content = value
            break

    model = "ai-broker"
    used = result.get("model_used")
    if isinstance(used, dict) and isinstance(used.get("model"), str):
        model = str(used["model"])

    usage = result.get("usage")
    tokens = ModelUsage()
    if isinstance(usage, dict):
        tokens = ModelUsage(
            input_tokens=_count(usage, "tokens_input", "input_tokens", "prompt_tokens"),
            output_tokens=_count(usage, "tokens_output", "output_tokens", "completion_tokens"),
        )
    return ModelResponse(content=content, model=model, finish_reason="stop", usage=tokens)


def _count(usage: Mapping[str, JSONValue], *keys: str) -> int:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return max(0, value)
    return 0


def _detail(payload: JSONObject) -> str:
    for key in ("detail", "error", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value[:400]
    return ""


__all__ = ["AiBrokerModelProvider"]
