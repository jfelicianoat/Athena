"""Lectura de la consulta, del ultimo evento visto y emparejado de rutas."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import parse_qsl

from athena.adapters.service.server.http import Request


def _last_event_id(request: Request) -> str | None:
    """Where a reconnecting client says it got to.

    The header is what the SSE specification defines and what a browser sends by itself on
    reconnect. The query parameter exists for clients that cannot set headers on the
    request that opens the stream, which is more of them than one would hope.
    """
    header = request.headers.get("last-event-id")
    if header and header.strip():
        return header.strip()
    query = request.query.get("last_event_id")
    return query.strip() if query and query.strip() else None


async def _send(
    writer: asyncio.StreamWriter,
    name: str,
    payload: Any,
    *,
    event_id: str | None = None,
) -> None:
    frame = ""
    if event_id:
        frame += f"id: {event_id}\n"
    frame += f"event: {name}\n"
    frame += f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
    writer.write(frame.encode("utf-8"))
    await writer.drain()


def _parse_query(raw: str) -> dict[str, str]:
    return dict(parse_qsl(raw, keep_blank_values=True))


def _positive_int(raw: str | None) -> int:
    """Un cursor del cliente, saneado. Lo que no sea un entero valido es cero.

    Rechazarlo con un 400 castigaria a quien pide la historia entera con un parametro
    sobrante; empezar por el principio es exactamente lo que queria.
    """
    if raw is None:
        return 0
    try:
        value = int(raw)
    except ValueError:
        return 0
    return max(value, 0)


def _match(path: str, template: str) -> str | None:
    """One-placeholder route match. Enough for this surface, and obvious to read."""
    prefix, _, suffix = template.partition("{}")
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    middle = path[len(prefix) : len(path) - len(suffix) if suffix else None]
    if not middle or "/" in middle:
        return None
    return middle


def _match2(path: str, template: str) -> tuple[str, str] | None:
    first, _, rest = template.partition("{}")
    second, _, tail = rest.partition("{}")
    if not path.startswith(first):
        return None
    remainder = path[len(first) :]
    left, _, right = remainder.partition(second)
    if not left or "/" in left:
        return None
    if tail:
        if not right.endswith(tail):
            return None
        right = right[: len(right) - len(tail)]
    if not right or "/" in right:
        return None
    return left, right
