"""Task lifecycle, budgets, and the processes a task leaves behind.

A task is a unit of work the runtime can name, bound, cancel, kill and — after a crash —
find again. The seven states exist because collapsing them loses information a human
needs: a task that was *killed* is not the same as one that *failed*, and a task the
runtime lost track of when the process died is neither, which is what `recovery_pending`
is for. As in H4, the unknown case resolves towards "needs a human", never "finished".
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from athena.cancellation import (
    CancellationScope,
    CancellationSource,
    CancellationToken,
    chained_source,
)
from athena.errors import AthenaRuntimeError, BudgetExceededError
from athena.process_tools import _spawn_process, _terminate_tree
from athena.state import classify_outcome
from athena.types import JSONObject


class TaskState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    #: Stopped from outside by force, rather than by asking it to stop.
    KILLED = "killed"
    #: The process died while this was live. Nobody knows how it ended.
    RECOVERY_PENDING = "recovery_pending"


_TERMINAL = frozenset(
    {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED, TaskState.KILLED}
)
_LIVE = frozenset({TaskState.PENDING, TaskState.RUNNING})

#: How long a task gets to notice it was cancelled before it is killed. Long enough for a
#: body between two awaits, short enough that nobody watches a stuck task for a minute.
DEFAULT_CANCEL_GRACE_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class TaskBudget:
    """Every dimension a long task can run away in.

    Tokens and cost are optional because not every provider reports them; a limit that
    cannot be measured is left unset rather than pretended.
    """

    max_iterations: int | None = None
    max_tool_calls: int | None = None
    max_tokens: int | None = None
    max_cost: float | None = None
    wall_clock_seconds: float | None = None

    def __post_init__(self) -> None:
        for name in ("max_iterations", "max_tool_calls", "max_tokens"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive when set")
        for name in ("max_cost", "wall_clock_seconds"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive when set")


@dataclass(slots=True)
class TaskUsage:
    iterations: int = 0
    tool_calls: int = 0
    tokens: int = 0
    cost: float = 0.0
    started_monotonic: float = field(default_factory=time.monotonic)

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_monotonic

    def to_json(self) -> JSONObject:
        return {
            "iterations": self.iterations,
            "tool_calls": self.tool_calls,
            "tokens": self.tokens,
            "cost": round(self.cost, 6),
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }


class TaskBudgetTracker:
    """Counts, and refuses to keep going past a limit that was actually set."""

    def __init__(self, budget: TaskBudget | None = None) -> None:
        self.budget = budget or TaskBudget()
        self.usage = TaskUsage()

    def consume_iteration(self, count: int = 1) -> None:
        self.usage.iterations += count
        self._check("iterations", self.usage.iterations, self.budget.max_iterations)

    def consume_tool_call(self, count: int = 1) -> None:
        self.usage.tool_calls += count
        self._check("tool calls", self.usage.tool_calls, self.budget.max_tool_calls)

    def consume_tokens(self, count: int) -> None:
        """Only called when the provider actually reported usage."""
        self.usage.tokens += count
        self._check("tokens", self.usage.tokens, self.budget.max_tokens)

    def consume_cost(self, amount: float) -> None:
        self.usage.cost += amount
        self._check("cost", self.usage.cost, self.budget.max_cost)

    def check_wall_clock(self) -> None:
        limit = self.budget.wall_clock_seconds
        if limit is not None and self.usage.elapsed_seconds > limit:
            raise BudgetExceededError(
                f"Task exceeded its wall-clock budget of {limit:g}s",
                details={"elapsed_seconds": round(self.usage.elapsed_seconds, 3)},
            )

    @staticmethod
    def _check(label: str, used: float, limit: float | None) -> None:
        if limit is not None and used > limit:
            raise BudgetExceededError(
                f"Task exceeded its budget of {limit:g} {label}",
                details={"used": used, "limit": limit},
            )


@dataclass(frozen=True, slots=True)
class TaskRecord:
    task_id: str
    name: str
    state: TaskState
    parent_id: str | None = None
    usage: JSONObject = field(default_factory=dict)
    error_code: str | None = None
    message: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def live(self) -> bool:
        return self.state in _LIVE

    @property
    def terminal(self) -> bool:
        return self.state in _TERMINAL


TaskBody = Callable[[CancellationToken, TaskBudgetTracker], Awaitable[object]]


class TaskManager:
    """Runs tasks, bounds them, cancels them, and does not lose them.

    Cancellation is hierarchical: a child's token is chained to its parent's, so cancelling
    a parent cancels its whole subtree. Killing is the harsher path — the asyncio task is
    cancelled outright and any background process the task registered is torn down with it.
    """

    def __init__(self) -> None:
        self._records: dict[str, TaskRecord] = {}
        self._sources: dict[str, CancellationSource] = {}
        self._tasks: dict[str, asyncio.Task[object]] = {}
        self._children: dict[str, list[str]] = {}
        self._trackers: dict[str, TaskBudgetTracker] = {}
        self._processes: dict[str, list[BackgroundProcess]] = {}

    # -- inspection -------------------------------------------------------

    def get(self, task_id: str) -> TaskRecord | None:
        return self._records.get(task_id)

    def list(self, state: TaskState | None = None) -> tuple[TaskRecord, ...]:
        records = sorted(self._records.values(), key=lambda item: item.created_at)
        if state is None:
            return tuple(records)
        return tuple(record for record in records if record.state is state)

    def tracker(self, task_id: str) -> TaskBudgetTracker | None:
        return self._trackers.get(task_id)

    def children_of(self, task_id: str) -> tuple[str, ...]:
        return tuple(self._children.get(task_id, ()))

    # -- lifecycle --------------------------------------------------------

    def submit(
        self,
        name: str,
        body: TaskBody,
        *,
        budget: TaskBudget | None = None,
        parent_id: str | None = None,
        parent_cancellation: CancellationToken | None = None,
    ) -> str:
        task_id = str(uuid4())
        parent_token = parent_cancellation
        if parent_token is None and parent_id is not None:
            parent_source = self._sources.get(parent_id)
            parent_token = parent_source.token if parent_source else None
        if parent_token is None:
            source = CancellationSource(CancellationScope.TASK)
        else:
            # Chained, not copied: the parent stopping is the child stopping, and the
            # child stopping leaves the parent alone. That asymmetry lives in
            # `CancellationSource` so every level of the hierarchy gets the same one.
            source = chained_source(parent_token, CancellationScope.TASK)
        tracker = TaskBudgetTracker(budget)
        self._records[task_id] = TaskRecord(task_id, name, TaskState.PENDING, parent_id)
        self._sources[task_id] = source
        self._trackers[task_id] = tracker
        if parent_id is not None:
            self._children.setdefault(parent_id, []).append(task_id)
        self._tasks[task_id] = asyncio.ensure_future(self._run(task_id, body))
        return task_id

    async def _run(self, task_id: str, body: TaskBody) -> object:
        source = self._sources[task_id]
        tracker = self._trackers[task_id]
        self._transition(task_id, TaskState.RUNNING)
        try:
            if tracker.budget.wall_clock_seconds is not None:
                result = await asyncio.wait_for(
                    body(source.token, tracker), timeout=tracker.budget.wall_clock_seconds
                )
            else:
                result = await body(source.token, tracker)
        except asyncio.CancelledError:
            await self._teardown(task_id)
            state = (
                TaskState.KILLED
                if self._records[task_id].state is TaskState.KILLED
                else TaskState.CANCELLED
            )
            self._transition(task_id, state, message="Task was stopped")
            raise
        except TimeoutError:
            await self._teardown(task_id)
            self._transition(
                task_id,
                TaskState.FAILED,
                error_code="budget_exceeded",
                message="Task exceeded its wall-clock budget",
            )
            raise BudgetExceededError("Task exceeded its wall-clock budget") from None
        except AthenaRuntimeError as exc:
            await self._teardown(task_id)
            # A body that cooperates stops by raising, and what it raises is a
            # cancellation. Filing that under FAILED would punish the task for doing
            # exactly what it was asked to do, and would make a cancelled run look broken.
            if classify_outcome(exc).is_stopped_deliberately:
                self._transition(task_id, TaskState.CANCELLED, message=exc.message)
            else:
                self._transition(
                    task_id, TaskState.FAILED, error_code=exc.code, message=exc.message
                )
            raise
        except Exception as exc:
            await self._teardown(task_id)
            self._transition(
                task_id,
                TaskState.FAILED,
                error_code="fatal_runtime_error",
                message=f"{type(exc).__name__}: {exc}",
            )
            raise
        await self._teardown(task_id)
        self._transition(task_id, TaskState.COMPLETED)
        return result

    async def wait(self, task_id: str) -> object:
        task = self._tasks.get(task_id)
        if task is None:
            raise AthenaRuntimeError(f"Unknown task: {task_id}")
        return await task

    async def cancel(
        self, task_id: str, *, grace_seconds: float = DEFAULT_CANCEL_GRACE_SECONDS
    ) -> None:
        """Ask a task and its whole subtree to stop, then make sure they did.

        Cancellation is cooperative, and cooperation is not guaranteed: a body that never
        checks its token would otherwise sit in `running` for ever, which is the state a
        person waits on and a restart tries to resurrect. So the ask is followed by a
        bounded wait and then by force — the same shape as SIGTERM before SIGKILL, and for
        the same reason.

        A task that stops when asked ends `cancelled`. One that had to be killed ends
        `killed`, because the difference is worth keeping: the second means something in
        that body ignores its token.
        """
        for child in self.children_of(task_id):
            await self.cancel(child, grace_seconds=grace_seconds)
        source = self._sources.get(task_id)
        if source is not None:
            source.cancel()
        task = self._tasks.get(task_id)
        if task is None or task.done():
            return
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(asyncio.shield(task), timeout=grace_seconds)
        if not task.done():
            await self.kill(task_id)

    async def kill(self, task_id: str) -> None:
        """Stop it now, and take its processes with it."""
        for child in self.children_of(task_id):
            await self.kill(child)
        record = self._records.get(task_id)
        if record is not None and record.live:
            self._records[task_id] = replace(
                record, state=TaskState.KILLED, updated_at=datetime.now(UTC)
            )
        source = self._sources.get(task_id)
        if source is not None:
            source.cancel()
        await self._teardown(task_id)
        task = self._tasks.get(task_id)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, BaseException):
                await task

    def register_process(self, task_id: str, process: BackgroundProcess) -> None:
        """Tie a background process to a task, so stopping one stops the other."""
        self._processes.setdefault(task_id, []).append(process)

    def processes_of(self, task_id: str) -> tuple[BackgroundProcess, ...]:
        return tuple(self._processes.get(task_id, ()))

    async def _teardown(self, task_id: str) -> None:
        for process in self._processes.get(task_id, ()):
            await process.kill()

    async def shutdown(self) -> None:
        for task_id in list(self._records):
            if self._records[task_id].live:
                await self.kill(task_id)

    def mark_interrupted(self) -> tuple[str, ...]:
        """After a restart: whatever was live is now of unknown outcome.

        Deliberately mirrors `SessionStore.mark_interrupted`. A task nobody watched to the
        end is not completed and not failed; it is pending recovery.
        """
        interrupted: list[str] = []
        for task_id, record in self._records.items():
            if record.live:
                self._records[task_id] = replace(
                    record,
                    state=TaskState.RECOVERY_PENDING,
                    message="The runtime stopped while this task was live",
                    updated_at=datetime.now(UTC),
                )
                interrupted.append(task_id)
        return tuple(interrupted)

    def _transition(
        self,
        task_id: str,
        state: TaskState,
        *,
        error_code: str | None = None,
        message: str = "",
    ) -> None:
        record = self._records[task_id]
        tracker = self._trackers.get(task_id)
        self._records[task_id] = replace(
            record,
            state=state,
            error_code=error_code or record.error_code,
            message=message or record.message,
            usage=tracker.usage.to_json() if tracker else record.usage,
            updated_at=datetime.now(UTC),
        )


class ProcessState(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    EXITED = "exited"
    KILLED = "killed"
    #: Recorded as live, but no longer present. Someone else ended it, or we crashed.
    DEAD = "dead"


@dataclass(frozen=True, slots=True)
class ProcessSnapshot:
    """What a supervisor knows about a child, including across a restart."""

    handle: str
    argv: tuple[str, ...]
    cwd: str
    pid: int | None
    state: ProcessState
    exit_code: int | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_json(self) -> JSONObject:
        return {
            "handle": self.handle,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "pid": self.pid,
            "state": self.state.value,
            "exit_code": self.exit_code,
            "started_at": self.started_at.isoformat(),
        }


class BackgroundProcess:
    """A long-running child the runtime keeps a handle on.

    The point is that nothing outlives its owner by accident: the process is killed as a
    tree, cancellation is chained to it, and a supervisor can tell a process that exited
    from one that vanished.
    """

    def __init__(self, argv: tuple[str, ...], cwd: Path, *, handle: str | None = None) -> None:
        if not argv:
            raise ValueError("A background process needs an executable")
        self.argv = argv
        self.cwd = cwd
        self.handle = handle or str(uuid4())
        self._process: asyncio.subprocess.Process | None = None
        self._state = ProcessState.STARTING
        self._exit_code: int | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._started_at = datetime.now(UTC)

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def state(self) -> ProcessState:
        if (
            self._state is ProcessState.RUNNING
            and self._process is not None
            and self._process.returncode is not None
        ):
            self._exit_code = self._process.returncode
            self._state = ProcessState.EXITED
        return self._state

    def snapshot(self) -> ProcessSnapshot:
        return ProcessSnapshot(
            self.handle,
            self.argv,
            str(self.cwd),
            self.pid,
            self.state,
            self._exit_code,
            self._started_at,
        )

    async def start(self, cancellation: CancellationToken | None = None) -> None:
        if self._process is not None:
            raise AthenaRuntimeError(f"Background process {self.handle} already started")
        self._process = await _spawn_process(self.argv, self.cwd)
        self._state = ProcessState.RUNNING
        self._started_at = datetime.now(UTC)
        if cancellation is not None:
            self._unsubscribe = cancellation.register(self._terminate)

    def _terminate(self) -> None:
        if self._process is not None:
            _terminate_tree(self._process)
            self._state = ProcessState.KILLED

    async def kill(self) -> None:
        if self._process is None:
            self._state = ProcessState.KILLED
            return
        if self._process.returncode is None:
            self._terminate()
            with contextlib.suppress(Exception):
                await self._process.wait()
        self._exit_code = self._process.returncode
        if self._state is not ProcessState.EXITED:
            self._state = ProcessState.KILLED
        self._release()

    async def wait(self, timeout: float | None = None) -> int | None:
        if self._process is None:
            return None
        try:
            if timeout is None:
                await self._process.wait()
            else:
                await asyncio.wait_for(self._process.wait(), timeout=timeout)
        except TimeoutError:
            return None
        self._exit_code = self._process.returncode
        self._state = ProcessState.EXITED
        self._release()
        return self._exit_code

    def _release(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None


class BackgroundProcessSupervisor:
    """Tracks live children so a restart can tell what happened to them."""

    def __init__(self) -> None:
        self._processes: dict[str, BackgroundProcess] = {}
        self._recorded: dict[str, ProcessSnapshot] = {}

    def track(self, process: BackgroundProcess) -> None:
        self._processes[process.handle] = process
        self._recorded[process.handle] = process.snapshot()

    def get(self, handle: str) -> BackgroundProcess | None:
        return self._processes.get(handle)

    def snapshots(self) -> tuple[ProcessSnapshot, ...]:
        return tuple(process.snapshot() for process in self._processes.values())

    def recorded(self) -> tuple[ProcessSnapshot, ...]:
        """What was written down before the crash, for a supervisor starting cold."""
        return tuple(self._recorded.values())

    async def kill_all(self) -> None:
        for process in list(self._processes.values()):
            await process.kill()

    def reconcile(self, alive: Iterable[int] | None = None) -> tuple[ProcessSnapshot, ...]:
        """Compare what was recorded against what is actually running.

        A process recorded as running whose pid is gone is reported DEAD rather than
        silently forgotten: it may have finished, or it may have been killed, and the
        difference matters to whoever has to clean up after it.
        """
        live_pids = set(alive) if alive is not None else None
        reconciled: list[ProcessSnapshot] = []
        for handle, snapshot in self._recorded.items():
            process = self._processes.get(handle)
            if process is not None and process.state is not ProcessState.RUNNING:
                reconciled.append(process.snapshot())
                continue
            if snapshot.state is not ProcessState.RUNNING:
                reconciled.append(snapshot)
                continue
            if live_pids is not None and snapshot.pid not in live_pids:
                reconciled.append(replace(snapshot, state=ProcessState.DEAD))
                continue
            reconciled.append(snapshot)
        self._recorded = {item.handle: item for item in reconciled}
        return tuple(reconciled)

    def restore(self, snapshots: Mapping[str, ProcessSnapshot]) -> None:
        """Load what a previous process recorded, without adopting the children."""
        self._recorded = dict(snapshots)


__all__ = [
    "BackgroundProcess",
    "BackgroundProcessSupervisor",
    "ProcessSnapshot",
    "ProcessState",
    "TaskBudget",
    "TaskBudgetTracker",
    "TaskManager",
    "TaskRecord",
    "TaskState",
    "TaskUsage",
]
