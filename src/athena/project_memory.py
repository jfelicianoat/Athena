"""Knowledge that outlives a session, and the rules that keep it worth having.

`ProjectMemory` has been a bare protocol since H4 with nothing behind it, which meant
Athena started every session knowing nothing it had ever learned. This implements it — and
most of the work is in refusing things, because a memory that accumulates freely stops
being knowledge and becomes a second, staler copy of the repository.

Three rules do that work.

**A model's conclusion is not a fact.** Everything the agent produces enters as `PROPOSED`.
It becomes `VERIFIED` only when something checked it, and `USER_CONFIRMED` only when a
person said so. Writing an LLM's inference straight in as truth is how a memory ends up
confidently wrong about a codebase that changed underneath it.

**Every item says where it came from and when.** A memory is a hint about the past; the
repository is the present. An item that cannot be dated or traced cannot be judged stale,
and an undatable hint is indistinguishable from a rumour.

**Superseding, not overwriting.** When something is learned that replaces an older belief,
the old one is marked rather than deleted, so "we used to think X" survives — which is
exactly what a person debugging a wrong decision needs.

Retrieval is selective on purpose. Nothing here ever hands back the whole store: a context
builder that loaded all of memory would spend the window on things the current task has no
use for.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import uuid4

from athena.errors import AthenaRuntimeError
from athena.types import JSONObject


class MemoryError_(AthenaRuntimeError):
    code = "project_memory_error"


class MemoryKind(StrEnum):
    """What sort of thing is being remembered.

    A closed set, because retrieval filters on it and an open one would degrade into free
    tagging — at which point nothing can be asked for reliably.
    """

    ARCHITECTURE_DECISION = "architecture_decision"
    PROJECT_CONVENTION = "project_convention"
    #: A command that was actually run and actually worked. The most useful kind, and the
    #: one that goes stale fastest.
    VERIFIED_COMMAND = "verified_command"
    KNOWN_CONSTRAINT = "known_constraint"
    DOMAIN_FACT = "domain_fact"
    USER_CONFIRMED_FACT = "user_confirmed_fact"
    ENVIRONMENT_FACT = "environment_fact"


class VerificationState(StrEnum):
    """How much weight an item has earned.

    The ordering matters and is the point of the type: `PROPOSED` is what a model said,
    `VERIFIED` is what something checked, `USER_CONFIRMED` is what a person stood behind.
    """

    PROPOSED = "proposed"
    VERIFIED = "verified"
    USER_CONFIRMED = "user_confirmed"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    #: Replaced by a newer item, and kept so "we used to think X" survives.
    SUPERSEDED = "superseded"
    #: Deliberately retired. Also kept, because deleting hides that it was ever believed.
    FORGOTTEN = "forgotten"


class MemoryScope(StrEnum):
    PROJECT = "project"
    WORKSPACE = "workspace"
    GLOBAL = "global"


@dataclass(frozen=True, slots=True)
class ProjectMemoryItem:
    """One remembered thing, with everything needed to judge whether to believe it."""

    id: str
    project_id: str
    kind: MemoryKind
    content: str
    source: str
    source_reference: str | None = None
    confidence: float = 0.5
    verification_state: VerificationState = VerificationState.PROPOSED
    scope: MemoryScope = MemoryScope.PROJECT
    status: MemoryStatus = MemoryStatus.ACTIVE
    supersedes: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise MemoryError_("A memory needs content")
        if not self.source.strip():
            # An untraceable item cannot be judged, and an unjudgeable hint is a rumour.
            raise MemoryError_("A memory needs a source")
        if not 0.0 <= self.confidence <= 1.0:
            raise MemoryError_("Confidence is a probability, not a score")

    @property
    def is_active(self) -> bool:
        return self.status is MemoryStatus.ACTIVE

    def age(self, *, now: datetime | None = None) -> timedelta:
        return (now or datetime.now(UTC)) - self.created_at

    def is_stale(self, *, older_than: timedelta, now: datetime | None = None) -> bool:
        """A memory is a hint about the past. This is how old the hint is.

        Deliberately a question the caller asks rather than a state the store maintains:
        how old is too old depends on the kind of thing, and the store does not know.
        """
        return self.age(now=now) > older_than

    def to_json(self) -> JSONObject:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "kind": self.kind.value,
            "content": self.content,
            "source": self.source,
            "source_reference": self.source_reference,
            "confidence": round(self.confidence, 3),
            "verification_state": self.verification_state.value,
            "scope": self.scope.value,
            "status": self.status.value,
            "supersedes": self.supersedes,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@runtime_checkable
class ProjectMemoryStore(Protocol):
    """The operations a memory has to support to be worth trusting.

    `propose` and `approve` are separate calls because they are separate decisions made by
    different parties. A single `remember` that took a confidence argument would let the
    agent grade its own homework.
    """

    async def propose(
        self,
        project_id: str,
        kind: MemoryKind,
        content: str,
        *,
        source: str,
        source_reference: str | None = None,
        scope: MemoryScope = MemoryScope.PROJECT,
        supersedes: str | None = None,
    ) -> ProjectMemoryItem: ...

    async def approve(
        self, item_id: str, *, state: VerificationState, confidence: float | None = None
    ) -> ProjectMemoryItem: ...

    async def update(self, item_id: str, content: str, *, source: str) -> ProjectMemoryItem: ...

    async def forget(self, item_id: str) -> bool: ...

    async def search(
        self,
        project_id: str,
        query: str,
        *,
        limit: int = 10,
        kinds: Sequence[MemoryKind] | None = None,
        minimum_state: VerificationState = VerificationState.PROPOSED,
    ) -> tuple[ProjectMemoryItem, ...]: ...

    async def get(self, item_id: str) -> ProjectMemoryItem | None: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_memory (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    source TEXT NOT NULL,
    source_reference TEXT,
    confidence REAL NOT NULL,
    verification_state TEXT NOT NULL,
    scope TEXT NOT NULL,
    status TEXT NOT NULL,
    supersedes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_project ON project_memory(project_id, status);
CREATE INDEX IF NOT EXISTS idx_memory_kind ON project_memory(project_id, kind, status);
"""

#: Ranking weight per verification state. A person's word outranks a check, which outranks
#: a model's guess — and the ordering is arithmetic so retrieval cannot quietly invert it.
_STATE_WEIGHT = {
    VerificationState.PROPOSED: 0.0,
    VerificationState.VERIFIED: 2.0,
    VerificationState.USER_CONFIRMED: 4.0,
}

_STATE_ORDER = {
    VerificationState.PROPOSED: 0,
    VerificationState.VERIFIED: 1,
    VerificationState.USER_CONFIRMED: 2,
}

_WORD = re.compile(r"[a-z0-9_]+")


class SqliteProjectMemory:
    """The memory, persisted. Blocking work runs off the event loop."""

    def __init__(self, database: Path | str) -> None:
        self.database = Path(database)
        if str(self.database) != ":memory:":
            self.database.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    # -- writing -----------------------------------------------------------

    async def propose(
        self,
        project_id: str,
        kind: MemoryKind,
        content: str,
        *,
        source: str,
        source_reference: str | None = None,
        scope: MemoryScope = MemoryScope.PROJECT,
        supersedes: str | None = None,
    ) -> ProjectMemoryItem:
        """Record something as *proposed*. There is no way to write a fact directly.

        This is the only entry point, and it always produces `PROPOSED`. An agent that
        could write `VERIFIED` would be grading its own homework, and the whole distinction
        would collapse into decoration.
        """
        item = ProjectMemoryItem(
            id=str(uuid4()),
            project_id=project_id,
            kind=kind,
            content=content.strip(),
            source=source,
            source_reference=source_reference,
            scope=scope,
            supersedes=supersedes,
        )
        async with self._lock:
            await asyncio.to_thread(self._insert, item)
        return item

    def _insert(self, item: ProjectMemoryItem) -> None:
        with self._connect() as connection:
            if item.supersedes is not None:
                replaced = connection.execute(
                    "UPDATE project_memory SET status = ?, updated_at = ? "
                    "WHERE id = ? AND status = ?",
                    (
                        MemoryStatus.SUPERSEDED.value,
                        _now(),
                        item.supersedes,
                        MemoryStatus.ACTIVE.value,
                    ),
                )
                if replaced.rowcount == 0:
                    raise MemoryError_(
                        "Nothing active to supersede",
                        details={"supersedes": item.supersedes},
                    )
            connection.execute(
                "INSERT INTO project_memory (id, project_id, kind, content, source, "
                "source_reference, confidence, verification_state, scope, status, "
                "supersedes, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    item.project_id,
                    item.kind.value,
                    item.content,
                    item.source,
                    item.source_reference,
                    item.confidence,
                    item.verification_state.value,
                    item.scope.value,
                    item.status.value,
                    item.supersedes,
                    item.created_at.isoformat(),
                    item.updated_at.isoformat(),
                ),
            )

    async def approve(
        self, item_id: str, *, state: VerificationState, confidence: float | None = None
    ) -> ProjectMemoryItem:
        """Raise an item's standing. It can only ever go up.

        Demotion is not offered because it is not a thing that happens: a belief that turns
        out to be wrong is superseded or forgotten, both of which keep the record. Silently
        downgrading would leave an item that once looked trustworthy with no trace of why
        it stopped.
        """
        async with self._lock:
            return await asyncio.to_thread(self._approve, item_id, state, confidence)

    def _approve(
        self, item_id: str, state: VerificationState, confidence: float | None
    ) -> ProjectMemoryItem:
        current = self._load(item_id)
        if current is None:
            raise MemoryError_("No such memory", details={"id": item_id})
        if _STATE_ORDER[state] < _STATE_ORDER[current.verification_state]:
            raise MemoryError_(
                "A memory's standing cannot be lowered; supersede or forget it instead",
                details={"id": item_id, "from": current.verification_state.value},
            )
        graded = confidence if confidence is not None else max(current.confidence, 0.8)
        with self._connect() as connection:
            connection.execute(
                "UPDATE project_memory SET verification_state = ?, confidence = ?, "
                "updated_at = ? WHERE id = ?",
                (state.value, graded, _now(), item_id),
            )
        updated = self._load(item_id)
        assert updated is not None
        return updated

    async def update(self, item_id: str, content: str, *, source: str) -> ProjectMemoryItem:
        """Correct an item by superseding it, never by editing it in place.

        Editing would rewrite history: the old wording would vanish along with the fact
        that anyone ever believed it, which is what a person debugging a wrong decision is
        looking for.
        """
        current = await self.get(item_id)
        if current is None:
            raise MemoryError_("No such memory", details={"id": item_id})
        return await self.propose(
            current.project_id,
            current.kind,
            content,
            source=source,
            source_reference=current.source_reference,
            scope=current.scope,
            supersedes=item_id,
        )

    async def forget(self, item_id: str) -> bool:
        """Retire an item. It stops being retrievable; it does not stop having existed."""
        async with self._lock:
            return await asyncio.to_thread(self._forget, item_id)

    def _forget(self, item_id: str) -> bool:
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE project_memory SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                (MemoryStatus.FORGOTTEN.value, _now(), item_id, MemoryStatus.ACTIVE.value),
            )
            return changed.rowcount == 1

    # -- reading -----------------------------------------------------------

    async def get(self, item_id: str) -> ProjectMemoryItem | None:
        return await asyncio.to_thread(self._load, item_id)

    def _load(self, item_id: str) -> ProjectMemoryItem | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM project_memory WHERE id = ?", (item_id,)
            ).fetchone()
        return None if row is None else _item_from(row)

    async def search(
        self,
        project_id: str,
        query: str,
        *,
        limit: int = 10,
        kinds: Sequence[MemoryKind] | None = None,
        minimum_state: VerificationState = VerificationState.PROPOSED,
    ) -> tuple[ProjectMemoryItem, ...]:
        """Selective retrieval. The whole store is never returned.

        A context builder that loaded everything would spend the window on items the
        current task has no use for, which is the failure mode that makes long-lived memory
        a liability rather than an asset.
        """
        return await asyncio.to_thread(self._search, project_id, query, limit, kinds, minimum_state)

    def _search(
        self,
        project_id: str,
        query: str,
        limit: int,
        kinds: Sequence[MemoryKind] | None,
        minimum_state: VerificationState,
    ) -> tuple[ProjectMemoryItem, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM project_memory WHERE project_id = ? AND status = ?",
                (project_id, MemoryStatus.ACTIVE.value),
            ).fetchall()
        candidates = [_item_from(row) for row in rows]
        wanted = None if kinds is None else set(kinds)
        floor = _STATE_ORDER[minimum_state]
        terms = set(_WORD.findall(query.casefold()))
        scored: list[tuple[float, ProjectMemoryItem]] = []
        for item in candidates:
            if wanted is not None and item.kind not in wanted:
                continue
            if _STATE_ORDER[item.verification_state] < floor:
                continue
            score = _score(item, terms)
            if score > 0:
                scored.append((score, item))
        scored.sort(key=lambda pair: (-pair[0], -pair[1].created_at.timestamp()))
        return tuple(item for _, item in scored[: max(1, limit)])

    async def active(self, project_id: str, *, limit: int = 50) -> tuple[ProjectMemoryItem, ...]:
        """Everything currently believed, newest first. For an operator, not for a prompt."""
        return await asyncio.to_thread(self._active, project_id, limit)

    def _active(self, project_id: str, limit: int) -> tuple[ProjectMemoryItem, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM project_memory WHERE project_id = ? AND status = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (project_id, MemoryStatus.ACTIVE.value, max(1, limit)),
            ).fetchall()
        return tuple(_item_from(row) for row in rows)

    async def history(self, item_id: str) -> tuple[ProjectMemoryItem, ...]:
        """An item and everything it replaced, newest first.

        This is what "we used to think X" looks like when someone asks.
        """
        chain: list[ProjectMemoryItem] = []
        current = await self.get(item_id)
        seen: set[str] = set()
        while current is not None and current.id not in seen:
            seen.add(current.id)
            chain.append(current)
            current = None if current.supersedes is None else await self.get(current.supersedes)
        return tuple(chain)


def _score(item: ProjectMemoryItem, terms: set[str]) -> float:
    """Word overlap, weighted by how much the item has earned.

    Deliberately not embeddings: the core declares no dependencies, and a memory that
    needed a model to be searched could not be read by a deployment without one. Overlap
    is crude, and it is honest about being crude.
    """
    if not terms:
        return 0.0
    words = set(_WORD.findall(item.content.casefold()))
    overlap = len(terms & words)
    if overlap == 0:
        return 0.0
    return overlap + _STATE_WEIGHT[item.verification_state] + item.confidence


def _item_from(row: sqlite3.Row) -> ProjectMemoryItem:
    return ProjectMemoryItem(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        kind=MemoryKind(row["kind"]),
        content=str(row["content"]),
        source=str(row["source"]),
        source_reference=None if row["source_reference"] is None else str(row["source_reference"]),
        confidence=float(row["confidence"]),
        verification_state=VerificationState(row["verification_state"]),
        scope=MemoryScope(row["scope"]),
        status=MemoryStatus(row["status"]),
        supersedes=None if row["supersedes"] is None else str(row["supersedes"]),
        created_at=_parse(str(row["created_at"])),
        updated_at=_parse(str(row["updated_at"])),
    )


def render_for_context(items: Iterable[ProjectMemoryItem]) -> str:
    """How retrieved memory reaches a prompt: as hints, labelled with their standing.

    The labels are not decoration. A model told "the build command is X" behaves
    differently from one told "somebody once proposed that the build command is X", and the
    second is what an unverified item actually is.
    """
    lines: list[str] = []
    for item in items:
        label = {
            VerificationState.PROPOSED: "unverified",
            VerificationState.VERIFIED: "verified",
            VerificationState.USER_CONFIRMED: "confirmed by the user",
        }[item.verification_state]
        lines.append(f"- [{item.kind.value}, {label}] {item.content}")
    if not lines:
        return ""
    return (
        "What Athena remembers about this project. These are hints from earlier sessions, "
        "not the current state — the repository is the source of truth.\n" + "\n".join(lines)
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


__all__ = [
    "MemoryError_",
    "MemoryKind",
    "MemoryScope",
    "MemoryStatus",
    "ProjectMemoryItem",
    "ProjectMemoryStore",
    "SqliteProjectMemory",
    "VerificationState",
    "render_for_context",
]
