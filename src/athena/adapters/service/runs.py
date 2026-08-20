"""Live runs, and who is allowed to steer them.

The registry owns three things the HTTP layer must not: which runs are live, who is
subscribed to each, and which single client may send intents. Everything else — the loop,
the permission engine, verification — belongs to Athena proper and is only assembled here.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from athena.adapters.service.approvals import (
    DEFAULT_APPROVAL_TIMEOUT_SECONDS,
    DEFAULT_DELIVERY_TIMEOUT_SECONDS,
    ApprovalRegistry,
    PendingApproval,
    RemotePermissionPrompt,
)
from athena.agent_loop import AgentLoop, AgentLoopConfig, AgentRunResult
from athena.cancellation import CancellationSource
from athena.context import ContextBuilder
from athena.errors import AthenaRuntimeError, ToolValidationError
from athena.events import EventBus, EventName, RuntimeEvent
from athena.git_tools import GitCommitTool, git_read_tools
from athena.models import ModelProvider
from athena.mutation_tools import workspace_mutation_tools
from athena.permissions import PermissionPolicy, PolicyPermissionEngine
from athena.process_tools import BashTool
from athena.registry import ToolRegistry
from athena.repository_tools import repository_read_tools
from athena.security import redact_sensitive
from athena.session_store import SessionRecord, SessionStore
from athena.state import AgentStatus
from athena.stores import ToolResultStore
from athena.tool_executor import ToolExecutor
from athena.tools import Tool
from athena.verification import CommandVerificationPolicy, VerificationPlanner
from athena.workspace import Workspace

#: How many events a slow client may fall behind before it is dropped rather than allowed
#: to hold the runtime's memory hostage.
_SUBSCRIBER_QUEUE_LIMIT = 512

#: How long `start` waits for the loop to announce itself before giving up.
_START_TIMEOUT_SECONDS = 10.0


class CapabilityMode(StrEnum):
    OFF = "off"
    ASK = "ask"
    ALLOW = "allow"


@dataclass(frozen=True, slots=True)
class RunOptions:
    """What a client may choose about a run. Defaults follow ADR-017 §14: it asks."""

    writes: CapabilityMode = CapabilityMode.ASK
    execution: CapabilityMode = CapabilityMode.ASK
    max_iterations: int = 12
    max_repair_cycles: int = 2
    session_timeout_seconds: float = 900.0

    @classmethod
    def from_json(cls, payload: Mapping[str, object]) -> RunOptions:
        def mode(key: str, default: CapabilityMode) -> CapabilityMode:
            raw = payload.get(key)
            if raw is None:
                return default
            try:
                return CapabilityMode(str(raw))
            except ValueError as exc:
                raise ToolValidationError(f"{key} must be one of off, ask, allow") from exc

        def positive_int(key: str, default: int) -> int:
            raw = payload.get(key, default)
            if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
                raise ToolValidationError(f"{key} must be a positive integer")
            return raw

        timeout = payload.get("session_timeout_seconds", 900.0)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ToolValidationError("session_timeout_seconds must be a positive number")

        return cls(
            writes=mode("writes", CapabilityMode.ASK),
            execution=mode("exec", CapabilityMode.ASK),
            max_iterations=positive_int("max_iterations", 12),
            max_repair_cycles=positive_int("max_repair_cycles", 2)
            if payload.get("max_repair_cycles") is not None
            else 2,
            session_timeout_seconds=float(timeout),
        )


@dataclass(slots=True)
class Subscriber:
    subscriber_id: str
    run_id: str
    queue: asyncio.Queue[RuntimeEvent | None]
    #: Exactly one subscriber per run may send intents.
    controls: bool = False
    dropped: int = 0


@dataclass(slots=True)
class LiveRun:
    run_id: str
    workspace: Workspace
    options: RunOptions
    cancellation: CancellationSource
    task: asyncio.Task[AgentRunResult] | None = None
    prompt: RemotePermissionPrompt | None = None
    subscribers: dict[str, Subscriber] = field(default_factory=dict)
    controller_id: str | None = None

    @property
    def finished(self) -> bool:
        return self.task is not None and self.task.done()


class RunRegistry:
    """Starts, observes, steers and recovers runs."""

    def __init__(
        self,
        provider: ModelProvider,
        event_bus: EventBus,
        session_store: SessionStore,
        result_store: ToolResultStore,
        *,
        approvals: ApprovalRegistry | None = None,
        delivery_timeout_seconds: float | None = None,
        approval_timeout_seconds: float | None = None,
    ) -> None:
        self.provider = provider
        self.event_bus = event_bus
        self.session_store = session_store
        self.result_store = result_store
        self.approvals = approvals or ApprovalRegistry()
        self.delivery_timeout_seconds = delivery_timeout_seconds
        self.approval_timeout_seconds = approval_timeout_seconds
        self._runs: dict[str, LiveRun] = {}
        event_bus.subscribe(self._fan_out)

    # -- fan-out ----------------------------------------------------------

    def _fan_out(self, event: RuntimeEvent) -> None:
        run = self._runs.get(event.session_id)
        if run is None:
            return
        for subscriber in tuple(run.subscribers.values()):
            try:
                subscriber.queue.put_nowait(event)
            except asyncio.QueueFull:
                # A client too slow to keep up loses events, not the runtime its memory.
                # The snapshot it fetches on reconnect is what makes this recoverable.
                subscriber.dropped += 1

    def subscribe(self, run_id: str, *, control: bool = False) -> Subscriber:
        run = self._require(run_id)
        subscriber = Subscriber(
            subscriber_id=str(uuid4()),
            run_id=run_id,
            queue=asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_LIMIT),
        )
        if control and run.controller_id is None:
            run.controller_id = subscriber.subscriber_id
            subscriber.controls = True
        run.subscribers[subscriber.subscriber_id] = subscriber
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        run = self._runs.get(subscriber.run_id)
        if run is None:
            return
        run.subscribers.pop(subscriber.subscriber_id, None)
        if run.controller_id == subscriber.subscriber_id:
            run.controller_id = None
        with contextlib.suppress(asyncio.QueueFull):
            subscriber.queue.put_nowait(None)

    def has_client(self, run_id: str) -> bool:
        run = self._runs.get(run_id)
        return bool(run and run.subscribers)

    def controls(self, run_id: str, subscriber_id: str | None) -> bool:
        """One writer per run: two UIs approving the same request is a race worth removing."""
        run = self._runs.get(run_id)
        if run is None:
            return False
        if run.controller_id is None:
            return True
        return run.controller_id == subscriber_id

    # -- lifecycle --------------------------------------------------------

    def tools_for(self, options: RunOptions, event_bus: EventBus) -> tuple[Tool, ...]:
        """Which tools a run gets. `off` means the tool does not exist for that run."""
        tools: list[Tool] = [*repository_read_tools(), *git_read_tools()]
        if options.writes is not CapabilityMode.OFF:
            tools.extend(workspace_mutation_tools(event_bus))
            tools.append(GitCommitTool())
        if options.execution is not CapabilityMode.OFF:
            tools.append(BashTool(event_bus=event_bus))
        return tuple(tools)

    def _build(self, run_id: str, workspace: Workspace, options: RunOptions) -> AgentLoop:
        registry = ToolRegistry(self.tools_for(options, self.event_bus))
        prompt = RemotePermissionPrompt(
            self.approvals,
            run_id,
            self._publish_approval,
            lambda: self.has_client(run_id),
            delivery_timeout_seconds=(
                self.delivery_timeout_seconds
                if self.delivery_timeout_seconds is not None
                else DEFAULT_DELIVERY_TIMEOUT_SECONDS
            ),
            approval_timeout_seconds=(
                self.approval_timeout_seconds
                if self.approval_timeout_seconds is not None
                else DEFAULT_APPROVAL_TIMEOUT_SECONDS
            ),
        )
        self._runs[run_id].prompt = prompt
        executor = ToolExecutor(
            registry,
            PolicyPermissionEngine(
                PermissionPolicy(
                    allow_workspace_writes=options.writes is CapabilityMode.ALLOW,
                    allow_local_execution=options.execution is CapabilityMode.ALLOW,
                )
            ),
            self.result_store,
            self.event_bus,
            prompt=prompt,
        )
        return AgentLoop(
            self.provider,
            registry,
            executor,
            ContextBuilder(workspace),
            self.event_bus,
            verification=CommandVerificationPolicy(
                VerificationPlanner(workspace), event_bus=self.event_bus
            ),
            session_store=self.session_store,
            config=AgentLoopConfig(
                max_iterations=options.max_iterations,
                session_timeout_seconds=options.session_timeout_seconds,
                max_repair_cycles=options.max_repair_cycles,
            ),
        )

    def _publish_approval(self, pending: PendingApproval) -> None:
        """Approvals reach the client the same way everything else does: as an event.

        Redaction is applied here explicitly. This event goes straight to the
        subscribers rather than through `EventBus.publish`, so it would otherwise
        be the one payload in the system that never passed a redactor — and it is
        the payload most likely to carry a tool's arguments.
        """
        run = self._runs.get(pending.run_id)
        if run is None:
            return
        payload = redact_sensitive({**pending.to_json(), "awaiting_decision": True})
        event = RuntimeEvent(
            EventName.PERMISSION_REQUESTED,
            pending.run_id,
            payload if isinstance(payload, dict) else {},
            pending.request_id,
        )
        self._fan_out(event)

    async def start(
        self, objective: str, workspace: Workspace, options: RunOptions | None = None
    ) -> str:
        """Begin a run and return only once it is genuinely addressable.

        Returning the id before the loop has persisted anything would hand a client an
        identifier that answers 404 for its first few milliseconds. The signal to wait for
        is `session.persisted`, not `agent.started`: the loop announces itself *before* it
        writes, so the earlier event would still race the store.
        """
        settings = options or RunOptions()
        run_id = str(uuid4())
        source = CancellationSource()
        self._runs[run_id] = LiveRun(run_id, workspace, settings, source)
        loop = self._build(run_id, workspace, settings)

        started = asyncio.Event()

        def note(event: RuntimeEvent) -> None:
            if event.session_id == run_id:
                started.set()

        unsubscribe = self.event_bus.subscribe(note, (EventName.SESSION_PERSISTED,))
        self._runs[run_id].task = asyncio.ensure_future(
            loop.run(objective, workspace, source.token, session_id=run_id)
        )
        try:
            await asyncio.wait_for(started.wait(), timeout=_START_TIMEOUT_SECONDS)
        except TimeoutError:
            # The loop never got going; do not hand back an id nothing will answer for.
            source.cancel()
            self._runs.pop(run_id, None)
            raise AthenaRuntimeError("The run did not start in time") from None
        finally:
            unsubscribe()
        return run_id

    async def resume(self, run_id: str, workspace: Workspace) -> str:
        record = await self.session_store.load(run_id)
        if record is None:
            raise ToolValidationError(f"Unknown run: {run_id}")
        if not record.resumable:
            raise ToolValidationError(
                f"Run {run_id} is {record.status.value}, not recovery_pending"
            )
        options = self._runs[run_id].options if run_id in self._runs else RunOptions()
        source = CancellationSource()
        self._runs[run_id] = LiveRun(run_id, workspace, options, source)
        loop = self._build(run_id, workspace, options)
        self._runs[run_id].task = asyncio.ensure_future(
            loop.resume(run_id, workspace, source.token)
        )
        return run_id

    async def cancel(self, run_id: str) -> None:
        run = self._require(run_id)
        self.approvals.cancel_run(run_id)
        run.cancellation.cancel()

    async def wait(self, run_id: str) -> AgentRunResult:
        run = self._require(run_id)
        if run.task is None:
            raise ToolValidationError(f"Run {run_id} has not started")
        return await run.task

    async def snapshot(self, run_id: str) -> SessionRecord | None:
        return await self.session_store.load(run_id)

    async def list(self, status: AgentStatus | None = None) -> tuple[SessionRecord, ...]:
        return await self.session_store.list_sessions(status)

    async def mark_interrupted(self) -> tuple[str, ...]:
        return await self.session_store.mark_interrupted()

    async def shutdown(self) -> None:
        for run_id in tuple(self._runs):
            with contextlib.suppress(AthenaRuntimeError):
                await self.cancel(run_id)
        for run in tuple(self._runs.values()):
            if run.task is not None and not run.task.done():
                run.task.cancel()
                with contextlib.suppress(BaseException):
                    await run.task

    def live_ids(self) -> tuple[str, ...]:
        return tuple(self._runs)

    def _require(self, run_id: str) -> LiveRun:
        run = self._runs.get(run_id)
        if run is None:
            raise ToolValidationError(f"Unknown or finished run: {run_id}")
        return run


def build_workspace(
    root: Path | str, authorized: Callable[[Path], bool] | None = None
) -> Workspace:
    """Resolve a workspace, honouring an external authorisation check when supplied."""
    workspace = Workspace.from_path(root)
    if authorized is not None and not authorized(workspace.root):
        raise ToolValidationError(f"Workspace is not authorised: {workspace.root}")
    return workspace


__all__ = [
    "CapabilityMode",
    "LiveRun",
    "RunOptions",
    "RunRegistry",
    "Subscriber",
    "build_workspace",
]
