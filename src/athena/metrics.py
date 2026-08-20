"""What a run cost and what it achieved, collected from events already being published.

Athena has been unable to answer "how well does this work" — which is a problem for anyone
operating it and a bigger one for anyone trying to defend the architecture with evidence.
The numbers were always there, scattered through the event stream; nothing was counting.

The collector is a subscriber and nothing more. It cannot influence a run, cannot fail one,
and adding it changes no behaviour — which is the property that makes it safe to leave on.
Everything it knows comes from events the runtime publishes anyway, so there is no
instrumentation threaded through the loop to fall out of step with the code.

No Prometheus, no exporters, no dependency. A SQLite table answers every question below,
and a comparison between a monoagent run and a hierarchical one is a SQL query rather than
an infrastructure project.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from athena.events import EventName, RuntimeEvent
from athena.state import ExecutionOutcome
from athena.types import JSONObject


@dataclass
class RunMetrics:
    """One run, counted.

    Mutable because it is accumulated as events arrive, and written once at the end. The
    fields are the questions people actually ask, not everything that could be counted.
    """

    run_id: str
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    status: str = "running"
    #: True when the run was executed as a graph rather than as a single loop. This is the
    #: field the whole comparison in the report turns on.
    hierarchical: bool = False

    model_calls: int = 0
    model_failures: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost: float = 0.0

    tasks_total: int = 0
    tasks_completed: int = 0
    tasks_failed: int = 0
    subagents_spawned: int = 0

    repair_cycles: int = 0
    permission_requests: int = 0
    permission_denials: int = 0
    verification_runs: int = 0
    verification_failures: int = 0
    provider_failures: int = 0
    cancellations: int = 0
    context_compactions: int = 0

    @property
    def duration_ms(self) -> int:
        end = self.completed_at or datetime.now(UTC)
        return int((end - self.started_at).total_seconds() * 1000)

    @property
    def succeeded(self) -> bool:
        return self.status == ExecutionOutcome.COMPLETED.value

    @property
    def first_pass(self) -> bool:
        """Finished without needing a repair cycle or failing a verification.

        The interesting measure. A run that succeeds after four repairs is a success, and
        it is not the same success as one that got it right immediately.
        """
        return self.succeeded and self.repair_cycles == 0 and self.verification_failures == 0

    def to_json(self) -> JSONObject:
        payload = {key: value for key, value in asdict(self).items()}
        payload["started_at"] = self.started_at.isoformat()
        payload["completed_at"] = (
            None if self.completed_at is None else self.completed_at.isoformat()
        )
        payload["duration_ms"] = self.duration_ms
        return payload


@dataclass(frozen=True, slots=True)
class AggregateMetrics:
    """Several runs, compared. The shape of a result in the report."""

    runs: int
    succeeded: int
    first_pass: int
    mean_duration_ms: float
    mean_model_calls: float
    mean_tool_calls: float
    mean_repair_cycles: float
    mean_tokens: float
    verification_failure_rate: float
    provider_failure_rate: float
    intervention_rate: float
    subagent_usage: float

    @property
    def success_rate(self) -> float:
        return self.succeeded / self.runs if self.runs else 0.0

    @property
    def first_pass_success_rate(self) -> float:
        return self.first_pass / self.runs if self.runs else 0.0

    def to_json(self) -> JSONObject:
        return {
            "runs": self.runs,
            "success_rate": round(self.success_rate, 4),
            "first_pass_success_rate": round(self.first_pass_success_rate, 4),
            "mean_duration_ms": round(self.mean_duration_ms, 1),
            "mean_model_calls": round(self.mean_model_calls, 2),
            "mean_tool_calls": round(self.mean_tool_calls, 2),
            "mean_repair_cycles": round(self.mean_repair_cycles, 2),
            "mean_tokens": round(self.mean_tokens, 1),
            "verification_failure_rate": round(self.verification_failure_rate, 4),
            "provider_failure_rate": round(self.provider_failure_rate, 4),
            "intervention_rate": round(self.intervention_rate, 4),
            "subagent_usage": round(self.subagent_usage, 4),
        }


#: How each event moves a counter. A table rather than a chain of `if`s, so adding an
#: event to the vocabulary is one line here and nothing else changes.
_COUNTERS: dict[EventName, str] = {
    EventName.MODEL_STARTED: "model_calls",
    EventName.MODEL_FAILED: "model_failures",
    EventName.TOOL_STARTED: "tool_calls",
    EventName.TOOL_FAILED: "tool_failures",
    EventName.PERMISSION_REQUESTED: "permission_requests",
    EventName.VERIFICATION_STARTED: "verification_runs",
    EventName.VERIFICATION_FAILED: "verification_failures",
    EventName.SUBAGENT_STARTED: "subagents_spawned",
    EventName.CONTEXT_COMPACTED: "context_compactions",
    EventName.TASK_STARTED: "tasks_total",
    EventName.TASK_COMPLETED: "tasks_completed",
    EventName.TASK_FAILED: "tasks_failed",
    EventName.RECOVERY_STARTED: "repair_cycles",
}

_TERMINAL: dict[EventName, str] = {
    EventName.AGENT_COMPLETED: ExecutionOutcome.COMPLETED.value,
    EventName.AGENT_FAILED: ExecutionOutcome.FAILED.value,
    EventName.AGENT_CANCELLED: ExecutionOutcome.CANCELLED.value,
    EventName.GRAPH_COMPLETED: ExecutionOutcome.COMPLETED.value,
    EventName.GRAPH_FAILED: ExecutionOutcome.FAILED.value,
    EventName.GRAPH_CANCELLED: ExecutionOutcome.CANCELLED.value,
}


class MetricsCollector:
    """Subscribes to the bus and counts. It cannot affect what it measures.

    Deliberately synchronous and exception-free in the hot path: an observer that could
    raise would turn a measurement problem into a run failure, and a metric is never worth
    that.
    """

    def __init__(self) -> None:
        self._runs: dict[str, RunMetrics] = {}

    def observe(self, event: RuntimeEvent) -> None:
        """Handle one event. Never raises, whatever the payload contains."""
        metrics = self._runs.get(event.session_id)
        if metrics is None:
            metrics = RunMetrics(run_id=event.session_id)
            self._runs[event.session_id] = metrics

        if event.name is EventName.GRAPH_STARTED:
            metrics.hierarchical = True
        counter = _COUNTERS.get(event.name)
        if counter is not None:
            setattr(metrics, counter, getattr(metrics, counter) + 1)
        denied = event.payload.get("decision") == "deny"
        if event.name is EventName.PERMISSION_RESOLVED and denied:
            metrics.permission_denials += 1
        # A model failure that names a provider is a provider failure; one that does not
        # is the model refusing, which is a different thing entirely.
        named_provider = bool(event.payload.get("provider"))
        if event.name is EventName.MODEL_FAILED and named_provider:
            metrics.provider_failures += 1
        if event.name is EventName.MODEL_COMPLETED:
            metrics.input_tokens += _count(event.payload, "input_tokens")
            metrics.output_tokens += _count(event.payload, "output_tokens")
            cost = event.payload.get("cost")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                metrics.estimated_cost += float(cost)

        terminal = _TERMINAL.get(event.name)
        if terminal is not None:
            # A graph event never downgrades a run that already reported: the outer result
            # is the run's, and an inner subagent finishing is not the run finishing.
            if metrics.completed_at is None:
                metrics.status = terminal
                metrics.completed_at = event.occurred_at
            if terminal == ExecutionOutcome.CANCELLED.value:
                metrics.cancellations += 1

    def get(self, run_id: str) -> RunMetrics | None:
        return self._runs.get(run_id)

    def all(self) -> tuple[RunMetrics, ...]:
        return tuple(self._runs.values())

    def reset(self) -> None:
        self._runs.clear()


def _count(payload: JSONObject, key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def aggregate(runs: Sequence[RunMetrics]) -> AggregateMetrics:
    """Turn a set of runs into the comparison the report asks for.

    An empty set produces zeros rather than raising: "we have no data yet" is a legitimate
    answer to give a dashboard, and an exception is not.
    """
    total = len(runs)
    if total == 0:
        return AggregateMetrics(0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    return AggregateMetrics(
        runs=total,
        succeeded=sum(1 for run in runs if run.succeeded),
        first_pass=sum(1 for run in runs if run.first_pass),
        mean_duration_ms=sum(run.duration_ms for run in runs) / total,
        mean_model_calls=sum(run.model_calls for run in runs) / total,
        mean_tool_calls=sum(run.tool_calls for run in runs) / total,
        mean_repair_cycles=sum(run.repair_cycles for run in runs) / total,
        mean_tokens=sum(run.input_tokens + run.output_tokens for run in runs) / total,
        verification_failure_rate=sum(1 for run in runs if run.verification_failures) / total,
        provider_failure_rate=sum(1 for run in runs if run.provider_failures) / total,
        #: How often a person had to be asked anything. The number that says whether the
        #: agent is autonomous or merely supervised.
        intervention_rate=sum(1 for run in runs if run.permission_requests) / total,
        subagent_usage=sum(1 for run in runs if run.subagents_spawned) / total,
    )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_metrics (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    duration_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    hierarchical INTEGER NOT NULL,
    model_calls INTEGER NOT NULL,
    model_failures INTEGER NOT NULL,
    tool_calls INTEGER NOT NULL,
    tool_failures INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    estimated_cost REAL NOT NULL,
    tasks_total INTEGER NOT NULL,
    tasks_completed INTEGER NOT NULL,
    tasks_failed INTEGER NOT NULL,
    subagents_spawned INTEGER NOT NULL,
    repair_cycles INTEGER NOT NULL,
    permission_requests INTEGER NOT NULL,
    permission_denials INTEGER NOT NULL,
    verification_runs INTEGER NOT NULL,
    verification_failures INTEGER NOT NULL,
    provider_failures INTEGER NOT NULL,
    cancellations INTEGER NOT NULL,
    context_compactions INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_metrics_shape ON run_metrics(hierarchical, status);
"""

_COLUMNS = (
    "run_id",
    "started_at",
    "completed_at",
    "duration_ms",
    "status",
    "hierarchical",
    "model_calls",
    "model_failures",
    "tool_calls",
    "tool_failures",
    "input_tokens",
    "output_tokens",
    "estimated_cost",
    "tasks_total",
    "tasks_completed",
    "tasks_failed",
    "subagents_spawned",
    "repair_cycles",
    "permission_requests",
    "permission_denials",
    "verification_runs",
    "verification_failures",
    "provider_failures",
    "cancellations",
    "context_compactions",
)


class SqliteMetricsStore:
    """Keeps run metrics between processes, so a comparison spans more than one session."""

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

    async def save(self, metrics: RunMetrics) -> None:
        async with self._lock:
            await asyncio.to_thread(self._save, metrics)

    def _save(self, metrics: RunMetrics) -> None:
        values = (
            metrics.run_id,
            metrics.started_at.isoformat(),
            None if metrics.completed_at is None else metrics.completed_at.isoformat(),
            metrics.duration_ms,
            metrics.status,
            int(metrics.hierarchical),
            metrics.model_calls,
            metrics.model_failures,
            metrics.tool_calls,
            metrics.tool_failures,
            metrics.input_tokens,
            metrics.output_tokens,
            metrics.estimated_cost,
            metrics.tasks_total,
            metrics.tasks_completed,
            metrics.tasks_failed,
            metrics.subagents_spawned,
            metrics.repair_cycles,
            metrics.permission_requests,
            metrics.permission_denials,
            metrics.verification_runs,
            metrics.verification_failures,
            metrics.provider_failures,
            metrics.cancellations,
            metrics.context_compactions,
        )
        assignments = ", ".join(f"{name} = excluded.{name}" for name in _COLUMNS[1:])
        with self._connect() as connection:
            connection.execute(
                f"INSERT INTO run_metrics ({', '.join(_COLUMNS)}) "
                f"VALUES ({', '.join('?' * len(_COLUMNS))}) "
                f"ON CONFLICT(run_id) DO UPDATE SET {assignments}",
                values,
            )

    async def load(self, *, hierarchical: bool | None = None) -> tuple[RunMetrics, ...]:
        """Every run, or only the ones of one shape. The comparison the report wants."""
        return await asyncio.to_thread(self._load, hierarchical)

    def _load(self, hierarchical: bool | None) -> tuple[RunMetrics, ...]:
        with self._connect() as connection:
            if hierarchical is None:
                rows = connection.execute(
                    "SELECT * FROM run_metrics ORDER BY started_at"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM run_metrics WHERE hierarchical = ? ORDER BY started_at",
                    (int(hierarchical),),
                ).fetchall()
        return tuple(_metrics_from(row) for row in rows)

    async def compare(self) -> JSONObject:
        """Monoagent against hierarchical, side by side.

        The one query the architecture has to be able to answer about itself.
        """
        flat = aggregate(await self.load(hierarchical=False))
        nested = aggregate(await self.load(hierarchical=True))
        return {"monoagent": flat.to_json(), "hierarchical": nested.to_json()}


def _metrics_from(row: sqlite3.Row) -> RunMetrics:
    completed = row["completed_at"]
    metrics = RunMetrics(
        run_id=str(row["run_id"]),
        started_at=_parse(str(row["started_at"])),
        completed_at=None if completed is None else _parse(str(completed)),
        status=str(row["status"]),
        hierarchical=bool(row["hierarchical"]),
    )
    for name in _COLUMNS[6:]:
        setattr(metrics, name, row[name])
    return metrics


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


__all__ = [
    "AggregateMetrics",
    "MetricsCollector",
    "RunMetrics",
    "SqliteMetricsStore",
    "aggregate",
]
