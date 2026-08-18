"""Contracts for externalizing large tool results."""

from __future__ import annotations

import asyncio
from hashlib import sha256
from typing import Protocol, runtime_checkable
from uuid import uuid4

from athena.cancellation import CancellationToken
from athena.tools import ToolResultReference


@runtime_checkable
class ToolResultStore(Protocol):
    async def put(
        self,
        content: str,
        *,
        media_type: str,
        cancellation: CancellationToken,
    ) -> ToolResultReference: ...

    async def get(
        self, reference: ToolResultReference, cancellation: CancellationToken
    ) -> str: ...


class InMemoryToolResultStore:
    """Basic bounded-lifetime store; persistence is intentionally deferred."""

    def __init__(self) -> None:
        self._content: dict[str, tuple[str, str]] = {}
        self._lock = asyncio.Lock()

    async def put(
        self,
        content: str,
        *,
        media_type: str,
        cancellation: CancellationToken,
    ) -> ToolResultReference:
        cancellation.raise_if_cancelled()
        key = str(uuid4())
        checksum = sha256(content.encode("utf-8")).hexdigest()
        async with self._lock:
            self._content[key] = (content, media_type)
        return ToolResultReference(
            store_key=key,
            media_type=media_type,
            size_chars=len(content),
            checksum=checksum,
        )

    async def get(
        self, reference: ToolResultReference, cancellation: CancellationToken
    ) -> str:
        cancellation.raise_if_cancelled()
        async with self._lock:
            content, _ = self._content[reference.store_key]
        if sha256(content.encode("utf-8")).hexdigest() != reference.checksum:
            raise ValueError("Stored tool result checksum mismatch")
        return content
