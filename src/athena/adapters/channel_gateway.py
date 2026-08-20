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
from athena.identity import IdentityDirectory, ResolvedIdentity

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

    def for_owner(self, owner_key: str) -> ChannelGrant | None:
        """A grant belongs to whoever owns the conversation.

        For a linked account that is the Athena user, so a workspace granted once is
        reachable from every surface that person uses. For an unlinked one it is the
        channel account, which is the only thing there is to grant to.
        """
        return self._grants.get(owner_key)

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
        directory: IdentityDirectory | None = None,
        bare_text_starts_run: bool = False,
    ) -> None:
        self.adapter = adapter
        self.registry = registry
        self.policy = policy
        self.event_bus = event_bus
        #: Athena's answer to "who is this". Without one, every channel account is its own
        #: person — which is correct, just not shared.
        self.directory = directory
        #: Whether plain text is an objective. Off unless the channel removed the
        #: ambiguity itself — see `parse_command`.
        self.bare_text_starts_run = bare_text_starts_run
        #: Which run belongs to whom, keyed by owner rather than by channel account. That
        #: is the whole point of linking: a run started in ChatyGPT is the same run
        #: Telegram can see and cancel, instead of a second one nobody asked for.
        self._owners: dict[str, str] = {}
        #: Where to answer an owner. Updated whenever they speak, because a person can
        #: reach Athena from more than one place and the newest is where they are looking.
        self._reply_to: dict[str, ChannelIdentity] = {}
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

    async def resolve(self, identity: ChannelIdentity) -> ResolvedIdentity:
        """Turn a channel account into the person behind it, if Athena knows of one.

        A directory failure resolves to "not linked" rather than propagating. Being unable
        to look someone up is not grounds for treating them as somebody else, and the
        unlinked path is already the safe one: they keep their own runs.
        """
        if self.directory is None:
            return ResolvedIdentity(identity)
        try:
            user = await self.directory.resolve(identity)
        except AthenaRuntimeError as error:
            _logger.warning("channel.identity_unresolved code=%s", error.code)
            return ResolvedIdentity(identity)
        return ResolvedIdentity(identity, user)

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

        # Linking comes before the grant check on purpose: someone who has a code has not
        # been granted anything yet, and telling them to go away would make the code
        # unusable. It is the one command an ungranted account may run.
        if command.name is CommandName.LINK_IDENTITY:
            return await self._link(identity, command.link_code or "")
        if command.name is CommandName.UNLINK_IDENTITY:
            return await self._unlink(identity)

        resolved = await self.resolve(identity)
        owner = resolved.owner_key
        self._reply_to[owner] = identity
        grant = self.policy.for_owner(owner)
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
                return await self._start_run(resolved, grant, command.objective or "")
            case CommandName.CANCEL_RUN:
                return await self._cancel_run(resolved, command.run_id)
            case CommandName.RUN_STATUS:
                return await self._run_status(resolved, command.run_id)
            case CommandName.LIST_RUNS:
                return await self._list_runs(resolved)
            case _:  # pragma: no cover - CommandName is closed and handled above
                return self._say(identity, HELP_TEXT)

    async def _start_run(
        self, resolved: ResolvedIdentity, grant: ChannelGrant, objective: str
    ) -> ChannelResponse:
        identity = resolved.channel
        owner = resolved.owner_key
        active = self._active_run(owner)
        if active is not None:
            # One at a time, per *person* rather than per account. Linking would otherwise
            # be worse than nothing: the same human could start one run from each surface
            # and get two, which is exactly the duplication linking exists to prevent.
            return self._say(
                identity,
                f"A run is already going ({active}). Cancel it before starting another.",
                ResponseKind.ERROR,
                run_id=active,
            )
        workspace = build_workspace(grant.workspace_root, self.policy.workspace_check())
        run_id = await self.registry.start(objective, workspace, grant.options)
        self._owners[run_id] = owner
        self._latest[owner] = run_id
        _logger.info("channel.run_started identity=%s run=%s", identity.key, run_id)
        return self._say(
            identity,
            f"Started. I will tell you how it goes.\nRun {run_id}",
            # Not PROGRESS: this answers a command, and a channel is free to drop
            # progress. Being told nothing after asking for a run is the worst outcome.
            ResponseKind.NOTICE,
            run_id=run_id,
        )

    async def _cancel_run(self, resolved: ResolvedIdentity, run_id: str | None) -> ChannelResponse:
        identity = resolved.channel
        target = self._run_reference(resolved.owner_key, run_id)
        if target is None:
            return self._say(identity, "You have no run to cancel.", ResponseKind.ERROR)
        await self.registry.cancel(target)
        return self._say(identity, "Cancelling.", ResponseKind.NOTICE, run_id=target)

    async def _run_status(self, resolved: ResolvedIdentity, run_id: str | None) -> ChannelResponse:
        identity = resolved.channel
        target = self._run_reference(resolved.owner_key, run_id)
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

    async def _list_runs(self, resolved: ResolvedIdentity) -> ChannelResponse:
        """The asker's own runs, newest first.

        Filtered by owner rather than listed wholesale: the registry knows about every run
        on the host, and a channel is not a window onto other people's work. For a linked
        account "their own" spans every surface they use, which is the point.
        """
        owner = resolved.owner_key
        mine = [run_id for run_id, holder in self._owners.items() if holder == owner]
        if not mine:
            return self._say(resolved.channel, "You have no runs yet.")
        lines: list[str] = []
        for run_id in reversed(mine[-_RUNS_LISTED:]):
            record = await self.registry.snapshot(run_id)
            state = record.status.value if record is not None else "unknown"
            objective = record.objective if record is not None else "(no record)"
            lines.append(f"{state} — {objective}\n{run_id}")
        return self._say(resolved.channel, "\n\n".join(lines), ResponseKind.RESULT)

    async def _link(self, identity: ChannelIdentity, code: str) -> ChannelResponse:
        """Redeem a link code, which is the only way an account becomes a person.

        Every decision is the directory's. This does not compare names, does not look at
        who is already linked to what, and does not decide that two accounts match — it
        hands over the code and reports what came back.
        """
        if self.directory is None:
            return self._say(identity, "Identity linking is not enabled here.", ResponseKind.ERROR)
        result = await self.directory.redeem(code, identity)
        if result.linked and result.user_id is not None:
            # Anything this account owned as a stranger stays with the stranger key. It is
            # not silently reassigned: linking says who someone is from now on, and
            # rewriting history would be inventing a fact nobody asserted.
            self._reply_to[result.user_id] = identity
        return self._say(
            identity,
            result.message,
            ResponseKind.RESULT if result.linked else ResponseKind.ERROR,
        )

    async def _unlink(self, identity: ChannelIdentity) -> ChannelResponse:
        if self.directory is None:
            return self._say(identity, "Identity linking is not enabled here.", ResponseKind.ERROR)
        removed = await self.directory.unlink(identity)
        if not removed:
            return self._say(identity, "This account was not linked.", ResponseKind.NOTICE)
        return self._say(
            identity,
            "Unlinked. This account is on its own again, and no longer sees the runs it shared.",
            ResponseKind.RESULT,
        )

    def _run_reference(self, owner_key: str, run_id: str | None) -> str | None:
        """Pick the run a command refers to, refusing anything the asker does not own.

        An explicit id belonging to somebody else resolves to nothing rather than to an
        error naming it: whether a given run id exists is not a channel's business.
        """
        if run_id is None:
            return self._latest.get(owner_key)
        return run_id if self._owners.get(run_id) == owner_key else None

    def _active_run(self, owner_key: str) -> str | None:
        run_id = self._latest.get(owner_key)
        if run_id is None:
            return None
        return run_id if run_id in self.registry.live_ids() else None

    async def run_for(self, identity: ChannelIdentity) -> str | None:
        """The most recent run the person behind this account started."""
        resolved = await self.resolve(identity)
        return self._latest.get(resolved.owner_key)

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
        """Route a run's event to the person who owns it.

        Two lookups rather than one: the run says who owns it, and the owner says where
        they were last reached. That indirection is what lets a run started before a link
        keep reporting, and a linked person hear about their run wherever they are now.
        """
        owner = self._owners.get(event.session_id)
        if owner is None:
            return
        identity = self._reply_to.get(owner)
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
    directory: IdentityDirectory | None = None,
    bare_text_starts_run: bool = False,
) -> None:
    """Open a channel, serve it until it closes, and close it cleanly either way."""
    gateway = ChannelGateway(
        adapter,
        registry,
        policy,
        event_bus,
        directory=directory,
        bare_text_starts_run=bare_text_starts_run,
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
