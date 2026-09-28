"""Primitivas HTTP/1.1: peticion, respuesta, limites y utilidades de ruta.

Athena no declara dependencias, asi que el transporte se escribe a mano. Los
limites de cabecera y cuerpo estan aqui y no repartidos: la superficie de un
parser propio se acota en un sitio o no se acota.
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from athena.errors import (
    ToolValidationError,
)
from athena.identity import IdentityDirectory
from athena.types import JSONObject

_logger = logging.getLogger(__name__)

_MAX_HEADER_BYTES = 16 * 1024
_MAX_BODY_BYTES = 4 * 1024 * 1024
_SSE_KEEPALIVE_SECONDS = 15.0

#: How many idempotency keys to remember. A retry that arrives long after the map has
#: turned over is a new request, which is the honest reading of a key nobody kept.
_IDEMPOTENCY_ENTRIES = 256


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    host: str = "127.0.0.1"
    port: int = 8770
    #: Minted per start, like AI_Broker. Never persisted by the service itself.
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    delivery_timeout_seconds: float | None = None
    approval_timeout_seconds: float | None = None
    #: Optional gate the host application supplies, e.g. ChatyGPT's authorized folders.
    authorized_workspace: Callable[[Path], bool] | None = None
    #: Athena's identity directory. Absent means this deployment has no notion of a person
    #: beyond "the client holding the bearer token", and link codes cannot be minted.
    directory: IdentityDirectory | None = None

    def __post_init__(self) -> None:
        if self.host not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError(
                "The Athena service binds to the loopback interface only; "
                f"refusing host {self.host!r}"
            )
        if not self.token:
            raise ValueError("The service requires a non-empty token")


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    path: str
    query: Mapping[str, str]
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> JSONObject:
        if not self.body:
            return {}
        try:
            payload = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ToolValidationError("Request body is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ToolValidationError("Request body must be a JSON object")
        return payload


@dataclass(frozen=True, slots=True)
class Response:
    status: int = 200
    payload: JSONObject | None = None
    body: bytes = b""
    content_type: str = "application/json"

    def rendered(self) -> tuple[bytes, str]:
        if self.payload is not None:
            return json.dumps(self.payload, ensure_ascii=False).encode("utf-8"), self.content_type
        return self.body, self.content_type


_REASONS = {
    200: "OK",
    201: "Created",
    202: "Accepted",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    410: "Gone",
    408: "Request Timeout",
    413: "Payload Too Large",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    503: "Service Unavailable",
}

Handler = Callable[[Request], Awaitable[Response]]
