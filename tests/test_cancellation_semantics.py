"""Cancellation as an outcome, and as a hierarchy.

Two claims are under test. First, that being stopped is not a kind of failure — a runtime
that reports it as one sends people looking for a bug that is not there. Second, that a
stop travels down and never up: cancelling one task must not take down the run that owns
it, and cancelling the run must take down everything under it.

The second claim is the one that has to hold before there is a hierarchy to get it wrong
in, which is why this comes before the executor.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from athena.cancellation import (
    CancellationReason,
    CancellationScope,
    CancellationSource,
    CancellationToken,
    chained_source,
)
from athena.errors import (
    CancellationError,
    ProcessCancelledError,
    ProcessTimeoutError,
    ToolExecutionError,
)
from athena.process_tools import BashTool
from athena.recovery import RecoveryAction, RecoveryPolicy
from athena.state import ExecutionOutcome, classify_outcome
from athena.tasks import TaskManager, TaskState
from athena.tools import ToolContext
from athena.workspace import Workspace

# --------------------------------------------------------------- outcome, not error kind


def test_being_stopped_is_not_a_failure() -> None:
    """The distinction the whole phase exists for.

    Nothing went wrong when someone asked to stop. Filing it under `FAILED` is a lie that
    costs somebody an afternoon looking for the fault.
    """
    assert classify_outcome(CancellationError("stopped")) is ExecutionOutcome.CANCELLED
    assert classify_outcome(ValueError("broken")) is ExecutionOutcome.FAILED
    assert classify_outcome(None) is ExecutionOutcome.COMPLETED


def test_a_deadline_is_not_the_same_as_being_abandoned() -> None:
    # A limit that was reached is a fact about the limit. Telling a person their work was
    # cancelled when their own timeout fired points them at the wrong thing.
    assert classify_outcome(ProcessTimeoutError("late")) is ExecutionOutcome.TIMED_OUT
    assert classify_outcome(CancellationError("stop")) is ExecutionOutcome.CANCELLED


def test_a_cancellation_caused_by_a_deadline_is_classified_as_a_timeout() -> None:
    """The token knows why; the exception on its own does not.

    A process killed because its clock ran out raises the same cancellation as one killed
    because a person asked, and the difference only survives if it is carried.
    """
    source = CancellationSource(CancellationScope.TASK)
    source.cancel(CancellationReason.TIMED_OUT)

    with pytest.raises(CancellationError) as caught:
        source.token.raise_if_cancelled()

    assert classify_outcome(caught.value) is ExecutionOutcome.TIMED_OUT


def test_the_outcome_vocabulary_answers_the_two_questions_callers_ask() -> None:
    assert ExecutionOutcome.COMPLETED.is_success
    assert not ExecutionOutcome.FAILED.is_success
    assert ExecutionOutcome.CANCELLED.is_stopped_deliberately
    assert ExecutionOutcome.TIMED_OUT.is_stopped_deliberately
    assert not ExecutionOutcome.FAILED.is_stopped_deliberately


def test_a_process_cancellation_is_stopped_not_broken() -> None:
    # `ProcessCancelledError` is a `ToolExecutionError` by inheritance, which is exactly
    # the ambiguity that used to need special-casing at three separate call sites.
    error = ProcessCancelledError("child terminated")

    assert isinstance(error, ToolExecutionError)
    assert classify_outcome(error) is ExecutionOutcome.CANCELLED


def test_the_recovery_policy_and_the_loop_agree_by_construction() -> None:
    """One classifier, so the two cannot drift apart.

    Before this they each carried their own `isinstance` pair, and a fourth level of the
    hierarchy would have added a third copy.
    """
    policy = RecoveryPolicy()

    assert policy.decide(CancellationError("stop")).action is RecoveryAction.CANCELLED
    assert policy.decide(ProcessCancelledError("stop")).action is RecoveryAction.CANCELLED
    directive = policy.decide(ProcessTimeoutError("late"))
    assert directive.action is RecoveryAction.CANCELLED
    assert "time" in directive.reason.lower(), "a timeout says so rather than saying cancelled"


# ------------------------------------------------------------------------ the hierarchy


def test_a_stop_travels_down_and_never_up() -> None:
    run = CancellationSource(CancellationScope.RUN)
    subgraph = run.child(CancellationScope.SUBGRAPH)
    task = subgraph.child(CancellationScope.TASK)

    task.cancel()

    assert task.token.is_cancelled
    assert not subgraph.token.is_cancelled, "a task does not take down its subgraph"
    assert not run.token.is_cancelled, "and certainly not the run"


def test_cancelling_a_subgraph_leaves_its_siblings_alone() -> None:
    """The shape from the report:

            A
           / \\
          B   C

    Stopping B must not touch C, because C's work is still wanted.
    """
    run = CancellationSource(CancellationScope.RUN)
    left = run.child(CancellationScope.SUBGRAPH)
    right = run.child(CancellationScope.SUBGRAPH)
    left_task = left.child(CancellationScope.TASK)
    right_task = right.child(CancellationScope.TASK)

    left.cancel()

    assert left_task.token.is_cancelled, "everything under B stops"
    assert not right.token.is_cancelled
    assert not right_task.token.is_cancelled, "and everything under C carries on"


def test_cancelling_the_run_cascades_to_every_level() -> None:
    run = CancellationSource(CancellationScope.RUN)
    subgraph = run.child(CancellationScope.SUBGRAPH)
    tasks = [subgraph.child(CancellationScope.TASK) for _ in range(3)]

    run.cancel()

    assert subgraph.token.is_cancelled
    assert all(task.token.is_cancelled for task in tasks)
    assert all(task.token.reason is CancellationReason.PARENT_CANCELLED for task in tasks), (
        "a child says it was stopped from above, not that it asked to stop"
    )


def test_a_child_created_after_the_parent_stopped_is_born_cancelled() -> None:
    # Otherwise a race between cancelling and submitting produces a task that outlives the
    # run that owned it, which is precisely the orphan this phase is meant to rule out.
    run = CancellationSource(CancellationScope.RUN)
    run.cancel()

    late = run.child(CancellationScope.TASK)

    assert late.token.is_cancelled


def test_the_scope_is_carried_so_a_report_can_say_what_stopped() -> None:
    run = CancellationSource(CancellationScope.RUN)
    task = run.child(CancellationScope.TASK)

    assert task.token.scope is CancellationScope.TASK
    assert run.token.scope is CancellationScope.RUN


def test_chaining_works_from_a_token_alone() -> None:
    # A level that owns work is handed the right to observe its parent's cancellation, not
    # the right to cause it. The primitive takes a token for that reason.
    run = CancellationSource(CancellationScope.RUN)

    task = chained_source(run.token, CancellationScope.TASK)
    run.cancel()

    assert task.token.is_cancelled


def test_cancelling_is_idempotent_and_keeps_its_first_reason() -> None:
    source = CancellationSource(CancellationScope.TASK)
    source.cancel(CancellationReason.TIMED_OUT)
    source.cancel(CancellationReason.REQUESTED)

    assert source.token.reason is CancellationReason.TIMED_OUT


# ------------------------------------------------------------------- through the runtime


def test_cancelling_a_task_does_not_cancel_its_siblings(tmp_path: Path) -> None:
    del tmp_path

    async def scenario() -> None:
        manager = TaskManager()
        started = asyncio.Event()

        async def forever(token: CancellationToken, tracker: object) -> str:
            del tracker
            started.set()
            while True:
                token.raise_if_cancelled()
                await asyncio.sleep(0.01)

        async def quick(token: object, tracker: object) -> str:
            del token, tracker
            return "done"

        slow_id = manager.submit("slow", forever)
        fast_id = manager.submit("fast", quick)
        await started.wait()

        await manager.cancel(slow_id)
        await asyncio.sleep(0.05)

        assert manager.get(slow_id) is not None
        assert manager.get(slow_id).state is TaskState.CANCELLED  # type: ignore[union-attr]
        assert manager.get(fast_id).state is TaskState.COMPLETED  # type: ignore[union-attr]
        await manager.shutdown()

    asyncio.run(scenario())


def test_cancelling_a_parent_task_cancels_its_children(tmp_path: Path) -> None:
    del tmp_path

    async def scenario() -> None:
        manager = TaskManager()
        child_running = asyncio.Event()

        async def forever(token: CancellationToken, tracker: object) -> str:
            del tracker
            child_running.set()
            while True:
                token.raise_if_cancelled()
                await asyncio.sleep(0.01)

        parent_id = manager.submit("parent", forever)
        child_id = manager.submit("child", forever, parent_id=parent_id)
        await child_running.wait()

        await manager.cancel(parent_id)
        await asyncio.sleep(0.05)

        assert manager.get(child_id).state is TaskState.CANCELLED  # type: ignore[union-attr]
        assert not manager.get(child_id).live  # type: ignore[union-attr]
        await manager.shutdown()

    asyncio.run(scenario())


def test_no_task_is_left_claiming_to_run_after_a_cancellation() -> None:
    """The state a crash-recovery reader would misread.

    A task still marked `running` after everything stopped is one a restart will resurrect
    or a person will wait on. Neither is what happened.
    """

    async def scenario() -> None:
        manager = TaskManager()
        running = asyncio.Event()

        async def forever(token: object, tracker: object) -> str:
            del tracker
            running.set()
            await asyncio.sleep(3600)
            return "never"

        for _ in range(3):
            manager.submit("worker", forever)
        await running.wait()
        await asyncio.sleep(0.01)

        await manager.shutdown()

        assert all(record.terminal for record in manager.list())
        assert manager.list(TaskState.RUNNING) == ()

    asyncio.run(scenario())


def test_cancelling_a_bash_tool_reports_being_stopped_not_broken(tmp_path: Path) -> None:
    """The bottom of the hierarchy.

    `test_process_tools.py` already proves the child process dies. What matters here is
    that what comes back out classifies as a stop, so a task wrapping this tool does not
    record a failure against work that was simply told to end.
    """
    script = tmp_path / "sleeper.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")

    async def scenario() -> None:
        tool = BashTool()
        source = CancellationSource(CancellationScope.TASK)
        context = ToolContext("session", Workspace.from_path(tmp_path), "call-1")
        command = f'"{sys.executable}" "{script}"'
        running = asyncio.ensure_future(
            tool.execute(context, {"command": command, "timeout_seconds": 60}, source.token)
        )
        await asyncio.sleep(0.4)
        source.cancel()

        with pytest.raises(ProcessCancelledError) as caught:
            await asyncio.wait_for(running, timeout=20)

        assert classify_outcome(caught.value).is_stopped_deliberately
        assert classify_outcome(caught.value) is not ExecutionOutcome.FAILED

    asyncio.run(scenario())


def test_a_task_that_ignores_its_token_is_killed_rather_than_left_running() -> None:
    """Cooperation is asked for, not assumed.

    A body that never checks its token would otherwise sit in `running` for ever — the
    state a person waits on and a restart tries to resurrect. Asking is followed by a
    bounded wait and then by force, and the two endings are recorded differently because
    `killed` means something in that body ignores its token.
    """

    async def scenario() -> None:
        manager = TaskManager()
        started = asyncio.Event()

        async def stubborn(token: CancellationToken, tracker: object) -> str:
            del token, tracker
            started.set()
            await asyncio.sleep(3600)
            return "never"

        task_id = manager.submit("stubborn", stubborn)
        await started.wait()

        await manager.cancel(task_id, grace_seconds=0.05)

        record = manager.get(task_id)
        assert record is not None
        assert record.state is TaskState.KILLED
        assert record.terminal, "nothing is left claiming to run"
        await manager.shutdown()

    asyncio.run(scenario())


def test_a_task_that_cooperates_ends_cancelled_not_killed() -> None:
    async def scenario() -> None:
        manager = TaskManager()
        started = asyncio.Event()

        async def polite(token: CancellationToken, tracker: object) -> str:
            del tracker
            started.set()
            while True:
                token.raise_if_cancelled()
                await asyncio.sleep(0.01)

        task_id = manager.submit("polite", polite)
        await started.wait()

        await manager.cancel(task_id, grace_seconds=2.0)

        record = manager.get(task_id)
        assert record is not None
        assert record.state is TaskState.CANCELLED, "asked, and it obliged"
        await manager.shutdown()

    asyncio.run(scenario())
