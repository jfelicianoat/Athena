"""Binds a `ChannelAdapter` to the runtime, and nothing more.

The gateway is the only place where the two sides meet. It reads messages from an adapter
it knows nothing about, turns them into commands the runtime already understands, calls the
run registry, and routes the runtime's events back out through the same adapter. It owns no
agent logic: it starts nothing the registry would not start, decides nothing the permission
engine would not decide, and holds no state about a run beyond who asked for it.

Access is an explicit grant, not a default. An identity with no grant cannot start anything
— which matters more here than over HTTP, because a chat account is discoverable by anyone
who finds the bot, and the workspace is a security boundary rather than a preference.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from athena.adapters.service.runs import RunOptions, RunRegistry, build_workspace
from athena.channels import (
    HELP_TEXT,
    NEW_INTERACTION_TEXT,
    REPORTED_EVENTS,
    ChannelAdapter,
    ChannelCommand,
    ChannelIdentity,
    ChannelResponse,
    CommandName,
    ResponseKind,
    parse_command,
    render_event,
)
from athena.errors import AthenaRuntimeError
from athena.events import EventBus, RuntimeEvent

#: Facts go in the message itself: `JsonFormatter` renders `message` and redacts it, and
#: drops `extra`, so anything put there would silently never be logged.
_logger = logging.getLogger(__name__)

#: How many of an identity's runs `/runs` shows. A chat reply is not a report.
_RUNS_LISTED = 5


@dataclass(frozen=True, slots=True)
class ChannelGrant:
    """What one identity is allowed to do, decided before it ever speaks.

    The capability modes are the same ones the HTTP client passes per run, but here they
    are fixed per identity: a channel cannot negotiate its own permissions upward, and
    anything left at ASK will be refused, because nobody on a channel can answer.
    """

    workspace_root: Path
    options: RunOptions = field(default_factory=RunOptions)


class ChannelAccessPolicy:
    """Who may command Athena from a channel, and over which workspace.

    A plain allow-list keyed by `ChannelIdentity.key`. Deterministic and dull on purpose:
    this is a security decision, so it is made by a table someone wrote down, never by
    inference from the message.
    """

    def __init__(
        self,
        grants: Mapping[str, ChannelGrant] | None = None,
        *,
        authorized: Callable[[Path], bool] | None = None,
    ) -> None:
        self._grants = dict(grants or {})
        self._authorized = authorized

    def grant(self, identity_key: str, grant: ChannelGrant) -> None:
        self._grants[identity_key] = grant

    def revoke(self, identity_key: str) -> None:
        self._grants.pop(identity_key, None)

    def for_identity(self, identity: ChannelIdentity) -> ChannelGrant | None:
        return self._grants.get(identity.key)

    def workspace_check(self) -> Callable[[Path], bool] | None:
        """The host's own authorisation check, applied on top of the grant."""
        return self._authorized


class ChannelGateway:
    """Runs one adapter: messages in, commands to the registry, events back out."""

    def __init__(
        self,
        adapter: ChannelAdapter,
        registry: RunRegistry,
        policy: ChannelAccessPolicy,
        event_bus: EventBus,
        *,
        bare_text_starts_run: bool = False,
    ) -> None:
        self.adapter = adapter
        self.registry = registry
        self.policy = policy
        self.event_bus = event_bus
        #: Whether plain text is an objective. Off unless the channel removed the
        #: ambiguity itself — see `parse_command`.
        self.bare_text_starts_run = bare_text_starts_run
        #: Which identity owns which run, so an event reaches the person who asked for it
        #: and nobody else. A run this gateway did not start has no owner and is ignored.
        self._owners: dict[str, ChannelIdentity] = {}
        self._latest: dict[str, str] = {}
        self._unsubscribe: Callable[[], None] | None = None

    async def start(self) -> None:
        await self.adapter.start()
        self._unsubscribe = self.event_bus.subscribe(self._on_event, REPORTED_EVENTS)

    async def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        await self.adapter.stop()

    async def run_forever(self) -> None:
        """Serve the channel until it closes.

        Sequential by design. `RunRegistry.start` returns as soon as the run is
        addressable, so a long run never blocks the next message; what this does prevent is
        two commands from the same conversation interleaving, which no channel expects.
        """
        while True:
            message = await self.adapter.receive()
            if message is None:
                return
            await self.handle(
                parse_command(message, bare_text_starts_run=self.bare_text_starts_run)
            )

    async def handle(self, command: ChannelCommand) -> None:
        """Execute one command and answer it.

        Only Athena's own error taxonomy is turned into a reply. Anything unclassified ends
        the gateway rather than being swallowed: an error nobody characterised is not a
        thing to keep serving commands through, and `RecoveryPolicy` takes the same view
        inside the loop.
        """
        try:
            response = await self._execute(command)
        except AthenaRuntimeError as error:
            _logger.warning(
                "channel.command_failed command=%s code=%s", command.name.value, error.code
            )
            response = self._say(command.identity, str(error), ResponseKind.ERROR)
        await self._deliver(response)

    async def _execute(self, command: ChannelCommand) -> ChannelResponse:
        identity = command.identity
        if command.name is CommandName.HELP:
            return self._say(identity, HELP_TEXT)
        if command.name is CommandName.NEW_INTERACTION:
            return self._say(identity, NEW_INTERACTION_TEXT)
        if command.name is CommandName.UNKNOWN:
            reason = command.reason or "That is not something Athena can do."
            return self._say(identity, reason, ResponseKind.ERROR)

        grant = self.policy.for_identity(identity)
        if grant is None:
            # Said the same way for every unknown identity: a message that distinguished
            # "no grant" from "wrong workspace" would be a way to probe the table.
            _logger.warning("channel.identity_refused identity=%s", identity.key)
            return self._say(
                identity,
                "This account is not authorised to use Athena.",
                ResponseKind.ERROR,
            )

        match command.name:
            case CommandName.START_RUN:
                return await self._start_run(identity, grant, command.objective or "")
            case CommandName.CANCEL_RUN:
                return await self._cancel_run(identity, command.run_id)
            case CommandName.RUN_STATUS:
                return await self._run_status(identity, command.run_id)
            case CommandName.LIST_RUNS:
                return await self._list_runs(identity)
            case _:  # pragma: no cover - CommandName is closed and handled above
                return self._say(identity, HELP_TEXT)

    async def _start_run(
        self, identity: ChannelIdentity, grant: ChannelGrant, objective: str
    ) -> ChannelResponse:
        active = self._active_run(identity)
        if active is not None:
            # One at a time. Without this, a chat account is an unbounded way to spend the
            # host's CPU, and the person could not tell which run any event belonged to.
            return self._say(
                identity,
                f"A run is already going ({active}). Cancel it before starting another.",
                ResponseKind.ERROR,
                run_id=active,
            )
        workspace = build_workspace(grant.workspace_root, self.policy.workspace_check())
        run_id = await self.registry.start(objective, workspace, grant.options)
        self._owners[run_id] = identity
        self._latest[identity.key] = run_id
        _logger.info("channel.run_started identity=%s run=%s", identity.key, run_id)
        return self._say(
            identity,
            f"Started. I will tell you how it goes.\nRun {run_id}",
            # Not PROGRESS: this answers a command, and a channel is free to drop
            # progress. Being told nothing after asking for a run is the worst outcome.
            ResponseKind.NOTICE,
            run_id=run_id,
        )

    async def _cancel_run(self, identity: ChannelIdentity, run_id: str | None) -> ChannelResponse:
        target = self._resolve(identity, run_id)
        if target is None:
            return self._say(identity, "You have no run to cancel.", ResponseKind.ERROR)
        await self.registry.cancel(target)
        return self._say(identity, "Cancelling.", ResponseKind.NOTICE, run_id=target)

    async def _run_status(self, identity: ChannelIdentity, run_id: str | None) -> ChannelResponse:
        target = self._resolve(identity, run_id)
        if target is None:
            return self._say(identity, "You have no runs yet.", ResponseKind.NOTICE)
        record = await self.registry.snapshot(target)
        if record is None:
            return self._say(identity, "Athena has no record of that run.", ResponseKind.ERROR)
        return self._say(
            identity,
            f"{record.objective}\nState: {record.status.value}",
            ResponseKind.RESULT,
            run_id=target,
        )

    async def _list_runs(self, identity: ChannelIdentity) -> ChannelResponse:
        """The asker's own runs, newest first.

        Filtered by ownership rather than listed wholesale: the registry knows about every
        run on the host, and a channel is not a window onto other people's work.
        """
        mine = [run_id for run_id, owner in self._owners.items() if owner.key == identity.key]
        if not mine:
            return self._say(identity, "You have no runs yet.")
        lines: list[str] = []
        for run_id in reversed(mine[-_RUNS_LISTED:]):
            record = await self.registry.snapshot(run_id)
            state = record.status.value if record is not None else "unknown"
            objective = record.objective if record is not None else "(no record)"
            lines.append(f"{state} — {objective}\n{run_id}")
        return self._say(identity, "\n\n".join(lines), ResponseKind.RESULT)

    def _resolve(self, identity: ChannelIdentity, run_id: str | None) -> str | None:
        """Pick the run a command refers to, refusing anything the asker does not own.

        An explicit id from someone else's conversation resolves to nothing rather than to
        an error naming it: whether a given run id exists is not a channel's business.
        """
        if run_id is None:
            return self._latest.get(identity.key)
        owner = self._owners.get(run_id)
        return run_id if owner is not None and owner.key == identity.key else None

    def _active_run(self, identity: ChannelIdentity) -> str | None:
        run_id = self._latest.get(identity.key)
        if run_id is None:
            return None
        return run_id if run_id in self.registry.live_ids() else None

    def run_for(self, identity: ChannelIdentity) -> str | None:
        """The most recent run this identity started, if it started one."""
        return self._latest.get(identity.key)

    def _say(
        self,
        identity: ChannelIdentity,
        text: str,
        kind: ResponseKind = ResponseKind.NOTICE,
        *,
        run_id: str | None = None,
    ) -> ChannelResponse:
        return ChannelResponse(identity, text, kind, run_id)

    async def _on_event(self, event: RuntimeEvent) -> None:
        identity = self._owners.get(event.session_id)
        if identity is None:
            return
        response = render_event(event, identity)
        if response is not None:
            await self._deliver(response)

    async def _deliver(self, response: ChannelResponse) -> None:
        """Hand a response to the adapter, treating a failed send as a failed send.

        A channel that is momentarily unreachable must not take the runtime down with it,
        and retrying is the adapter's job — it is the only side that knows what its service
        considers transient.
        """
        try:
            await self.adapter.deliver(response)
        except AthenaRuntimeError as error:
            _logger.warning("channel.delivery_failed code=%s", error.code)


async def serve_channel(
    adapter: ChannelAdapter,
    registry: RunRegistry,
    policy: ChannelAccessPolicy,
    event_bus: EventBus,
    *,
    bare_text_starts_run: bool = False,
) -> None:
    """Open a channel, serve it until it closes, and close it cleanly either way."""
    gateway = ChannelGateway(
        adapter, registry, policy, event_bus, bare_text_starts_run=bare_text_starts_run
    )
    await gateway.start()
    try:
        await gateway.run_forever()
    finally:
        with contextlib.suppress(asyncio.CancelledError):
            await gateway.stop()


__all__ = [
    "ChannelAccessPolicy",
    "ChannelGateway",
    "ChannelGrant",
    "serve_channel",
]
