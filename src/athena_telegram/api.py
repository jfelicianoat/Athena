"""A minimum Telegram Bot API client, on the standard library.

Two calls are enough for this adapter: `getUpdates` in long-polling mode and `sendMessage`.
Everything else the Bot API offers — keyboards, media, webhooks, edits — belongs to a
richer channel than the one Athena needs, and each would be another shape to keep working.

No third-party client, for the same reason `OpenAICompatibleModelProvider` has none: the
repository installs with no dependencies, and a bot library would put a transport's release
cadence in front of the runtime's. `http.client` on a worker thread is the pattern already
in use here.
"""

from __future__ import annotations

import asyncio
import http.client
import json
from dataclasses import dataclass
from typing import cast

from athena.errors import AthenaRuntimeError
from athena.types import JSONObject, JSONValue

_API_HOST = "api.telegram.org"

#: How long a `getUpdates` call may wait for something to happen. Long polling is what
#: keeps the bot responsive without hammering the API; Telegram allows up to 50.
DEFAULT_POLL_SECONDS = 25

#: Read timeout, deliberately longer than the poll: the socket is *expected* to sit idle
#: for the whole poll window, and a shorter one would turn normal quiet into an error.
_SOCKET_MARGIN_SECONDS = 10


class TelegramTransportError(AthenaRuntimeError):
    """Telegram is unreachable or answered with something unusable.

    Transient by nature: the adapter retries, because it is the only side that knows what
    its service considers temporary. It stays inside Athena's taxonomy so that a gateway
    catching `AthenaRuntimeError` treats it as a delivery failure rather than a crash.
    """

    code = "telegram_transport_error"


class TelegramRejectedError(AthenaRuntimeError):
    """Telegram understood the request and refused it.

    Distinct from a transport failure because retrying will not help: a wrong token, a
    chat the bot was removed from, a message the API will never accept.
    """

    code = "telegram_rejected"


@dataclass(frozen=True, slots=True)
class TelegramUpdate:
    """One update, reduced to what a transport-only adapter needs.

    Everything not listed here is dropped at the edge on purpose. An adapter that carried
    the raw payload inward would be inviting the runtime to learn Telegram's schema.
    """

    update_id: int
    chat_id: int
    chat_type: str
    user_id: int
    text: str
    username: str | None = None
    message_id: int | None = None

    @property
    def is_private(self) -> bool:
        return self.chat_type == "private"


def parse_update(raw: object) -> TelegramUpdate | None:
    """Read one update, or `None` if it is not one this adapter can act on.

    Returning `None` rather than raising is the point. A bot receives edits, joins, polls,
    stickers, callback queries and things added to the API after this was written; none of
    them is malformed, and none of them is a message. Treating "not for me" as an error
    would make normal traffic look like a fault, so only the shape is checked, and anything
    that fails the check is simply not acted upon.
    """
    if not isinstance(raw, dict):
        return None
    update_id = raw.get("update_id")
    if not isinstance(update_id, int) or isinstance(update_id, bool):
        return None
    message = raw.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    sender = message.get("from")
    text = message.get("text")
    if not isinstance(chat, dict) or not isinstance(sender, dict) or not isinstance(text, str):
        return None
    chat_id = chat.get("id")
    user_id = sender.get("id")
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        return None
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        return None
    chat_type = chat.get("type")
    username = sender.get("username")
    message_id = message.get("message_id")
    return TelegramUpdate(
        update_id=update_id,
        chat_id=chat_id,
        chat_type=chat_type if isinstance(chat_type, str) else "",
        user_id=user_id,
        text=text,
        username=username if isinstance(username, str) else None,
        message_id=message_id if isinstance(message_id, int) else None,
    )


class TelegramApi:
    """Calls the Bot API. Knows nothing about Athena."""

    def __init__(
        self,
        token: str,
        *,
        poll_seconds: int = DEFAULT_POLL_SECONDS,
        host: str = _API_HOST,
        port: int | None = None,
        use_tls: bool = True,
    ) -> None:
        if not token.strip():
            raise ValueError("A Telegram bot token is required")
        self._token = token.strip()
        self._poll_seconds = poll_seconds
        self._host = host
        self._port = port
        self._use_tls = use_tls

    async def get_updates(self, offset: int | None) -> list[JSONValue]:
        """Long-poll for updates from `offset` onwards."""
        payload: dict[str, JSONValue] = {
            "timeout": self._poll_seconds,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = await self._call("getUpdates", payload)
        return list(result) if isinstance(result, list) else []

    async def send_message(self, chat_id: int, text: str) -> None:
        await self._call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                # No parse mode: Athena's text is not markup, and asking Telegram to parse
                # it would turn a stray underscore in a file path into a rejected message.
                "disable_web_page_preview": True,
            },
        )

    async def _call(self, method: str, payload: JSONObject) -> JSONValue:
        return await asyncio.to_thread(self._blocking_call, method, payload)

    def _blocking_call(self, method: str, payload: JSONObject) -> JSONValue:
        timeout = float(self._poll_seconds + _SOCKET_MARGIN_SECONDS)
        connection_type = (
            http.client.HTTPSConnection if self._use_tls else http.client.HTTPConnection
        )
        connection = connection_type(self._host, self._port, timeout=timeout)
        body = json.dumps(payload).encode("utf-8")
        try:
            connection.request(
                "POST",
                f"/bot{self._token}/{method}",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            raw = response.read().decode("utf-8", errors="replace")
        except (OSError, http.client.HTTPException) as exc:
            raise TelegramTransportError(f"Telegram is unreachable: {type(exc).__name__}") from exc
        finally:
            connection.close()

        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TelegramTransportError("Telegram returned something that is not JSON") from exc
        if not isinstance(document, dict):
            raise TelegramTransportError("Telegram returned a document that is not an object")

        if document.get("ok") is True:
            return cast(JSONValue, document.get("result"))

        description = document.get("description")
        detail = description if isinstance(description, str) else f"HTTP {response.status}"
        # 429 and 5xx are worth trying again; the rest are the API saying no, and a retry
        # loop against a wrong token is just a slow way to stay broken.
        if response.status == 429 or response.status >= 500:
            raise TelegramTransportError(f"Telegram asked us to back off: {detail}")
        raise TelegramRejectedError(f"Telegram refused the request: {detail}")


__all__ = [
    "DEFAULT_POLL_SECONDS",
    "TelegramApi",
    "TelegramRejectedError",
    "TelegramTransportError",
    "TelegramUpdate",
    "parse_update",
]
