"""Durable session state.

The chat log is not a database. What survives a restart is this: session metadata, the
working memory, the references to externalized tool results, the verification evidence,
and a bounded trail of event checkpoints.

The one rule that matters on recovery: a session that was live when the process died is
never resurrected as `completed`. It becomes `recovery_pending`, because the runtime does
not know whether the work finished, and guessing in the optimistic direction is how an
agent ends up claiming success it never achieved.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from athena.errors import AthenaRuntimeError
from athena.sqlite_support import closing_connection, migrate, process_alive
from athena.state import AgentStatus
from athena.tools import ToolResultReference
from athena.types import JSONObject
from athena.working_state import WorkingState

#: Statuses that mean "this session was live when we last saw it".
_LIVE_STATUSES = (
    AgentStatus.RUNNING.value,
    AgentStatus.VERIFYING.value,
    AgentStatus.WAITING_PERMISSION.value,
)

_MAX_CHECKPOINTS = 200

_HOST = socket.gethostname()


class SessionStoreError(AthenaRuntimeError):
    code = "session_store_error"


@dataclass(frozen=True, slots=True)
class EventCheckpoint:
    """A milestone worth replaying to a human, not the full event stream."""

    name: str
    payload: JSONObject = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session_id: str
    workspace_id: str
    status: AgentStatus
    working_memory: WorkingState
    tool_references: tuple[ToolResultReference, ...] = ()
    verification: JSONObject = field(default_factory=dict)
    checkpoints: tuple[EventCheckpoint, ...] = ()
    #: True when the stored payload was incomplete and had to be partially reconstructed.
    degraded: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def objective(self) -> str:
        return self.working_memory.objective

    @property
    def resumable(self) -> bool:
        return self.status is AgentStatus.RECOVERY_PENDING

    def checkpointed(self, checkpoint: EventCheckpoint) -> SessionRecord:
        trail = (*self.checkpoints, checkpoint)[-_MAX_CHECKPOINTS:]
        return replace(self, checkpoints=trail, updated_at=datetime.now(UTC))


@runtime_checkable
class SessionStore(Protocol):
    async def save(self, record: SessionRecord) -> None: ...

    async def load(self, session_id: str) -> SessionRecord | None: ...

    async def list_sessions(
        self, status: AgentStatus | None = None
    ) -> tuple[SessionRecord, ...]: ...

    async def mark_interrupted(self) -> tuple[str, ...]: ...

    async def delete(self, session_id: str) -> None: ...

    async def save_manifest(self, session_id: str, manifest: JSONObject) -> None: ...

    async def load_manifest(self, session_id: str) -> JSONObject | None: ...


# --------------------------------------------------------------------------- serialisation


def _encode(record: SessionRecord) -> tuple[str, str, str]:
    return (
        json.dumps(record.working_memory.to_json(), ensure_ascii=False),
        json.dumps(dict(record.verification), ensure_ascii=False, default=str),
        json.dumps(
            [
                {
                    "name": checkpoint.name,
                    "payload": dict(checkpoint.payload),
                    "occurred_at": checkpoint.occurred_at.isoformat(),
                }
                for checkpoint in record.checkpoints
            ],
            ensure_ascii=False,
            default=str,
        ),
    )


def _decode_working_memory(raw: str, session_id: str) -> tuple[WorkingState, bool]:
    """Never raise on a damaged row: reconstruct the safest state we can justify."""
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return _placeholder(session_id), True
    if not isinstance(payload, Mapping):
        return _placeholder(session_id), True
    try:
        return WorkingState.from_json(payload), False
    except AthenaRuntimeError:
        return _placeholder(session_id), True


def _placeholder(session_id: str) -> WorkingState:
    return WorkingState(
        objective=f"[unrecovered objective for session {session_id}]",
        remaining_work=("Re-state the objective: the stored working memory was damaged.",),
    )


def _decode_checkpoints(raw: str) -> tuple[EventCheckpoint, ...]:
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ()
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
        return ()
    checkpoints: list[EventCheckpoint] = []
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        if not isinstance(name, str):
            continue
        moment = item.get("occurred_at")
        try:
            occurred_at = (
                datetime.fromisoformat(moment) if isinstance(moment, str) else datetime.now(UTC)
            )
        except ValueError:
            occurred_at = datetime.now(UTC)
        payload_value = item.get("payload")
        checkpoints.append(
            EventCheckpoint(
                name,
                dict(payload_value) if isinstance(payload_value, Mapping) else {},
                occurred_at,
            )
        )
    return tuple(checkpoints)


def _decode_json_object(raw: str) -> JSONObject:
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _decode_status(raw: str) -> tuple[AgentStatus, bool]:
    try:
        return AgentStatus(raw), False
    except ValueError:
        return AgentStatus.RECOVERY_PENDING, True


def _decode_moment(raw: str) -> datetime:
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return datetime.now(UTC)


# --------------------------------------------------------------------------- sqlite


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT PRIMARY KEY,
    workspace_id   TEXT NOT NULL,
    status         TEXT NOT NULL,
    working_memory TEXT NOT NULL,
    verification   TEXT NOT NULL DEFAULT '{}',
    checkpoints    TEXT NOT NULL DEFAULT '[]',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_tool_references (
    session_id  TEXT NOT NULL,
    store_key   TEXT NOT NULL,
    media_type  TEXT NOT NULL,
    size_chars  INTEGER NOT NULL,
    checksum    TEXT,
    PRIMARY KEY (session_id, store_key)
);
CREATE INDEX IF NOT EXISTS sessions_status ON sessions (status);
CREATE TABLE IF NOT EXISTS session_owners (
    session_id  TEXT PRIMARY KEY,
    pid         INTEGER NOT NULL,
    host        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_manifests (
    session_id  TEXT PRIMARY KEY,
    manifest    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""

#: Version del esquema de esta base. Sube con cada cambio de forma; una base mas nueva
#: que el codigo se rechaza en vez de leerse a medias (A22).
_SCHEMA_VERSION = 3

#: Una sesion viva que lleva esto sin escribir se da por huerfana aunque su proceso
#: parezca vivo: cubre la reutilizacion de pids y los procesos colgados. Mas largo que
#: cualquier plazo de run que Athena permite por defecto.
_STALE_AFTER_SECONDS = 6 * 3600


class SqliteSessionStore:
    """SQLite-backed session persistence. Blocking calls run off the event loop."""

    def __init__(self, database: Path | str) -> None:
        self.database = Path(database)
        if str(self.database) != ":memory:":
            self.database.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        with self._connect() as connection:
            migrate(connection, _SCHEMA, _SCHEMA_VERSION, "sessions")

    def _connect(self) -> closing_connection:
        return closing_connection(self.database, foreign_keys=True)

    async def save(self, record: SessionRecord) -> None:
        async with self._lock:
            await asyncio.to_thread(self._save, record)

    def _save(self, record: SessionRecord) -> None:
        working, verification, checkpoints = _encode(record)
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO sessions (session_id, workspace_id, status, working_memory,
                                          verification, checkpoints, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        workspace_id = excluded.workspace_id,
                        status = excluded.status,
                        working_memory = excluded.working_memory,
                        verification = excluded.verification,
                        checkpoints = excluded.checkpoints,
                        updated_at = excluded.updated_at
                    """,
                    (
                        record.session_id,
                        record.workspace_id,
                        record.status.value,
                        working,
                        verification,
                        checkpoints,
                        record.created_at.isoformat(),
                        record.updated_at.isoformat(),
                    ),
                )
                if record.status.value in _LIVE_STATUSES:
                    # Quien la esta ejecutando, para que otra instancia que arranque sobre
                    # la misma base no la de por interrumpida mientras sigue viva (A22).
                    connection.execute(
                        """
                        INSERT INTO session_owners (session_id, pid, host) VALUES (?, ?, ?)
                        ON CONFLICT(session_id) DO UPDATE SET pid = excluded.pid,
                                                              host = excluded.host
                        """,
                        (record.session_id, os.getpid(), _HOST),
                    )
                connection.execute(
                    "DELETE FROM session_tool_references WHERE session_id = ?",
                    (record.session_id,),
                )
                connection.executemany(
                    """
                    INSERT INTO session_tool_references
                        (session_id, store_key, media_type, size_chars, checksum)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            record.session_id,
                            reference.store_key,
                            reference.media_type,
                            reference.size_chars,
                            reference.checksum,
                        )
                        for reference in record.tool_references
                    ],
                )
        except sqlite3.Error as exc:
            raise SessionStoreError(f"Cannot persist session {record.session_id}") from exc

    async def load(self, session_id: str) -> SessionRecord | None:
        return await asyncio.to_thread(self._load, session_id)

    def _load(self, session_id: str) -> SessionRecord | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
                ).fetchone()
                if row is None:
                    return None
                references = connection.execute(
                    "SELECT * FROM session_tool_references WHERE session_id = ?",
                    (session_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise SessionStoreError(f"Cannot read session {session_id}") from exc
        return _row_to_record(row, references)

    async def list_sessions(self, status: AgentStatus | None = None) -> tuple[SessionRecord, ...]:
        return await asyncio.to_thread(self._list, status)

    def _list(self, status: AgentStatus | None) -> tuple[SessionRecord, ...]:
        query = "SELECT * FROM sessions"
        parameters: tuple[str, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            parameters = (status.value,)
        query += " ORDER BY updated_at DESC"
        try:
            with self._connect() as connection:
                rows = connection.execute(query, parameters).fetchall()
                records = []
                for row in rows:
                    references = connection.execute(
                        "SELECT * FROM session_tool_references WHERE session_id = ?",
                        (row["session_id"],),
                    ).fetchall()
                    records.append(_row_to_record(row, references))
        except sqlite3.Error as exc:
            raise SessionStoreError("Cannot list sessions") from exc
        return tuple(records)

    async def mark_interrupted(self) -> tuple[str, ...]:
        """Called at startup: nothing that was live may look finished."""
        async with self._lock:
            return await asyncio.to_thread(self._mark_interrupted)

    def _mark_interrupted(self) -> tuple[str, ...]:
        """Las sesiones vivas cuyo dueno ya no existe, y solo esas.

        Antes se marcaban todas: el escritorio o la CLI, al arrancar sobre la misma base,
        daban por recuperable el trabajo que otra instancia seguia haciendo (A22). Ahora
        se mira el proceso que la escribio; una sesion sin dueno registrado (de antes de
        esto) o sin latido en horas se recupera como antes.
        """
        placeholders = ", ".join("?" for _ in _LIVE_STATUSES)
        now = datetime.now(UTC)
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    f"""
                    SELECT s.session_id, s.updated_at, o.pid, o.host
                    FROM sessions s LEFT JOIN session_owners o ON o.session_id = s.session_id
                    WHERE s.status IN ({placeholders})
                    """,
                    _LIVE_STATUSES,
                ).fetchall()
                orphaned: list[str] = []
                for row in rows:
                    age = (now - _decode_moment(str(row["updated_at"]))).total_seconds()
                    pid = row["pid"]
                    alive = (
                        pid is not None
                        and int(pid) != os.getpid()
                        and (row["host"] != _HOST or process_alive(int(pid)))
                    )
                    if alive and age < _STALE_AFTER_SECONDS:
                        continue
                    orphaned.append(str(row["session_id"]))
                for session_id in orphaned:
                    connection.execute(
                        "UPDATE sessions SET status = ?, updated_at = ? WHERE session_id = ?",
                        (AgentStatus.RECOVERY_PENDING.value, now.isoformat(), session_id),
                    )
        except sqlite3.Error as exc:
            raise SessionStoreError("Cannot mark interrupted sessions") from exc
        return tuple(orphaned)

    async def delete(self, session_id: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._delete, session_id)

    def _delete(self, session_id: str) -> None:
        try:
            with self._connect() as connection:
                connection.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
                connection.execute(
                    "DELETE FROM session_tool_references WHERE session_id = ?", (session_id,)
                )
                connection.execute("DELETE FROM run_manifests WHERE session_id = ?", (session_id,))
        except sqlite3.Error as exc:
            raise SessionStoreError(f"Cannot delete session {session_id}") from exc

    async def save_manifest(self, session_id: str, manifest: JSONObject) -> None:
        """Lo que se autorizo para este run, escrito una vez al crearlo.

        En su propia tabla y no en la fila de la sesion: el bucle reescribe esa fila en
        cada checkpoint sin saber nada del manifiesto, y compartirla obligaria a cada
        escritura a no pisarlo.
        """
        async with self._lock:
            await asyncio.to_thread(self._save_manifest, session_id, manifest)

    def _save_manifest(self, session_id: str, manifest: JSONObject) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO run_manifests (session_id, manifest, created_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET manifest = excluded.manifest
                    """,
                    (
                        session_id,
                        json.dumps(dict(manifest), ensure_ascii=False, default=str),
                        datetime.now(UTC).isoformat(),
                    ),
                )
        except sqlite3.Error as exc:
            raise SessionStoreError(f"Cannot persist the manifest of {session_id}") from exc

    async def load_manifest(self, session_id: str) -> JSONObject | None:
        return await asyncio.to_thread(self._load_manifest, session_id)

    def _load_manifest(self, session_id: str) -> JSONObject | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT manifest FROM run_manifests WHERE session_id = ?", (session_id,)
                ).fetchone()
        except sqlite3.Error as exc:
            raise SessionStoreError(f"Cannot read the manifest of {session_id}") from exc
        if row is None:
            return None
        return _decode_json_object(str(row["manifest"]))


def _row_to_record(row: sqlite3.Row, references: Iterable[sqlite3.Row]) -> SessionRecord:
    session_id = str(row["session_id"])
    working_memory, degraded_memory = _decode_working_memory(str(row["working_memory"]), session_id)
    status, degraded_status = _decode_status(str(row["status"]))
    return SessionRecord(
        session_id=session_id,
        workspace_id=str(row["workspace_id"]),
        status=status,
        working_memory=working_memory,
        tool_references=tuple(
            ToolResultReference(
                store_key=str(reference["store_key"]),
                media_type=str(reference["media_type"]),
                size_chars=int(reference["size_chars"]),
                checksum=(
                    str(reference["checksum"]) if reference["checksum"] is not None else None
                ),
            )
            for reference in references
        ),
        verification=_decode_json_object(str(row["verification"])),
        checkpoints=_decode_checkpoints(str(row["checkpoints"])),
        degraded=degraded_memory or degraded_status,
        created_at=_decode_moment(str(row["created_at"])),
        updated_at=_decode_moment(str(row["updated_at"])),
    )


class InMemorySessionStore:
    """Test double with the same recovery semantics as the durable store."""

    def __init__(self) -> None:
        self._records: dict[str, SessionRecord] = {}
        self._manifests: dict[str, JSONObject] = {}

    async def save(self, record: SessionRecord) -> None:
        self._records[record.session_id] = record

    async def load(self, session_id: str) -> SessionRecord | None:
        return self._records.get(session_id)

    async def list_sessions(self, status: AgentStatus | None = None) -> tuple[SessionRecord, ...]:
        records = sorted(self._records.values(), key=lambda item: item.updated_at, reverse=True)
        if status is None:
            return tuple(records)
        return tuple(record for record in records if record.status is status)

    async def mark_interrupted(self) -> tuple[str, ...]:
        interrupted: list[str] = []
        for session_id, record in self._records.items():
            if record.status.value in _LIVE_STATUSES:
                self._records[session_id] = replace(
                    record,
                    status=AgentStatus.RECOVERY_PENDING,
                    updated_at=datetime.now(UTC),
                )
                interrupted.append(session_id)
        return tuple(interrupted)

    async def delete(self, session_id: str) -> None:
        self._records.pop(session_id, None)
        self._manifests.pop(session_id, None)

    async def save_manifest(self, session_id: str, manifest: JSONObject) -> None:
        self._manifests[session_id] = dict(manifest)

    async def load_manifest(self, session_id: str) -> JSONObject | None:
        stored = self._manifests.get(session_id)
        return None if stored is None else dict(stored)


__all__ = [
    "EventCheckpoint",
    "InMemorySessionStore",
    "SessionRecord",
    "SessionStore",
    "SessionStoreError",
    "SqliteSessionStore",
]
