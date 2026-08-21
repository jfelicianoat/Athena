"""One-line startup handshake for clients that manage the Athena service process."""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlsplit

_READY_PREFIX = "ATHENA_SERVICE_READY "


@dataclass(frozen=True, slots=True)
class ServiceEndpoint:
    """Connection details emitted once, after the local socket is listening."""

    base_url: str
    token: str

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).hostname or ""

    @property
    def port(self) -> int:
        return urlsplit(self.base_url).port or 0


def service_ready_line(endpoint: ServiceEndpoint) -> str:
    payload = {"base_url": endpoint.base_url, "token": endpoint.token}
    return _READY_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def parse_service_ready(line: str) -> ServiceEndpoint | None:
    if not line.startswith(_READY_PREFIX):
        return None
    try:
        payload = json.loads(line[len(_READY_PREFIX) :])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    base_url = payload.get("base_url")
    token = payload.get("token")
    if not isinstance(base_url, str) or not isinstance(token, str) or not token:
        return None
    parsed = urlsplit(base_url)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "::1", "localhost"):
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is None:
        return None
    return ServiceEndpoint(base_url, token)


__all__ = ["ServiceEndpoint", "parse_service_ready", "service_ready_line"]
