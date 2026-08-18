"""Standard-library adapter for OpenAI-compatible chat-completions endpoints."""

from __future__ import annotations

import asyncio
import http.client
import json
from collections.abc import AsyncIterator, Mapping
from typing import cast
from urllib.parse import urlsplit

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
    ModelMessage,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    ModelUsage,
)
from athena.types import JSONObject, JSONValue


class OpenAICompatibleModelProvider(ModelProvider):
    """Adapter suitable for OpenAI-compatible APIs, Ollama proxies, and LM Studio."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        request_timeout_seconds: float = 60.0,
    ) -> None:
        parsed = urlsplit(base_url.rstrip("/"))
        host = parsed.hostname
        if parsed.scheme not in ("http", "https") or not host:
            raise ValueError("base_url must be an HTTP(S) URL")
        self._url = parsed
        self._host = host
        self._base_path = parsed.path.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._timeout = request_timeout_seconds

    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        cancellation.raise_if_cancelled()
        payload: dict[str, JSONValue] = {
            "model": request.model or self._model,
            "messages": [self._message(message) for message in request.messages],
            "tools": list(request.tools),
            **dict(request.options),
        }
        if request.response_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": request.response_schema,
            }
        raw = await asyncio.to_thread(
            self._request,
            "POST",
            f"{self._base_path}/chat/completions",
            payload,
            cancellation,
        )
        cancellation.raise_if_cancelled()
        return self._parse_response(raw)

    async def stream(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> AsyncIterator[ModelEvent]:
        del request, cancellation
        if False:
            yield cast(ModelEvent, None)
        raise ModelStreamingUnsupportedError("This H1 adapter does not implement streaming")

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(streaming=False, tool_calls=True, structured_output=True)

    async def health(self, cancellation: CancellationToken) -> ModelHealth:
        try:
            await asyncio.to_thread(
                self._request,
                "GET",
                f"{self._base_path}/models",
                None,
                cancellation,
            )
        except ModelTransientError as exc:
            return ModelHealth(ModelHealthStatus.UNAVAILABLE, exc.message)
        except ModelPermanentError as exc:
            return ModelHealth(ModelHealthStatus.DEGRADED, exc.message)
        return ModelHealth(ModelHealthStatus.HEALTHY)

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, JSONValue] | None,
        cancellation: CancellationToken,
    ) -> JSONObject:
        connection_type = (
            http.client.HTTPSConnection
            if self._url.scheme == "https"
            else http.client.HTTPConnection
        )
        connection = connection_type(
            self._host,
            self._url.port,
            timeout=self._timeout,
        )
        unsubscribe = cancellation.register(connection.close)
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        try:
            cancellation.raise_if_cancelled()
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read().decode("utf-8", errors="replace")
        except (OSError, http.client.HTTPException) as exc:
            cancellation.raise_if_cancelled()
            raise ModelTransientError(f"Model endpoint unavailable: {type(exc).__name__}") from exc
        finally:
            unsubscribe()
            connection.close()
        if response.status in (408, 409, 425, 429) or response.status >= 500:
            raise ModelTransientError(
                f"Model endpoint returned HTTP {response.status}",
                details={"status": response.status},
            )
        if response.status >= 400:
            raise ModelPermanentError(
                f"Model endpoint returned HTTP {response.status}",
                details={"status": response.status},
            )
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ModelPermanentError("Model endpoint returned invalid JSON") from exc
        if not isinstance(parsed, Mapping):
            raise ModelPermanentError("Model endpoint returned a non-object response")
        return cast(JSONObject, parsed)

    @staticmethod
    def _message(message: ModelMessage) -> JSONObject:
        payload: dict[str, JSONValue] = {
            "role": message.role.value,
            "content": message.content,
        }
        if message.name:
            payload["name"] = message.name
        if message.tool_call_id:
            payload["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ]
        return payload

    @staticmethod
    def _parse_response(payload: JSONObject) -> ModelResponse:
        try:
            choices = payload["choices"]
            if not isinstance(choices, list) or not choices:
                raise TypeError
            choice = choices[0]
            if not isinstance(choice, Mapping):
                raise TypeError
            message = choice["message"]
            if not isinstance(message, Mapping):
                raise TypeError
            raw_calls = message.get("tool_calls", [])
            if not isinstance(raw_calls, list):
                raise TypeError
            calls = tuple(OpenAICompatibleModelProvider._parse_call(call) for call in raw_calls)
            usage = payload.get("usage", {})
            if not isinstance(usage, Mapping):
                usage = {}
            return ModelResponse(
                content=str(message.get("content") or ""),
                model=str(payload.get("model") or "unknown"),
                finish_reason=str(choice.get("finish_reason") or "stop"),
                tool_calls=calls,
                usage=ModelUsage(
                    input_tokens=OpenAICompatibleModelProvider._usage_value(
                        usage.get("prompt_tokens", 0)
                    ),
                    output_tokens=OpenAICompatibleModelProvider._usage_value(
                        usage.get("completion_tokens", 0)
                    ),
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelPermanentError("Malformed chat-completions response") from exc

    @staticmethod
    def _parse_call(value: object) -> ModelToolCall:
        if not isinstance(value, Mapping):
            raise TypeError
        function = value.get("function")
        if not isinstance(function, Mapping):
            raise TypeError
        arguments = json.loads(str(function.get("arguments", "{}")))
        if not isinstance(arguments, Mapping):
            raise TypeError
        return ModelToolCall(
            call_id=str(value.get("id") or ""),
            name=str(function.get("name") or ""),
            arguments=cast(JSONObject, arguments),
        )

    @staticmethod
    def _usage_value(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError
        return value
