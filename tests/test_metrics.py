"""Counting a run without being able to change it.

The collector's most important property is negative: it cannot fail a run, cannot slow one
down in any way that matters, and cannot make a run behave differently by being attached.
An observer that could raise would turn a measurement problem into an outage, and a metric
is never worth that.

The second thing under test is that the numbers are the ones the report actually asks for —
in particular that a monoagent run and a hierarchical one can be compared, which is the
question the whole architecture has to be able to answer about itself.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from athena.events import EventName, InMemoryEventBus, RuntimeEvent
from athena.metrics import (
    MetricsCollector,
    RunMetrics,
    SqliteMetricsStore,
    aggregate,
)
from athena.state import ExecutionOutcome
from athena.types import JSONValue

RUN = "run-1"


def event(name: EventName, payload: dict[str, JSONValue] | None = None) -> RuntimeEvent:
    return RuntimeEvent(name, RUN, payload or {})


def _collector(*events: RuntimeEvent) -> MetricsCollector:
    collector = MetricsCollector()
    for item in events:
        collector.observe(item)
    return collector


# ------------------------------------------------------------------ it cannot break a run


def test_a_malformed_payload_never_raises() -> None:
    """The property that makes it safe to leave switched on.

    Every payload here is wrong in a different way, and none of them is the collector's
    problem to have an opinion about.
    """
    collector = MetricsCollector()

    nonsense: list[dict[str, JSONValue]] = [
        {"input_tokens": "many"},
        {"input_tokens": True},
        {"input_tokens": -5},
        {"cost": "free"},
        {"decision": None},
        {"provider": ""},
    ]
    for payload in nonsense:
        collector.observe(event(EventName.MODEL_COMPLETED, payload))
        collector.observe(event(EventName.PERMISSION_RESOLVED, payload))
        collector.observe(event(EventName.MODEL_FAILED, payload))

    metrics = collector.get(RUN)
    assert metrics is not None
    assert metrics.input_tokens == 0, "nonsense counts as nothing, not as a crash"


def test_an_unknown_event_is_ignored_rather_than_guessed_at() -> None:
    collector = _collector(event(EventName.SESSION_PERSISTED), event(EventName.FILE_CHANGED))

    metrics = collector.get(RUN)
    assert metrics is not None
    assert metrics.model_calls == 0
    assert metrics.tool_calls == 0


def test_attaching_it_to_a_bus_changes_nothing_about_the_run() -> None:
    async def scenario() -> None:
        bus = InMemoryEventBus()
        delivered: list[RuntimeEvent] = []
        bus.subscribe(delivered.append)
        collector = MetricsCollector()
        bus.subscribe(collector.observe)

        await bus.publish(event(EventName.MODEL_STARTED))

        assert len(delivered) == 1, "the other subscriber still got its event"
        metrics = collector.get(RUN)
        assert metrics is not None
        assert metrics.model_calls == 1

    asyncio.run(scenario())


# ------------------------------------------------------------------------------ counting


def test_the_ordinary_counters_move() -> None:
    collector = _collector(
        event(EventName.MODEL_STARTED),
        event(EventName.MODEL_STARTED),
        event(EventName.TOOL_STARTED),
        event(EventName.TOOL_FAILED),
        event(EventName.PERMISSION_REQUESTED),
        event(EventName.VERIFICATION_STARTED),
        event(EventName.VERIFICATION_FAILED),
        event(EventName.CONTEXT_COMPACTED),
    )

    metrics = collector.get(RUN)
    assert metrics is not None
    assert metrics.model_calls == 2
    assert metrics.tool_calls == 1
    assert metrics.tool_failures == 1
    assert metrics.permission_requests == 1
    assert metrics.verification_runs == 1
    assert metrics.verification_failures == 1
    assert metrics.context_compactions == 1


def test_tokens_and_cost_accumulate_when_the_provider_reports_them() -> None:
    collector = _collector(
        event(EventName.MODEL_COMPLETED, {"input_tokens": 100, "output_tokens": 20, "cost": 0.01}),
        event(EventName.MODEL_COMPLETED, {"input_tokens": 50, "output_tokens": 10, "cost": 0.005}),
    )

    metrics = collector.get(RUN)
    assert metrics is not None
    assert metrics.input_tokens == 150
    assert metrics.output_tokens == 30
    assert round(metrics.estimated_cost, 4) == 0.015


def test_only_a_denial_counts_as_a_denial() -> None:
    collector = _collector(
        event(EventName.PERMISSION_RESOLVED, {"decision": "allow"}),
        event(EventName.PERMISSION_RESOLVED, {"decision": "deny"}),
    )

    metrics = collector.get(RUN)
    assert metrics is not None
    assert metrics.permission_denials == 1


def test_a_model_refusing_is_not_a_provider_failing() -> None:
    # Two different problems that would otherwise share one number: the endpoint being
    # down, and the model declining to answer.
    collector = _collector(
        event(EventName.MODEL_FAILED, {"provider": "ai_broker"}),
        event(EventName.MODEL_FAILED, {}),
    )

    metrics = collector.get(RUN)
    assert metrics is not None
    assert metrics.model_failures == 2
    assert metrics.provider_failures == 1


def test_task_events_are_counted_separately_from_subagent_ones() -> None:
    # A task uses a subagent; it is not one. Conflating them would make the graph view and
    # the metrics disagree about how much work there was.
    collector = _collector(
        event(EventName.TASK_STARTED),
        event(EventName.TASK_COMPLETED),
        event(EventName.TASK_STARTED),
        event(EventName.TASK_FAILED),
        event(EventName.SUBAGENT_STARTED),
        event(EventName.SUBAGENT_STARTED),
    )

    metrics = collector.get(RUN)
    assert metrics is not None
    assert metrics.tasks_total == 2
    assert metrics.tasks_completed == 1
    assert metrics.tasks_failed == 1
    assert metrics.subagents_spawned == 2


# ----------------------------------------------------------------------- how a run ended


def test_a_run_records_how_it_finished() -> None:
    collector = _collector(event(EventName.AGENT_STARTED), event(EventName.AGENT_COMPLETED))

    metrics = collector.get(RUN)
    assert metrics is not None
    assert metrics.status == ExecutionOutcome.COMPLETED.value
    assert metrics.succeeded
    assert metrics.completed_at is not None


def test_a_cancelled_run_is_not_recorded_as_a_failure() -> None:
    collector = _collector(event(EventName.GRAPH_CANCELLED))

    metrics = collector.get(RUN)
    assert metrics is not None
    assert metrics.status == ExecutionOutcome.CANCELLED.value
    assert not metrics.succeeded
    assert metrics.cancellations == 1


def test_an_inner_ending_does_not_overwrite_the_run_s_own() -> None:
    """A subagent finishing is not the run finishing.

    Both publish terminal events on the same bus, and the first one to arrive is the one
    that belongs to the run.
    """
    collector = _collector(
        event(EventName.GRAPH_FAILED),
        event(EventName.AGENT_COMPLETED),
    )

    metrics = collector.get(RUN)
    assert metrics is not None
    assert metrics.status == ExecutionOutcome.FAILED.value


def test_a_hierarchical_run_says_so() -> None:
    """The field the whole comparison turns on."""
    flat = _collector(event(EventName.AGENT_STARTED)).get(RUN)
    nested = _collector(event(EventName.GRAPH_STARTED)).get(RUN)

    assert flat is not None and not flat.hierarchical
    assert nested is not None and nested.hierarchical


def test_first_pass_success_is_not_the_same_as_success() -> None:
    """A run that succeeds after four repairs is a success, and not that success.

    Collapsing the two would hide exactly the effect the architecture is meant to have.
    """
    clean = _collector(event(EventName.AGENT_COMPLETED)).get(RUN)
    repaired = _collector(
        event(EventName.RECOVERY_STARTED),
        event(EventName.VERIFICATION_FAILED),
        event(EventName.AGENT_COMPLETED),
    ).get(RUN)

    assert clean is not None and clean.first_pass
    assert repaired is not None
    assert repaired.succeeded
    assert not repaired.first_pass


def test_runs_are_kept_apart() -> None:
    collector = MetricsCollector()
    collector.observe(RuntimeEvent(EventName.MODEL_STARTED, "a"))
    collector.observe(RuntimeEvent(EventName.MODEL_STARTED, "b"))
    collector.observe(RuntimeEvent(EventName.MODEL_STARTED, "b"))

    first = collector.get("a")
    second = collector.get("b")
    assert first is not None
    assert second is not None
    assert first.model_calls == 1
    assert second.model_calls == 2


# ---------------------------------------------------------------------------- aggregates


def _run(run_id: str, **fields: object) -> RunMetrics:
    metrics = RunMetrics(run_id=run_id, started_at=datetime.now(UTC) - timedelta(seconds=2))
    metrics.completed_at = datetime.now(UTC)
    for name, value in fields.items():
        setattr(metrics, name, value)
    return metrics


def test_an_empty_set_answers_zero_rather_than_raising() -> None:
    # "We have no data yet" is a legitimate thing to tell a dashboard.
    summary = aggregate([])

    assert summary.runs == 0
    assert summary.success_rate == 0.0


def test_the_rates_the_report_asks_for_are_computed() -> None:
    runs = [
        _run("a", status="completed"),
        _run("b", status="completed", repair_cycles=2),
        _run("c", status="failed", verification_failures=1),
        _run("d", status="completed", permission_requests=3, tasks_total=2),
    ]

    summary = aggregate(runs)

    assert summary.runs == 4
    assert summary.success_rate == 0.75
    assert summary.first_pass_success_rate == 0.5
    assert summary.mean_repair_cycles == 0.5
    assert summary.verification_failure_rate == 0.25
    assert summary.intervention_rate == 0.25
    assert summary.subagent_usage == 0.25


def test_intervention_rate_says_whether_it_is_autonomous_or_supervised() -> None:
    supervised = aggregate([_run("a", permission_requests=1), _run("b", permission_requests=4)])
    autonomous = aggregate([_run("a"), _run("b")])

    assert supervised.intervention_rate == 1.0
    assert autonomous.intervention_rate == 0.0


# ------------------------------------------------------------------------- persistence


def test_metrics_survive_the_process_that_measured_them(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SqliteMetricsStore(tmp_path / "metrics.db")
        await store.save(_run("a", status="completed", model_calls=7, input_tokens=1_000))

        reopened = SqliteMetricsStore(tmp_path / "metrics.db")
        loaded = await reopened.load()

        assert len(loaded) == 1
        assert loaded[0].model_calls == 7
        assert loaded[0].input_tokens == 1_000
        assert loaded[0].succeeded

    asyncio.run(scenario())


def test_saving_the_same_run_twice_updates_rather_than_duplicates(tmp_path: Path) -> None:
    # A run is saved as it progresses and again when it ends.
    async def scenario() -> None:
        store = SqliteMetricsStore(tmp_path / "metrics.db")
        await store.save(_run("a", status="running", model_calls=1))
        await store.save(_run("a", status="completed", model_calls=5))

        loaded = await store.load()

        assert len(loaded) == 1
        assert loaded[0].model_calls == 5
        assert loaded[0].status == "completed"

    asyncio.run(scenario())


def test_the_two_architectures_can_be_compared(tmp_path: Path) -> None:
    """The one query Athena has to be able to answer about itself.

    This is what turns an architectural argument into a measurement.
    """

    async def scenario() -> None:
        store = SqliteMetricsStore(tmp_path / "metrics.db")
        await store.save(_run("flat-1", status="failed", hierarchical=False, repair_cycles=3))
        await store.save(_run("flat-2", status="completed", hierarchical=False, repair_cycles=1))
        await store.save(_run("graph-1", status="completed", hierarchical=True, tasks_total=3))
        await store.save(_run("graph-2", status="completed", hierarchical=True, tasks_total=2))

        comparison = await store.compare()
        flat = comparison["monoagent"]
        nested = comparison["hierarchical"]
        assert isinstance(flat, dict)
        assert isinstance(nested, dict)

        assert flat["runs"] == 2
        assert nested["runs"] == 2
        assert flat["success_rate"] == 0.5
        assert nested["success_rate"] == 1.0
        assert nested["subagent_usage"] == 1.0

    asyncio.run(scenario())


def test_a_shape_can_be_asked_for_on_its_own(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SqliteMetricsStore(tmp_path / "metrics.db")
        await store.save(_run("flat", hierarchical=False))
        await store.save(_run("graph", hierarchical=True))

        assert [item.run_id for item in await store.load(hierarchical=True)] == ["graph"]
        assert [item.run_id for item in await store.load(hierarchical=False)] == ["flat"]

    asyncio.run(scenario())


def test_a_run_that_never_finished_still_reports_a_duration() -> None:
    # An unfinished run is the one someone is most likely to be looking at.
    metrics = RunMetrics(run_id="live", started_at=datetime.now(UTC) - timedelta(seconds=3))

    assert metrics.duration_ms >= 3_000
    assert metrics.completed_at is None
    assert not metrics.succeeded


def test_the_json_shape_is_flat_enough_to_chart() -> None:
    payload = _run("a", status="completed", model_calls=3).to_json()

    assert payload["status"] == "completed"
    assert payload["model_calls"] == 3
    assert isinstance(payload["duration_ms"], int)
    assert isinstance(payload["started_at"], str)


def test_the_shape_decision_is_recorded_for_counting() -> None:
    """Las cuatro columnas de la comparación, sin parsear prosa.

    Los códigos existen para esto: la frase que acompaña a cada decisión se reescribirá,
    y un recuento de cuántas veces un despliegue cae al bucle por no tener planificador
    no puede depender de que la redacción no cambie.
    """
    collector = MetricsCollector()
    collector.observe(
        RuntimeEvent(
            EventName.PLAN_DECIDED,
            "run-1",
            {
                "execution_mode": "auto",
                "executed_as": "direct",
                "reason_code": "plan_not_worthwhile",
                "reason": "auto -> direct: the plan holds 1 task(s) in one sequence…",
                "policy_verdict": "decompose",
                "policy_explanation": "Decomposition is worth its overhead here: …",
            },
        )
    )

    metrics = collector.all()[0]
    assert metrics.requested_mode == "auto"
    assert metrics.selected_shape == "direct"
    assert metrics.policy_verdict == "decompose"
    assert metrics.reason_code == "plan_not_worthwhile"
    # `hierarchical` es otra cosa y sigue siendo otra cosa: se observa del grafo al
    # arrancar, no de lo que se decidió. Este run decidió no ser un grafo y no lo fue.
    assert not metrics.hierarchical


def test_a_decision_with_nothing_useful_in_it_records_nothing() -> None:
    """Un payload sin los campos no deja basura en las métricas.

    `observe` no puede lanzar —lo dice su contrato— así que la otra forma de fallar sería
    guardar el `repr` de lo que hubiera y contaminar el recuento con valores que no son
    de nadie.
    """
    collector = MetricsCollector()
    collector.observe(
        RuntimeEvent(EventName.PLAN_DECIDED, "run-2", {"executed_as": 7, "reason_code": None})
    )

    metrics = collector.all()[0]
    assert metrics.selected_shape == ""
    assert metrics.reason_code == ""
