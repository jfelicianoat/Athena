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
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import cast
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
    ModelToolCall,
    ModelUsage,
)
from athena.types import JSONObject, JSONValue

#: How often to ask whether a task is done. Fast enough that a short answer feels
#: immediate, slow enough that a long one does not cost hundreds of requests.
_POLL_INTERVAL_SECONDS = 1.0

#: How long a single HTTP call may take. Not how long a task may take — that is the
#: caller's timeout, and conflating them would make a slow model look like a dead broker.
#:
#: Ninety seconds, not thirty. A broker with a full queue answers `/health` in eight
#: seconds and `/api/v1/queue` in twenty; a poll that arrives while a 30B model is loading
#: waits behind it. At thirty seconds those turn into `TimeoutError`, which the adapter
#: correctly reports as transient — and after two retries the run dies of a busy server
#: rather than of anything being wrong.
DEFAULT_REQUEST_TIMEOUT_SECONDS = 90.0

#: States the broker reports. Anything not listed is treated as still working, because a
#: state this adapter does not recognise is not evidence that the task ended.
_SUCCEEDED = frozenset({"succeeded", "completed", "done"})
_FAILED = frozenset({"failed", "error", "rejected"})
_CANCELLED = frozenset({"cancelled", "canceled", "expired", "timeout"})


@dataclass(frozen=True, slots=True)
class _PreparedRequest:
    prompt: str
    output_schema: JSONObject | None
    allowed_tools: frozenset[str]
    tool_choice_required: bool = False


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
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
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
        #: Configurable because how slow "slow but alive" is depends on the deployment: a
        #: broker sharing a GPU with something else is not a broken broker.
        self._request_timeout = max(1.0, request_timeout_seconds)

    # -- the port ----------------------------------------------------------

    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        cancellation.raise_if_cancelled()
        prepared = _prepare_request(request)
        task_id = await self._submit(request, prepared, cancellation)
        try:
            return await self._await_result(task_id, prepared, cancellation)
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
        # AI_Broker accepts prompts plus a JSON output schema. The adapter translates
        # Athena's native tool interface into that structured contract and translates the
        # model's decision back into ModelToolCall objects. Athena still executes them.
        return ModelCapabilities(streaming=False, tool_calls=True, structured_output=True)

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

    async def _submit(
        self,
        request: ModelRequest,
        prepared: _PreparedRequest,
        cancellation: CancellationToken,
    ) -> str:
        """One task, with an idempotency key so a retried submission is not a second task."""
        body: dict[str, JSONValue] = {
            "idempotency_key": str(uuid4()),
            "content": {"prompt": prepared.prompt},
        }
        if prepared.output_schema is not None:
            body["output"] = {
                "format": "json",
                "json_schema": dict(prepared.output_schema),
            }
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

    async def _await_result(
        self,
        task_id: str,
        prepared: _PreparedRequest,
        cancellation: CancellationToken,
    ) -> ModelResponse:
        """Poll until the task ends, the caller stops, or the wait runs out.

        The clock is the wall clock. Adding up the sleeps instead counts only the time
        spent waiting between polls, and not the time each poll takes — which against a
        loaded broker is the larger of the two by an order of magnitude. A `GET` that takes
        twenty seconds and a poll interval of one turned a ten-minute ceiling into three
        hours, so a broker that never finished generating left a run hanging with no event
        and no failure for as long as anybody was willing to watch it.
        """
        deadline = time.monotonic() + self._max_wait
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
                return _response_from(
                    payload,
                    prepared.allowed_tools,
                    tool_choice_required=prepared.tool_choice_required,
                )
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
            if time.monotonic() >= deadline:
                raise ModelTransientError(
                    f"AI_Broker did not answer within {self._max_wait:g}s",
                    details={"task": task_id, "last_status": state},
                )
            await asyncio.sleep(self._poll)

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
        connection = connection_type(self._host, self._url.port, timeout=self._request_timeout)
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
        if message.role is ModelRole.TOOL:
            identity = message.name or "unknown"
            correlation = f" for {message.tool_call_id}" if message.tool_call_id else ""
            label = f"Tool {identity} result{correlation}"
        if message.content.strip():
            parts.append(f"{label}: {message.content.strip()}")
        if message.tool_calls:
            calls = [
                {
                    "call_id": call.call_id,
                    "name": call.name,
                    "arguments": dict(call.arguments),
                }
                for call in message.tool_calls
            ]
            parts.append(
                "Assistant requested tool calls: "
                + json.dumps(calls, ensure_ascii=False, separators=(",", ":"))
            )
    return "\n\n".join(parts)


def _prepare_request(request: ModelRequest) -> _PreparedRequest:
    contracts = _tool_contracts(request.tools)
    prompt = _flatten(request)
    if not contracts:
        return _PreparedRequest(prompt, request.response_schema, frozenset())

    tool_choice_required = request.options.get("tool_choice") == "required"
    tools_json = json.dumps(request.tools, ensure_ascii=False, separators=(",", ":"))
    protocol = (
        "You control Athena through the tools listed below. Athena, not you, executes each "
        "tool and applies its permission policy. Return exactly one JSON object matching "
        "the supplied schema. Use kind=tool_calls when an action or more information is "
        "needed, and include only listed tool names with valid arguments. Never claim a "
        "tool ran before Athena returns its result. Use kind=message only when the objective "
        "is complete and put the final answer in message.\n\nAvailable tools:\n" + tools_json
    )
    if tool_choice_required:
        protocol += (
            "\n\nA tool call is mandatory on this turn. Return kind=tool_calls; "
            "kind=message is not permitted."
        )
    return _PreparedRequest(
        f"{prompt}\n\nSystem tool protocol: {protocol}",
        _decision_schema(
            contracts,
            request.response_schema,
            tool_choice_required=tool_choice_required,
        ),
        frozenset(name for name, _ in contracts),
        tool_choice_required,
    )


def _tool_contracts(
    tools: tuple[JSONObject, ...],
) -> tuple[tuple[str, JSONObject], ...]:
    contracts: list[tuple[str, JSONObject]] = []
    for definition in tools:
        function = definition.get("function")
        if not isinstance(function, Mapping):
            continue
        name = function.get("name")
        parameters = function.get("parameters")
        if not isinstance(name, str) or not name or not isinstance(parameters, Mapping):
            continue
        contracts.append((name, parameters))
    return tuple(contracts)


def _decision_schema(
    contracts: tuple[tuple[str, JSONObject], ...],
    response_schema: JSONObject | None,
    *,
    tool_choice_required: bool = False,
) -> JSONObject:
    names = [name for name, _ in contracts]
    message_schema: JSONObject = response_schema or {"type": "string"}
    kind_values = ["tool_calls"] if tool_choice_required else ["message", "tool_calls"]
    required = (
        ["kind", "tool_calls"]
        if tool_choice_required
        else [
            "kind",
            "message",
            "tool_calls",
        ]
    )
    return {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": kind_values},
            "message": dict(message_schema),
            "tool_calls": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "call_id": {"type": "string"},
                        "name": {"type": "string", "enum": names},
                        # The full per-tool schemas remain in the prompt. Athena validates
                        # arguments again before permission or execution, while this broad
                        # object keeps the broker contract portable across model vendors.
                        "arguments": {"type": "object"},
                    },
                    "required": ["name", "arguments"],
                    "additionalProperties": False,
                },
                "minItems": 1 if tool_choice_required else 0,
            },
        },
        "required": required,
        "additionalProperties": False,
    }


def _response_from(
    payload: JSONObject,
    allowed_tools: frozenset[str] = frozenset(),
    *,
    tool_choice_required: bool = False,
) -> ModelResponse:
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

    tool_calls: tuple[ModelToolCall, ...] = ()
    finish_reason = "stop"
    if allowed_tools:
        decision = _structured_decision(result, payload, content)
        content, tool_calls = _parse_decision(
            decision,
            allowed_tools,
            tool_choice_required=tool_choice_required,
        )
        if tool_calls:
            finish_reason = "tool_calls"

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
    return ModelResponse(
        content=content,
        model=model,
        finish_reason=finish_reason,
        tool_calls=tool_calls,
        usage=tokens,
    )


def _structured_decision(
    result: Mapping[str, JSONValue], payload: JSONObject, content: str
) -> JSONObject:
    candidates = [
        result.get("structured_output"),
        result.get("output"),
        result.get("json"),
        payload.get("structured_output"),
        payload.get("output"),
    ]
    for candidate in candidates:
        if isinstance(candidate, Mapping) and "kind" in candidate:
            return candidate
    decoded = _decode_json_object(content)
    if decoded is None:
        raise ModelPermanentError(
            "AI_Broker model did not return the structured Athena tool decision"
        )
    return decoded


def _decode_json_object(content: str) -> JSONObject | None:
    candidate = content.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    try:
        decoded = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return cast(JSONObject, decoded) if isinstance(decoded, Mapping) else None


def _parse_decision(
    decision: JSONObject,
    allowed_tools: frozenset[str],
    *,
    tool_choice_required: bool = False,
) -> tuple[str, tuple[ModelToolCall, ...]]:
    kind = decision.get("kind")
    if kind == "message":
        if tool_choice_required:
            raise ModelTransientError(
                "AI_Broker returned a final message when Athena required a tool call"
            )
        message = decision.get("message", "")
        content = message if isinstance(message, str) else json.dumps(message, ensure_ascii=False)
        if not content.strip():
            raise ModelPermanentError("AI_Broker returned an empty final message")
        return content, ()
    if kind != "tool_calls":
        raise ModelPermanentError("AI_Broker returned an unknown Athena decision kind")
    raw_calls = decision.get("tool_calls")
    if not isinstance(raw_calls, list) or not raw_calls:
        raise ModelPermanentError("AI_Broker returned an empty tool decision")
    calls = tuple(_parse_tool_call(value, allowed_tools) for value in raw_calls)
    return "", calls


def _parse_tool_call(value: JSONValue, allowed_tools: frozenset[str]) -> ModelToolCall:
    if not isinstance(value, Mapping):
        raise ModelPermanentError("AI_Broker returned a malformed tool call")
    name = value.get("name")
    arguments = value.get("arguments")
    if not isinstance(name, str) or name not in allowed_tools:
        raise ModelPermanentError("AI_Broker selected a tool Athena did not offer")
    if not isinstance(arguments, Mapping):
        raise ModelPermanentError("AI_Broker returned malformed tool arguments")
    call_id = value.get("call_id")
    if not isinstance(call_id, str) or not call_id.strip():
        call_id = str(uuid4())
    return ModelToolCall(call_id, name, arguments)


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


__all__ = ["DEFAULT_REQUEST_TIMEOUT_SECONDS", "AiBrokerModelProvider"]
