from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from athena.cancellation import CancellationSource, CancellationToken
from athena.checkpoints import CheckpointStore
from athena.concurrency import (
    WORKSPACE,
    AccessMode,
    ConcurrencyScheduler,
    ResourceClaim,
    claim_for,
)
from athena.errors import BudgetExceededError, ToolExecutionError, WorkspaceBoundaryError
from athena.events import InMemoryEventBus
from athena.git_tools import GitCommitTool, git_read_tools
from athena.mutation_tools import workspace_mutation_tools
from athena.process_tools import BashTool
from athena.repository_tools import repository_read_tools
from athena.tasks import (
    BackgroundProcess,
    BackgroundProcessSupervisor,
    ProcessState,
    TaskBudget,
    TaskBudgetTracker,
    TaskManager,
    TaskState,
)
from athena.tools import Tool
from athena.types import JSONObject
from athena.workspace import Workspace
from athena.workspaces import (
    IsolationKind,
    SharedWorkspaceStrategy,
    WorkspaceIsolationUnavailable,
    default_strategies,
)

SLEEPER = "import time\ntime.sleep(60)\n"
HEARTBEAT = """
import sys, time
marker = sys.argv[1]
while True:
    with open(marker, 'a') as handle:
        handle.write('x')
        handle.flush()
    time.sleep(0.05)
"""


def _catalog() -> dict[str, Tool]:
    bus = InMemoryEventBus()
    tools: list[Tool] = [
        *repository_read_tools(),
        *workspace_mutation_tools(bus),
        *git_read_tools(),
        GitCommitTool(),
        BashTool(event_bus=bus),
    ]
    return {tool.spec.name: tool for tool in tools}


# ------------------------------------------------------------------ concurrency


def test_independent_reads_run_in_parallel() -> None:
    catalog = _catalog()
    calls: list[tuple[str, Tool, JSONObject]] = [
        ("r1", catalog["read_file"], {"path": "a.py"}),
        ("r2", catalog["read_file"], {"path": "b.py"}),
        ("r3", catalog["grep"], {"query": "x", "glob": "*.md"}),
    ]

    batches = ConcurrencyScheduler().plan_calls(calls)

    assert len(batches) == 1
    assert batches[0].parallel
    assert set(batches[0].call_ids) == {"r1", "r2", "r3"}


def test_conflicting_edits_are_serialised() -> None:
    catalog = _catalog()
    calls: list[tuple[str, Tool, JSONObject]] = [
        ("w1", catalog["edit_file"], {"path": "calc.py", "old_string": "a", "new_string": "b"}),
        ("w2", catalog["edit_file"], {"path": "calc.py", "old_string": "b", "new_string": "c"}),
    ]

    batches = ConcurrencyScheduler().plan_calls(calls)

    assert len(batches) == 2
    assert batches[0].call_ids == ("w1",)
    assert batches[1].call_ids == ("w2",)


def test_a_read_never_overtakes_a_write_to_the_same_file() -> None:
    catalog = _catalog()
    calls: list[tuple[str, Tool, JSONObject]] = [
        ("w1", catalog["edit_file"], {"path": "calc.py", "old_string": "a", "new_string": "b"}),
        ("r1", catalog["read_file"], {"path": "calc.py"}),
    ]

    batches = ConcurrencyScheduler().plan_calls(calls)

    assert [batch.call_ids for batch in batches] == [("w1",), ("r1",)]


def test_writes_to_different_files_still_do_not_run_together() -> None:
    """Two writes are serialised even when their paths differ: safety is not tool identity."""
    catalog = _catalog()
    calls: list[tuple[str, Tool, JSONObject]] = [
        ("w1", catalog["write_file"], {"path": "a.py", "content": "x"}),
        ("w2", catalog["edit_file"], {"path": "b.py", "old_string": "a", "new_string": "b"}),
    ]

    batches = ConcurrencyScheduler().plan_calls(calls)

    assert len(batches) == 2, "different tools and different files is not proof of safety"


def test_git_mutation_takes_the_workspace_exclusively() -> None:
    catalog = _catalog()
    calls: list[tuple[str, Tool, JSONObject]] = [
        ("r1", catalog["read_file"], {"path": "a.py"}),
        ("c1", catalog["git_commit"], {"message": "m", "paths": ["a.py"]}),
        ("r2", catalog["read_file"], {"path": "b.py"}),
    ]

    batches = ConcurrencyScheduler().plan_calls(calls)

    assert [batch.call_ids for batch in batches] == [("r1",), ("c1",), ("r2",)]


def test_a_command_takes_the_workspace_exclusively() -> None:
    catalog = _catalog()
    calls: list[tuple[str, Tool, JSONObject]] = [
        ("r1", catalog["read_file"], {"path": "a.py"}),
        ("b1", catalog["bash"], {"command": "git status"}),
    ]

    batches = ConcurrencyScheduler().plan_calls(calls)

    assert [batch.call_ids for batch in batches] == [("r1",), ("b1",)]


def test_a_tool_that_declines_concurrency_is_not_parallelised() -> None:
    safe = ResourceClaim("a", "read_file", AccessMode.READ, frozenset({"a.py"}), True)
    unsafe = ResourceClaim("b", "read_file", AccessMode.READ, frozenset({"b.py"}), False)

    assert safe.conflicts_with(unsafe)
    assert not safe.conflicts_with(
        ResourceClaim("c", "grep", AccessMode.READ, frozenset({"c.py"}), True)
    )


def test_a_predicate_that_raises_is_treated_as_unsafe() -> None:
    class _Broken:
        spec = _catalog()["read_file"].spec

        def is_read_only(self, arguments: dict[str, object]) -> bool:
            raise RuntimeError("boom")

        def is_destructive(self, arguments: dict[str, object]) -> bool:
            raise RuntimeError("boom")

        def is_concurrency_safe(self, arguments: dict[str, object]) -> bool:
            raise RuntimeError("boom")

    claim = claim_for(_Broken(), "x1", {"path": "a.py"})  # type: ignore[arg-type]

    assert claim.mode is AccessMode.EXCLUSIVE
    assert claim.concurrency_safe is False


def test_max_parallel_caps_a_wave() -> None:
    catalog = _catalog()
    calls = [(f"r{index}", catalog["read_file"], {"path": f"f{index}.py"}) for index in range(5)]

    batches = ConcurrencyScheduler(max_parallel=2).plan_calls(calls)

    assert [len(batch.claims) for batch in batches] == [2, 2, 1]


def test_a_claim_with_no_identifiable_resource_claims_the_workspace() -> None:
    catalog = _catalog()

    claim = claim_for(catalog["git_status"], "g1", {})

    assert WORKSPACE in claim.resources


# ------------------------------------------------------------------ tasks


def test_a_task_runs_and_reports_its_usage() -> None:
    async def scenario() -> None:
        manager = TaskManager()

        async def body(cancellation: CancellationToken, tracker: TaskBudgetTracker) -> str:
            del cancellation
            tracker.consume_iteration()
            tracker.consume_tool_call(3)
            return "done"

        task_id = manager.submit("work", body)
        assert await manager.wait(task_id) == "done"

        record = manager.get(task_id)
        assert record is not None
        assert record.state is TaskState.COMPLETED
        assert record.usage["tool_calls"] == 3

    asyncio.run(scenario())


def test_every_budget_dimension_stops_a_runaway_task() -> None:
    tracker = TaskBudgetTracker(
        TaskBudget(max_iterations=1, max_tool_calls=2, max_tokens=10, max_cost=0.5)
    )

    tracker.consume_iteration()
    with pytest.raises(BudgetExceededError):
        tracker.consume_iteration()

    fresh = TaskBudgetTracker(TaskBudget(max_tool_calls=1))
    fresh.consume_tool_call()
    with pytest.raises(BudgetExceededError):
        fresh.consume_tool_call()

    tokens = TaskBudgetTracker(TaskBudget(max_tokens=5))
    with pytest.raises(BudgetExceededError):
        tokens.consume_tokens(6)

    cost = TaskBudgetTracker(TaskBudget(max_cost=0.01))
    with pytest.raises(BudgetExceededError):
        cost.consume_cost(0.02)


def test_an_unset_budget_dimension_never_fires() -> None:
    tracker = TaskBudgetTracker(TaskBudget())

    for _ in range(1_000):
        tracker.consume_iteration()
        tracker.consume_tool_call()
    tracker.consume_tokens(10_000)
    tracker.consume_cost(99.0)
    tracker.check_wall_clock()

    assert tracker.usage.iterations == 1_000


def test_a_task_that_exceeds_its_budget_fails_with_that_reason() -> None:
    async def scenario() -> None:
        manager = TaskManager()

        async def body(cancellation: CancellationToken, tracker: TaskBudgetTracker) -> None:
            del cancellation
            for _ in range(10):
                tracker.consume_tool_call()

        task_id = manager.submit("greedy", body, budget=TaskBudget(max_tool_calls=3))
        with pytest.raises(BudgetExceededError):
            await manager.wait(task_id)

        record = manager.get(task_id)
        assert record is not None
        assert record.state is TaskState.FAILED
        assert record.error_code == "budget_exceeded"

    asyncio.run(scenario())


def test_a_wall_clock_budget_stops_a_task_that_will_not_end() -> None:
    async def scenario() -> None:
        manager = TaskManager()

        async def body(cancellation: CancellationToken, tracker: TaskBudgetTracker) -> None:
            del cancellation, tracker
            await asyncio.Event().wait()

        task_id = manager.submit("forever", body, budget=TaskBudget(wall_clock_seconds=0.2))
        with pytest.raises(BudgetExceededError):
            await manager.wait(task_id)

        record = manager.get(task_id)
        assert record is not None
        assert record.state is TaskState.FAILED

    asyncio.run(scenario())


def test_cancelling_a_parent_task_cancels_its_children() -> None:
    async def scenario() -> None:
        manager = TaskManager()
        started = asyncio.Event()

        async def parent(cancellation: CancellationToken, tracker: TaskBudgetTracker) -> None:
            del tracker
            started.set()
            await cancellation.wait()

        async def child(cancellation: CancellationToken, tracker: TaskBudgetTracker) -> None:
            del tracker
            await cancellation.wait()

        parent_id = manager.submit("parent", parent)
        await asyncio.wait_for(started.wait(), timeout=5)
        child_id = manager.submit("child", child, parent_id=parent_id)
        grandchild_id = manager.submit("grandchild", child, parent_id=child_id)
        await asyncio.sleep(0)

        await manager.cancel(parent_id)
        await asyncio.wait_for(manager.wait(parent_id), timeout=5)
        await asyncio.wait_for(manager.wait(child_id), timeout=5)
        await asyncio.wait_for(manager.wait(grandchild_id), timeout=5)

        for task_id in (parent_id, child_id, grandchild_id):
            record = manager.get(task_id)
            assert record is not None
            assert record.state is TaskState.COMPLETED, "each body returned on cancellation"

    asyncio.run(scenario())


def test_killing_a_task_is_distinct_from_it_failing() -> None:
    async def scenario() -> None:
        manager = TaskManager()
        started = asyncio.Event()

        async def body(cancellation: CancellationToken, tracker: TaskBudgetTracker) -> None:
            del cancellation, tracker
            started.set()
            await asyncio.Event().wait()

        task_id = manager.submit("stubborn", body)
        await asyncio.wait_for(started.wait(), timeout=5)

        await manager.kill(task_id)

        record = manager.get(task_id)
        assert record is not None
        assert record.state is TaskState.KILLED
        assert record.state.value != TaskState.FAILED.value

    asyncio.run(scenario())


def test_a_restart_leaves_live_tasks_pending_recovery() -> None:
    async def scenario() -> None:
        manager = TaskManager()
        started = asyncio.Event()

        async def body(cancellation: CancellationToken, tracker: TaskBudgetTracker) -> None:
            del cancellation, tracker
            started.set()
            await asyncio.Event().wait()

        async def quick(cancellation: CancellationToken, tracker: TaskBudgetTracker) -> str:
            del cancellation, tracker
            return "ok"

        live_id = manager.submit("live", body)
        done_id = manager.submit("done", quick)
        await manager.wait(done_id)
        await asyncio.wait_for(started.wait(), timeout=5)

        interrupted = manager.mark_interrupted()

        assert interrupted == (live_id,)
        live = manager.get(live_id)
        finished = manager.get(done_id)
        assert live is not None and live.state is TaskState.RECOVERY_PENDING
        assert finished is not None and finished.state is TaskState.COMPLETED

        await manager.shutdown()

    asyncio.run(scenario())


# ------------------------------------------------------------------ background


def test_a_background_process_runs_and_reports_its_exit(tmp_path: Path) -> None:
    script = tmp_path / "quick.py"
    script.write_text("print('done')", encoding="utf-8")

    async def scenario() -> None:
        process = BackgroundProcess((sys.executable, str(script)), tmp_path)
        await process.start()

        assert process.pid is not None
        exit_code = await asyncio.wait_for(process.wait(timeout=30), timeout=35)

        assert exit_code == 0
        assert process.state is ProcessState.EXITED

    asyncio.run(scenario())


def test_killing_a_background_process_leaves_no_orphan(tmp_path: Path) -> None:
    parent_script = tmp_path / "parent.py"
    child_script = tmp_path / "child.py"
    marker = tmp_path / "beat.txt"
    parent_script.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    child_script.write_text(HEARTBEAT, encoding="utf-8")

    async def scenario() -> None:
        process = BackgroundProcess(
            (sys.executable, str(parent_script), str(child_script), str(marker)), tmp_path
        )
        await process.start()
        for _ in range(200):
            if marker.exists() and marker.stat().st_size > 3:
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("the grandchild never started")

        await process.kill()

        assert process.state is ProcessState.KILLED
        await asyncio.sleep(0.5)
        settled = marker.stat().st_size
        await asyncio.sleep(1.0)
        assert marker.stat().st_size == settled, "an orphaned grandchild is still running"

    asyncio.run(scenario())


def test_cancelling_a_task_kills_the_process_it_registered(tmp_path: Path) -> None:
    script = tmp_path / "sleeper.py"
    script.write_text(SLEEPER, encoding="utf-8")

    async def scenario() -> None:
        manager = TaskManager()
        running = asyncio.Event()

        async def body(cancellation: CancellationToken, tracker: TaskBudgetTracker) -> None:
            del tracker
            process = BackgroundProcess((sys.executable, str(script)), tmp_path)
            await process.start(cancellation)
            manager.register_process(task_id, process)
            running.set()
            await cancellation.wait()

        task_id = manager.submit("with-process", body)
        await asyncio.wait_for(running.wait(), timeout=10)

        await manager.kill(task_id)

        process = manager.processes_of(task_id)[0]
        assert process.state in (ProcessState.KILLED, ProcessState.EXITED)

    asyncio.run(scenario())


def test_a_supervisor_reports_a_process_that_died_while_we_were_gone(tmp_path: Path) -> None:
    script = tmp_path / "quick.py"
    script.write_text("print('bye')", encoding="utf-8")

    async def scenario() -> None:
        supervisor = BackgroundProcessSupervisor()
        process = BackgroundProcess((sys.executable, str(script)), tmp_path, handle="h1")
        await process.start()
        supervisor.track(process)
        await process.wait(timeout=30)

        # A cold supervisor sees only what was written down, and no matching live pid.
        cold = BackgroundProcessSupervisor()
        cold.restore({item.handle: item for item in supervisor.recorded()})
        reconciled = cold.reconcile(alive=())

        assert [item.state for item in reconciled] == [ProcessState.DEAD]
        assert reconciled[0].handle == "h1"

    asyncio.run(scenario())


def test_a_supervisor_kills_everything_it_tracks(tmp_path: Path) -> None:
    script = tmp_path / "sleeper.py"
    script.write_text(SLEEPER, encoding="utf-8")

    async def scenario() -> None:
        supervisor = BackgroundProcessSupervisor()
        for index in range(2):
            process = BackgroundProcess((sys.executable, str(script)), tmp_path, handle=f"h{index}")
            await process.start()
            supervisor.track(process)

        await supervisor.kill_all()

        assert all(item.state is ProcessState.KILLED for item in supervisor.snapshots())

    asyncio.run(scenario())


# ------------------------------------------------------------------ checkpoints


def test_a_checkpoint_restores_a_file_without_touching_git(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "calc.py"
    target.write_text("original\n", encoding="utf-8")
    workspace = Workspace.from_path(root)
    store = CheckpointStore(tmp_path / "checkpoints")

    checkpoint = store.create(workspace, ("calc.py",), label="before edit")
    target.write_text("ruined\n", encoding="utf-8")
    restored = store.restore(checkpoint, workspace)

    assert restored == ("calc.py",)
    assert target.read_text(encoding="utf-8") == "original\n"
    assert not (root / ".git").exists(), "a checkpoint is not a commit"


def test_restoring_removes_a_file_that_did_not_exist_before(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    workspace = Workspace.from_path(root)
    store = CheckpointStore(tmp_path / "checkpoints")

    checkpoint = store.create(workspace, ("new.py",))
    (root / "new.py").write_text("created later\n", encoding="utf-8")
    store.restore(checkpoint, workspace)

    assert not (root / "new.py").exists()


def test_a_checkpoint_cannot_reach_outside_the_workspace(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("private", encoding="utf-8")
    store = CheckpointStore(tmp_path / "checkpoints")

    with pytest.raises(WorkspaceBoundaryError):
        store.create(Workspace.from_path(root), ("../secret.txt",))


def test_a_checkpoint_refuses_to_restore_into_a_different_workspace(tmp_path: Path) -> None:
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    (first / "a.txt").write_text("a", encoding="utf-8")
    store = CheckpointStore(tmp_path / "checkpoints")
    checkpoint = store.create(Workspace.from_path(first), ("a.txt",))

    with pytest.raises(ToolExecutionError):
        store.restore(checkpoint, Workspace.from_path(second))


def test_checkpoints_are_listed_and_can_be_discarded(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    workspace = Workspace.from_path(root)
    store = CheckpointStore(tmp_path / "checkpoints")

    checkpoint = store.create(workspace, ("a.txt",), label="one")

    assert [item.checkpoint_id for item in store.list()] == [checkpoint.checkpoint_id]
    assert store.get(checkpoint.checkpoint_id) is not None
    store.discard(checkpoint.checkpoint_id)
    assert store.list() == ()


# ------------------------------------------------------------------ isolation


def test_the_shared_workspace_is_the_only_implemented_strategy(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    workspace = Workspace.from_path(root)

    async def scenario() -> None:
        strategies = default_strategies(workspace)
        shared = strategies[IsolationKind.SHARED]
        assert isinstance(shared, SharedWorkspaceStrategy)

        lease = await shared.acquire("task-1")
        assert lease.workspace.root == workspace.root
        assert lease.shared is True
        await shared.release(lease)

        for kind in (IsolationKind.WORKTREE, IsolationKind.CONTAINER, IsolationKind.REMOTE):
            with pytest.raises(WorkspaceIsolationUnavailable):
                await strategies[kind].acquire("task-1")

    asyncio.run(scenario())


def test_an_unavailable_strategy_refuses_rather_than_falling_back(tmp_path: Path) -> None:
    """Silently sharing when isolation was requested would be worse than refusing."""
    root = tmp_path / "repo"
    root.mkdir()
    strategies = default_strategies(Workspace.from_path(root))

    async def scenario() -> None:
        with pytest.raises(WorkspaceIsolationUnavailable) as failure:
            await strategies[IsolationKind.WORKTREE].acquire("t")
        assert "not implemented" in failure.value.message

    asyncio.run(scenario())


def test_cancellation_source_is_unused_but_available() -> None:
    source = CancellationSource()
    assert not source.token.is_cancelled
