"""Externalizing large tool results.

A reference is a promise: the model is told the payload exists elsewhere instead of being
handed the payload. The promise is only worth something if the store outlives the moment,
so the durable implementation states its retention window explicitly and fails loudly when
a reference has expired rather than returning a plausible-looking nothing.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from hashlib import sha256
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import uuid4

from athena.cancellation import CancellationToken
from athena.errors import ToolResultUnavailableError
from athena.tools import ToolResultReference

#: Default retention for an externalized tool result. Documented because callers hold
#: references across restarts and need to know when one stops being valid.
DEFAULT_RETENTION_SECONDS = 7 * 24 * 60 * 60


@runtime_checkable
class ToolResultStore(Protocol):
    async def put(
        self,
        content: str,
        *,
        media_type: str,
        cancellation: CancellationToken,
    ) -> ToolResultReference: ...

    async def get(self, reference: ToolResultReference, cancellation: CancellationToken) -> str: ...


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

    async def get(self, reference: ToolResultReference, cancellation: CancellationToken) -> str:
        cancellation.raise_if_cancelled()
        async with self._lock:
            entry = self._content.get(reference.store_key)
        if entry is None:
            raise ToolResultUnavailableError(
                f"No stored result for {reference.uri}",
                details={"store_key": reference.store_key},
            )
        content, _ = entry
        if sha256(content.encode("utf-8")).hexdigest() != reference.checksum:
            raise ToolResultUnavailableError(
                f"Stored result for {reference.uri} does not match its checksum",
                details={"store_key": reference.store_key},
            )
        return content


_STORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_results (
    store_key   TEXT PRIMARY KEY,
    media_type  TEXT NOT NULL,
    content     TEXT NOT NULL,
    checksum    TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tool_results_created ON tool_results (created_at);
"""


class SqliteToolResultStore:
    """Durable store whose references survive a restart for a documented window."""

    def __init__(
        self,
        database: Path | str,
        *,
        retention_seconds: float = DEFAULT_RETENTION_SECONDS,
    ) -> None:
        if retention_seconds <= 0:
            raise ValueError("retention_seconds must be positive")
        self.database = Path(database)
        self.retention_seconds = retention_seconds
        if str(self.database) != ":memory:":
            self.database.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        with self._connect() as connection:
            connection.executescript(_STORE_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

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
            await asyncio.to_thread(self._insert, key, media_type, content, checksum)
        return ToolResultReference(
            store_key=key,
            media_type=media_type,
            size_chars=len(content),
            checksum=checksum,
        )

    def _insert(self, key: str, media_type: str, content: str, checksum: str) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO tool_results (store_key, media_type, content, checksum, "
                    "created_at) VALUES (?, ?, ?, ?, ?)",
                    (key, media_type, content, checksum, time.time()),
                )
        except sqlite3.Error as exc:
            raise ToolResultUnavailableError("Cannot persist tool result") from exc

    async def get(self, reference: ToolResultReference, cancellation: CancellationToken) -> str:
        cancellation.raise_if_cancelled()
        row = await asyncio.to_thread(self._select, reference.store_key)
        if row is None:
            raise ToolResultUnavailableError(
                f"No stored result for {reference.uri}",
                details={"store_key": reference.store_key},
            )
        content, checksum, created_at = row
        age = time.time() - created_at
        if age > self.retention_seconds:
            raise ToolResultUnavailableError(
                f"The result behind {reference.uri} expired after {self.retention_seconds:.0f}s",
                details={"store_key": reference.store_key, "age_seconds": round(age, 3)},
            )
        if reference.checksum is not None and reference.checksum != checksum:
            raise ToolResultUnavailableError(
                f"Stored result for {reference.uri} does not match its checksum",
                details={"store_key": reference.store_key},
            )
        return content

    def _select(self, key: str) -> tuple[str, str, float] | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT content, checksum, created_at FROM tool_results WHERE store_key = ?",
                    (key,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise ToolResultUnavailableError("Cannot read tool result") from exc
        if row is None:
            return None
        return str(row["content"]), str(row["checksum"]), float(row["created_at"])

    async def purge_expired(self) -> int:
        """Drop everything past the retention window. Returns how many rows went."""
        async with self._lock:
            return await asyncio.to_thread(self._purge)

    def _purge(self) -> int:
        cutoff = time.time() - self.retention_seconds
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "DELETE FROM tool_results WHERE created_at < ?", (cutoff,)
                )
                return int(cursor.rowcount or 0)
        except sqlite3.Error as exc:
            raise ToolResultUnavailableError("Cannot purge expired tool results") from exc
