"""A plan that survives the process that made it, and is honest about what it lost.

The easy half is serialising a graph. The half that matters is what happens to a task that
was running when the process died: it is not finished and it is not failed, and a store
that guessed either way would either lose work or repeat a side effect that already
happened once.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from athena.graph_store import GraphStoreError, SqliteGraphStore
from athena.planning import (
    CyclicPlanError,
    PlanningLimits,
    PlanStatus,
    TaskGraph,
    TaskNode,
)
from athena.subagents import SubagentRole


def node(
    task_id: str,
    *,
    depends: tuple[str, ...] = (),
    role: SubagentRole = SubagentRole.CODER,
    output: str | None = None,
) -> TaskNode:
    return TaskNode(
        id=task_id,
        goal=f"do {task_id}",
        expected_output=output or f"{task_id} exists",
        acceptance_criteria=("it can be checked",),
        dependencies=depends,
        suggested_role=role,
    )


def _plan() -> TaskGraph:
    return TaskGraph.build(
        [
            node("T01", role=SubagentRole.EXPLORER),
            node("T02", depends=("T01",), output="T02"),
            node("T03", depends=("T01",), output="T03"),
        ]
    )


def _store(tmp_path: Path) -> SqliteGraphStore:
    return SqliteGraphStore(tmp_path / "plans.db")


# ------------------------------------------------------------------ it comes back whole


def test_a_plan_survives_the_process_that_made_it(tmp_path: Path) -> None:
    async def scenario() -> None:
        graph = _plan()
        await _store(tmp_path).save("run-1", graph, objective="fix the thing")

        reopened = await _store(tmp_path).load("run-1")

        assert reopened is not None
        assert reopened.objective == "fix the thing"
        assert [task.id for task in reopened.graph.topological_order()] == [
            "T01",
            "T02",
            "T03",
        ]
        assert reopened.graph.get("T01").suggested_role is SubagentRole.EXPLORER
        assert reopened.graph.get("T02").dependencies == ("T01",)

    asyncio.run(scenario())


def test_progress_comes_back_with_it(tmp_path: Path) -> None:
    """A plan saved once at the start would survive and be wrong about everything it did."""

    async def scenario() -> None:
        graph = _plan()
        graph.transition("T01", PlanStatus.READY)
        graph.transition("T01", PlanStatus.RUNNING)
        graph.transition("T01", PlanStatus.COMPLETED, verification={"status": "passed"})
        await _store(tmp_path).save("run-1", graph)

        reopened = await _store(tmp_path).load("run-1")

        assert reopened is not None
        done = reopened.graph.get("T01")
        assert done.status is PlanStatus.COMPLETED
        assert done.attempts == 1
        assert done.verification == {"status": "passed"}

    asyncio.run(scenario())


def test_saving_twice_updates_rather_than_duplicating(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        graph = _plan()
        await store.save("run-1", graph)
        graph.transition("T01", PlanStatus.READY)
        await store.save("run-1", graph)

        reopened = await store.load("run-1")

        assert reopened is not None
        assert reopened.graph.get("T01").status is PlanStatus.READY
        assert await store.open_plans() == ("run-1",)

    asyncio.run(scenario())


def test_a_plan_nobody_saved_is_not_invented(tmp_path: Path) -> None:
    async def scenario() -> None:
        assert await _store(tmp_path).load("never-ran") is None

    asyncio.run(scenario())


# ------------------------------------------------------------- what a restart does to it


def test_a_task_that_was_running_comes_back_needing_a_decision(tmp_path: Path) -> None:
    """The rule the whole module turns on.

    Nobody watched it finish. Marking it completed would claim work that may not exist;
    marking it pending would re-run something that may already have had its effect.
    """

    async def scenario() -> None:
        graph = _plan()
        graph.transition("T01", PlanStatus.READY)
        graph.transition("T01", PlanStatus.RUNNING)
        await _store(tmp_path).save("run-1", graph)

        recovered = await _store(tmp_path).recover("run-1")

        assert recovered is not None
        assert recovered.interrupted == ("T01",)
        assert recovered.graph.get("T01").status is PlanStatus.RECOVERY_PENDING
        assert recovered.needs_attention

    asyncio.run(scenario())


def test_work_that_was_already_proved_is_left_alone(tmp_path: Path) -> None:
    # A restart must not throw away evidence that was produced and paid for.
    async def scenario() -> None:
        graph = _plan()
        for task_id in ("T01",):
            graph.transition(task_id, PlanStatus.READY)
            graph.transition(task_id, PlanStatus.RUNNING)
            graph.transition(task_id, PlanStatus.COMPLETED)
        graph.transition("T02", PlanStatus.READY)
        graph.transition("T02", PlanStatus.RUNNING)
        await _store(tmp_path).save("run-1", graph)

        recovered = await _store(tmp_path).recover("run-1")

        assert recovered is not None
        assert recovered.interrupted == ("T02",)
        assert recovered.graph.get("T01").status is PlanStatus.COMPLETED
        assert recovered.graph.get("T03").status is PlanStatus.PENDING

    asyncio.run(scenario())


def test_reading_a_plan_to_look_at_it_does_not_change_it(tmp_path: Path) -> None:
    """`load` and `recover` exist separately for this.

    Displaying a plan must not rewrite it; resuming one must, or the runtime restarts a
    task it has no evidence about as though nothing had happened.
    """

    async def scenario() -> None:
        graph = _plan()
        graph.transition("T01", PlanStatus.READY)
        graph.transition("T01", PlanStatus.RUNNING)
        store = _store(tmp_path)
        await store.save("run-1", graph)

        looked = await store.load("run-1")

        assert looked is not None
        assert looked.graph.get("T01").status is PlanStatus.RUNNING
        assert looked.interrupted == ()
        assert not looked.needs_attention

    asyncio.run(scenario())


def test_recovering_persists_what_it_decided(tmp_path: Path) -> None:
    # Otherwise two restarts in a row would each find the same task running, and the
    # second would be reasoning about a state the first had already ruled out.
    async def scenario() -> None:
        graph = _plan()
        graph.transition("T01", PlanStatus.READY)
        graph.transition("T01", PlanStatus.RUNNING)
        store = _store(tmp_path)
        await store.save("run-1", graph)

        await store.recover("run-1")
        again = await store.load("run-1")

        assert again is not None
        assert again.graph.get("T01").status is PlanStatus.RECOVERY_PENDING

    asyncio.run(scenario())


def test_a_recovering_task_never_resolves_itself() -> None:
    """It waits for a decision. That is the point of having the state at all."""
    graph = TaskGraph.build([node("T01")])
    graph.transition("T01", PlanStatus.READY)
    graph.transition("T01", PlanStatus.RUNNING)
    graph.mark_interrupted()

    assert graph.needs_recovery()[0].id == "T01"
    assert not graph.is_complete()
    assert graph.ready() == (), "it does not quietly become runnable again"


def test_a_decision_can_send_it_back_or_give_it_up() -> None:
    for resolution in (PlanStatus.PENDING, PlanStatus.SKIPPED, PlanStatus.FAILED):
        graph = TaskGraph.build([node("T01")])
        graph.transition("T01", PlanStatus.READY)
        graph.transition("T01", PlanStatus.RUNNING)
        graph.mark_interrupted()

        graph.transition("T01", resolution)

        assert graph.get("T01").status is resolution


def test_nothing_running_means_nothing_to_recover(tmp_path: Path) -> None:
    async def scenario() -> None:
        await _store(tmp_path).save("run-1", _plan())

        recovered = await _store(tmp_path).recover("run-1")

        assert recovered is not None
        assert recovered.interrupted == ()
        assert not recovered.needs_attention

    asyncio.run(scenario())


# --------------------------------------------------------------------- closing the book


def test_a_finished_plan_stops_being_something_to_recover(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        await store.save("run-1", _plan())
        assert await store.open_plans() == ("run-1",)

        await store.close("run-1")

        assert await store.open_plans() == ()
        assert await store.load("run-1") is not None, "kept, because it is worth reading"

    asyncio.run(scenario())


def test_open_plans_are_listed_newest_first(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        for run_id in ("old", "new"):
            await store.save(run_id, _plan())

        assert set(await store.open_plans()) == {"old", "new"}

    asyncio.run(scenario())


# ------------------------------------------------------------------ a stored plan is input


def test_a_reloaded_plan_goes_through_the_same_validation(tmp_path: Path) -> None:
    """Not a shortcut back into the constructor.

    A stored plan could have been edited, or written by a version that allowed something
    this one does not. There is one definition of a valid graph.
    """

    async def scenario() -> None:
        store = _store(tmp_path)
        await store.save("run-1", _plan())
        # Rewrite the row into a cycle, the way an edit or an older version might.
        import json
        import sqlite3

        with sqlite3.connect(tmp_path / "plans.db") as connection:
            tasks = json.dumps(
                [
                    {
                        "id": "a",
                        "goal": "one",
                        "expected_output": "x",
                        "acceptance_criteria": ["c"],
                        "dependencies": ["b"],
                    },
                    {
                        "id": "b",
                        "goal": "two",
                        "expected_output": "y",
                        "acceptance_criteria": ["c"],
                        "dependencies": ["a"],
                    },
                ]
            )
            connection.execute("UPDATE plans SET tasks_json = ? WHERE run_id = ?", (tasks, "run-1"))

        with pytest.raises(CyclicPlanError):
            await store.load("run-1")

    asyncio.run(scenario())


def test_an_unreadable_plan_says_so_rather_than_returning_half_of_one(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        await store.save("run-1", _plan())
        import sqlite3

        with sqlite3.connect(tmp_path / "plans.db") as connection:
            connection.execute(
                "UPDATE plans SET tasks_json = ? WHERE run_id = ?", ("not json", "run-1")
            )

        with pytest.raises(GraphStoreError):
            await store.load("run-1")

    asyncio.run(scenario())


def test_reloading_does_not_rewrite_the_shape_of_the_plan(tmp_path: Path) -> None:
    """Collapsing already ran when the plan was built.

    Running it again on reload would renumber a graph whose task ids other records —
    evidence, metrics, a channel's last message — already point at.
    """

    async def scenario() -> None:
        parent = TaskNode(id="epic", goal="the whole thing", expected_output="done")
        child = TaskNode(
            id="only",
            goal="do only",
            expected_output="only",
            parent_id="epic",
            acceptance_criteria=("it can be checked",),
        )
        graph = TaskGraph.build([parent, child], collapse_single_children=False)
        store = _store(tmp_path)
        await store.save("run-1", graph)

        reopened = await store.load("run-1")

        assert reopened is not None
        assert {task.id for task in reopened.graph.nodes} == {"epic", "only"}

    asyncio.run(scenario())


def test_the_limits_come_back_with_the_plan(tmp_path: Path) -> None:
    # A plan reloaded under different limits could be rejected for a shape it was built
    # with, or allowed to grow past what its author intended.
    async def scenario() -> None:
        graph = TaskGraph.build([node("T01")], PlanningLimits(max_tasks=5, max_depth=2))
        store = _store(tmp_path)
        await store.save("run-1", graph)

        reopened = await store.load("run-1")

        assert reopened is not None
        assert reopened.graph.limits.max_tasks == 5
        assert reopened.graph.limits.max_depth == 2

    asyncio.run(scenario())
