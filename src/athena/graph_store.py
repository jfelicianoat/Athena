"""Plans that survive the process that made them.

`PlanBoard` holds the graph in memory, which is right for a channel that wants to draw it
and wrong for a runtime that might be restarted mid-execution. Without this, a crash halfway
through a twelve-task plan loses the plan — and with it, which tasks had already been proved.
The session store keeps the run; nothing kept what the run was doing.

The rule on reload is the same one `SessionStore`, `TaskManager` and `AgentStatus` already
follow, and it is the reason this module cannot be a plain serialiser: a task that was
`RUNNING` when the process died is not `COMPLETED` and not `FAILED`. It is
`RECOVERY_PENDING`, because nobody watched it finish and re-running it blindly could repeat
a side effect that already happened once.

What is stored is the plan and its progress, not a transcript. Evidence that was already
produced is kept — it was paid for — but nothing here reconstructs a conversation.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from athena.errors import AthenaRuntimeError
from athena.planning import (
    PlanningLimits,
    PlanStatus,
    TaskGraph,
    TaskNode,
)
from athena.subagents import SubagentRole
from athena.types import JSONObject


class GraphStoreError(AthenaRuntimeError):
    code = "graph_store_error"


@dataclass(frozen=True, slots=True)
class StoredPlan:
    """A plan as it was last seen, and what a restart did to it."""

    run_id: str
    graph: TaskGraph
    objective: str = ""
    interrupted: tuple[str, ...] = ()
    #: Cuándo se escribió. Por defecto, ahora: un plan sin fecha no se puede ordenar ni
    #: juzgar por antigüedad, y una fecha mínima fingiría que es viejísimo.
    saved_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def needs_attention(self) -> bool:
        return bool(self.interrupted)

    def to_json(self) -> JSONObject:
        return {
            "run_id": self.run_id,
            "objective": self.objective,
            "interrupted": list(self.interrupted),
            "tasks": len(self.graph),
            "saved_at": self.saved_at.isoformat(),
        }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    run_id TEXT PRIMARY KEY,
    objective TEXT NOT NULL,
    limits_json TEXT NOT NULL,
    tasks_json TEXT NOT NULL,
    saved_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_plans_open ON plans(closed_at, saved_at DESC);
"""


class SqliteGraphStore:
    """Plans, persisted. Blocking work runs off the event loop."""

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

    async def save(self, run_id: str, graph: TaskGraph, *, objective: str = "") -> None:
        """Write the plan and its current progress.

        Called on every transition rather than only at the end. A plan saved once at the
        start would survive a restart and be wrong about everything it had already done,
        which is worse than not surviving at all.
        """
        async with self._lock:
            await asyncio.to_thread(self._save, run_id, graph, objective)

    def _save(self, run_id: str, graph: TaskGraph, objective: str) -> None:
        tasks = json.dumps([node.to_json() for node in graph.nodes], ensure_ascii=False)
        limits = json.dumps(
            {
                "max_depth": graph.limits.max_depth,
                "max_tasks": graph.limits.max_tasks,
                "max_children": graph.limits.max_children,
                "max_total_attempts": graph.limits.max_total_attempts,
            }
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO plans (run_id, objective, limits_json, tasks_json, saved_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET "
                "objective = excluded.objective, limits_json = excluded.limits_json, "
                "tasks_json = excluded.tasks_json, saved_at = excluded.saved_at, "
                "closed_at = NULL",
                (run_id, objective, limits, tasks, _now()),
            )

    async def close(self, run_id: str) -> None:
        """A finished plan stops being something to recover.

        Kept rather than deleted: what a run planned is worth reading afterwards, and a
        row is cheap next to the evidence it points at.
        """
        async with self._lock:
            await asyncio.to_thread(self._close, run_id)

    def _close(self, run_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE plans SET closed_at = ? WHERE run_id = ? AND closed_at IS NULL",
                (_now(), run_id),
            )

    # -- reading -----------------------------------------------------------

    async def load(self, run_id: str) -> StoredPlan | None:
        """The plan as it was, with nothing reinterpreted."""
        return await asyncio.to_thread(self._load, run_id, False)

    async def recover(self, run_id: str) -> StoredPlan | None:
        """The plan, with anything that was running marked as of unknown outcome.

        The distinction from `load` is deliberate and is the reason both exist. Reading a
        plan to display it must not change it; reading one to resume execution must, or
        the runtime would restart a task it has no evidence about as though nothing had
        happened.
        """
        async with self._lock:
            stored = await asyncio.to_thread(self._load, run_id, True)
            if stored is not None and stored.interrupted:
                await asyncio.to_thread(self._save, run_id, stored.graph, stored.objective)
            return stored

    def _load(self, run_id: str, mark: bool) -> StoredPlan | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM plans WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        graph = _graph_from(str(row["tasks_json"]), str(row["limits_json"]))
        interrupted = graph.mark_interrupted() if mark else ()
        return StoredPlan(
            run_id=str(row["run_id"]),
            graph=graph,
            objective=str(row["objective"]),
            interrupted=interrupted,
            saved_at=_parse(str(row["saved_at"])),
        )

    async def open_plans(self, limit: int = 20) -> tuple[str, ...]:
        """Runs whose plan was never closed. The list a restart works through."""
        return await asyncio.to_thread(self._open_plans, limit)

    def _open_plans(self, limit: int) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT run_id FROM plans WHERE closed_at IS NULL ORDER BY saved_at DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        return tuple(str(row["run_id"]) for row in rows)


def _graph_from(tasks_json: str, limits_json: str) -> TaskGraph:
    """Rebuild a graph through the same validation a fresh one goes through.

    Not a shortcut back into `TaskGraph.__init__`. A stored plan is input like any other —
    the file could have been edited, or written by a version that allowed something this
    one does not — and there is exactly one definition of a valid graph.

    Collapsing is off: it already ran when the plan was built, and re-running it on reload
    would rewrite a graph whose task ids other records already point at.
    """
    try:
        raw_tasks = json.loads(tasks_json)
        raw_limits = json.loads(limits_json)
    except json.JSONDecodeError as exc:
        raise GraphStoreError("The stored plan is not readable") from exc
    if not isinstance(raw_tasks, list) or not isinstance(raw_limits, dict):
        raise GraphStoreError("The stored plan has the wrong shape")
    limits = PlanningLimits(
        max_depth=int(raw_limits.get("max_depth", 3)),
        max_tasks=int(raw_limits.get("max_tasks", 32)),
        max_children=int(raw_limits.get("max_children", 8)),
        max_total_attempts=int(raw_limits.get("max_total_attempts", 64)),
    )
    return TaskGraph.build(
        (_node_from(entry) for entry in raw_tasks),
        limits,
        collapse_single_children=False,
    )


def _node_from(entry: object) -> TaskNode:
    if not isinstance(entry, dict):
        raise GraphStoreError("A stored task is not an object")
    try:
        return TaskNode(
            id=str(entry["id"]),
            goal=str(entry["goal"]),
            expected_output=str(entry["expected_output"]),
            parent_id=entry.get("parent_id") or None,
            inputs=_strings(entry.get("inputs")),
            acceptance_criteria=_strings(entry.get("acceptance_criteria")),
            dependencies=_strings(entry.get("dependencies")),
            suggested_role=SubagentRole(entry.get("suggested_role", "coder")),
            toolsets=_strings(entry.get("toolsets")),
            status=PlanStatus(entry.get("status", "pending")),
            attempts=int(entry.get("attempts", 0)),
            verification=entry.get("verification"),
        )
    except (KeyError, ValueError) as exc:
        raise GraphStoreError(f"A stored task is unreadable: {exc}") from exc


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return ()
    return tuple(str(item) for item in value if str(item))


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


__all__ = ["GraphStoreError", "SqliteGraphStore", "StoredPlan"]
