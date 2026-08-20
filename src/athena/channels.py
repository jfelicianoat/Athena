"""The boundary between Athena and any conversational channel.

Athena has one interface today (the CLI) and one client (ChatyGPT, over HTTP). A chat
channel — Telegram, Slack, IRC, a terminal that reads stdin — is a third shape, and the
temptation is to write it once against whichever service happened to be first. That is how
a runtime acquires a dependency on a messaging SDK and a set of assumptions about chat ids,
and it is why this module exists before any concrete channel does.

Everything here is stdlib. Nothing under `athena/` imports a channel SDK; a concrete
adapter lives outside the runtime, is handed to the gateway, and the runtime never learns
what it is. `tests/test_channels.py` enforces that structurally rather than by good
intentions.

Two translations define the boundary, and both are narrowing:

- **Inbound**, a `ChannelMessage` becomes a `ChannelCommand` — a small closed set of things
  Athena can be asked to do. Free text is not an objective unless a channel has earned it:
  a stray message must never start an agent against a workspace, so `bare_text_starts_run`
  is off until the channel can show there is no ambiguity left to exploit.
- **Outbound**, a `RuntimeEvent` becomes at most one `ChannelResponse`. Most events become
  nothing. A chat is not a log, and a channel that relays `tool.progress` is a channel
  nobody reads.

What a channel deliberately cannot do is answer a permission prompt. A chat account is a
weak claim of identity, and ADR-009's single-use approval is a decision by a person about a
specific action; inventing a chat protocol for it is a design question in its own right,
not a detail of this boundary. A run started from a channel therefore carries the
capability modes its identity was registered with, and anything left at ASK meets Athena's
unattended default, which is to refuse.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from athena.events import EventName, RuntimeEvent
from athena.types import JSONObject

#: Longest objective a channel may submit. A chat message is not a specification, and an
#: unbounded one would arrive in the model's context whatever the channel decided.
MAX_OBJECTIVE_CHARS = 2_000


@dataclass(frozen=True, slots=True)
class ChannelIdentity:
    """Who is speaking, in the channel's own terms.

    `channel` and `user_id` together are the identity; `conversation_id` is where a reply
    belongs, which is not the same thing — the same person may speak in several rooms, and
    a room may hold several people. Keeping them separate is what lets a policy grant a
    workspace to a person rather than to a chat.

    `display_name` is for humans reading logs and is never used to decide anything.
    """

    channel: str
    user_id: str
    conversation_id: str
    display_name: str | None = None

    @property
    def key(self) -> str:
        """Stable identity key. Two adapters for the same channel agree on this."""
        return f"{self.channel}:{self.user_id}"

    def to_json(self) -> JSONObject:
        return {
            "channel": self.channel,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "display_name": self.display_name,
        }


@dataclass(frozen=True, slots=True)
class ChannelMessage:
    """One inbound message, already stripped of everything channel-specific.

    An adapter is responsible for turning its own payload into this: no attachments, no
    formatting, no reply threading, no bot API objects. What survives is text and who sent
    it, because that is all the boundary can promise every channel will have.
    """

    identity: ChannelIdentity
    text: str
    message_id: str | None = None
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class ResponseKind(StrEnum):
    """How a response should read, without saying how it should look.

    Rendering belongs to the adapter — bold text, an emoji, a colour, a thread — because
    only it knows what its channel supports. This says what the message *is*.
    """

    NOTICE = "notice"
    #: Unsolicited, and safe to drop. Only `render_event` produces these: nobody asked for
    #: them, so a channel with a rate limit may coalesce or discard them. A reply to a
    #: command is never PROGRESS, because dropping one reads as the bot ignoring you.
    PROGRESS = "progress"
    RESULT = "result"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ChannelResponse:
    """What Athena wants said, and where."""

    identity: ChannelIdentity
    text: str
    kind: ResponseKind = ResponseKind.NOTICE
    run_id: str | None = None

    def to_json(self) -> JSONObject:
        return {
            "identity": self.identity.to_json(),
            "text": self.text,
            "kind": self.kind.value,
            "run_id": self.run_id,
        }


class CommandName(StrEnum):
    """The closed set of things a channel may ask for.

    Closed on purpose. Each entry maps to a method the runtime already exposes; adding one
    means deciding that a channel should be able to do that, which is a decision, not a
    convenience.
    """

    START_RUN = "start_run"
    CANCEL_RUN = "cancel_run"
    RUN_STATUS = "run_status"
    LIST_RUNS = "list_runs"
    #: Clear the slate and invite an objective. Distinct from `START_RUN` because it
    #: carries none: it is the prompt, not the request.
    NEW_INTERACTION = "new_interaction"
    HELP = "help"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ChannelCommand:
    """An application command, with the identity that asked for it.

    This is the only thing that crosses from the channel side into the runtime side. It
    carries no channel object, no socket, no SDK type — which is what makes the runtime
    unable to depend on a channel even by accident.
    """

    name: CommandName
    identity: ChannelIdentity
    objective: str | None = None
    run_id: str | None = None
    #: Why the command could not be understood. Only set for `UNKNOWN`.
    reason: str | None = None


@runtime_checkable
class ChannelEventSink(Protocol):
    """Where translated runtime events go.

    An adapter implements this to receive what Athena has to say. It is deliberately not
    the `EventBus`: the bus speaks in runtime events, and handing those to a channel would
    make every channel learn Athena's internals and decide for itself what is worth
    relaying — which is how one channel ends up quiet and another floods.
    """

    async def deliver(self, response: ChannelResponse) -> None:
        """Send one response. Must not raise for an ordinary delivery failure."""
        ...


@runtime_checkable
class ChannelAdapter(Protocol):
    """A concrete channel, from the runtime's point of view.

    Both halves are here because they are two directions of one connection: `listen` brings
    messages in for as long as the channel is open, and `deliver` — inherited from
    `ChannelEventSink` — takes responses out. An adapter owns its transport, its
    credentials and its retries; the runtime owns none of those and cannot see them.
    """

    @property
    def channel(self) -> str:
        """Channel name, matching `ChannelIdentity.channel`."""
        ...

    async def start(self) -> None:
        """Open the channel. Called once before any message is read."""
        ...

    async def stop(self) -> None:
        """Close the channel and release its transport."""
        ...

    async def receive(self) -> ChannelMessage | None:
        """Next inbound message, or `None` when the channel is closed for good.

        Returning `None` ends the gateway's loop. A transient failure is the adapter's to
        retry — the runtime cannot know what is transient for a channel it has never heard
        of.
        """
        ...

    async def deliver(self, response: ChannelResponse) -> None:
        """Send one response."""
        ...


def parse_command(message: ChannelMessage, *, bare_text_starts_run: bool = False) -> ChannelCommand:
    """Translate an inbound message into an application command.

    Parsing is deliberately dumb — a prefix and a remainder. A channel message is not a
    place to be clever about intent, and anything richer would be a second command language
    to keep in step with the real one.

    `bare_text_starts_run` decides what plain text means, and it is off by default. The
    hazard is an agent running against someone's repository because they said hello, so a
    channel only earns it by removing the ambiguity some other way: a private, one-to-one
    conversation with an allow-listed account, where there is nobody else in the room and
    nothing to overhear. A group chat never earns it.
    """
    identity = message.identity
    text = message.text.strip()
    if not text:
        return ChannelCommand(CommandName.UNKNOWN, identity, reason="The message was empty")
    if not text.startswith("/"):
        if bare_text_starts_run:
            if len(text) > MAX_OBJECTIVE_CHARS:
                return ChannelCommand(
                    CommandName.UNKNOWN,
                    identity,
                    reason=f"That objective is longer than {MAX_OBJECTIVE_CHARS} characters",
                )
            return ChannelCommand(CommandName.START_RUN, identity, objective=text)
        return ChannelCommand(
            CommandName.UNKNOWN,
            identity,
            reason="Athena only acts on explicit commands. Send /help to see them.",
        )

    verb, _, rest = text[1:].partition(" ")
    argument = rest.strip()
    match verb.lower():
        case "run" | "new":
            # `/new` with an objective is `/run`; without one it is an invitation, which
            # is why the two are not the same command.
            if not argument and verb.lower() == "new":
                return ChannelCommand(CommandName.NEW_INTERACTION, identity)
            if not argument:
                return ChannelCommand(
                    CommandName.UNKNOWN, identity, reason="Say what to do: /run <objective>"
                )
            if len(argument) > MAX_OBJECTIVE_CHARS:
                return ChannelCommand(
                    CommandName.UNKNOWN,
                    identity,
                    reason=f"That objective is longer than {MAX_OBJECTIVE_CHARS} characters",
                )
            return ChannelCommand(CommandName.START_RUN, identity, objective=argument)
        case "cancel":
            return ChannelCommand(CommandName.CANCEL_RUN, identity, run_id=argument or None)
        case "status":
            return ChannelCommand(CommandName.RUN_STATUS, identity, run_id=argument or None)
        case "runs":
            return ChannelCommand(CommandName.LIST_RUNS, identity)
        case "help" | "start":
            return ChannelCommand(CommandName.HELP, identity)
        case _:
            return ChannelCommand(CommandName.UNKNOWN, identity, reason=f"Unknown command: /{verb}")


HELP_TEXT = (
    "Athena understands:\n"
    "/new <objective> — start a run in your workspace\n"
    "/status [run id] — how your latest run is doing\n"
    "/runs — your recent runs\n"
    "/cancel [run id] — stop the current one\n"
    "\n"
    "Permission requests cannot be answered from here. A run started from a channel "
    "only does what your workspace was configured to allow without asking."
)

NEW_INTERACTION_TEXT = (
    "Ready. Tell me what to do, in one message.\n"
    "It runs in the workspace this account was granted, and nowhere else."
)


#: Events a channel is told about. Everything else is suppressed rather than forwarded,
#: because a channel is a conversation and a conversation cannot absorb a run's full event
#: stream. Each of these is either the end of something or a fact a person would want
#: pushed to them unprompted.
REPORTED_EVENTS: frozenset[EventName] = frozenset(
    {
        EventName.AGENT_STARTED,
        EventName.AGENT_COMPLETED,
        EventName.AGENT_FAILED,
        EventName.AGENT_CANCELLED,
        EventName.PERMISSION_REQUESTED,
        # Phase changes, not chatter: the run stopped writing and started proving, or
        # started repairing itself. Both are things a person waiting would want pushed.
        EventName.VERIFICATION_STARTED,
        EventName.RECOVERY_STARTED,
        EventName.VERIFICATION_COMPLETED,
        EventName.VERIFICATION_FAILED,
        EventName.RECOVERY_EXHAUSTED,
    }
)


def _text(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def render_event(event: RuntimeEvent, identity: ChannelIdentity) -> ChannelResponse | None:
    """Translate a runtime event into at most one channel response.

    `None` means "say nothing", which is the answer for most events. What comes back is
    operational fact — what happened, to which run, with what verdict — and never the
    model's reasoning, which does not reach this layer in the first place.

    Payloads arrive already redacted by the bus. This adds no field the runtime did not
    publish, so a channel cannot become a way to read something the HTTP client could not.
    """
    if event.name not in REPORTED_EVENTS:
        return None

    run_id = event.session_id
    payload = event.payload

    def response(text: str, kind: ResponseKind = ResponseKind.NOTICE) -> ChannelResponse:
        return ChannelResponse(identity, text, kind, run_id)

    match event.name:
        case EventName.AGENT_STARTED:
            return response("Athena has started.", ResponseKind.PROGRESS)
        case EventName.AGENT_COMPLETED:
            # The verification summary rides along because it is the evidence: a run that
            # says it finished without saying what it proved is the exact claim Athena
            # refuses to accept from the model, and a channel should not launder it.
            evidence = _text(payload, "verification")
            text = "Athena finished." if evidence is None else f"Athena finished. {evidence}"
            return response(text, ResponseKind.RESULT)
        case EventName.AGENT_FAILED:
            detail = _text(payload, "message") or _text(payload, "error_code") or "no detail given"
            return response(f"Athena stopped: {detail}", ResponseKind.ERROR)
        case EventName.AGENT_CANCELLED:
            return response("The run was cancelled.", ResponseKind.RESULT)
        case EventName.PERMISSION_REQUESTED:
            # Worth saying precisely because it cannot be answered here: silence would
            # leave a run that refused itself looking like a run that did nothing.
            action = _text(payload, "action") or _text(payload, "tool_name") or "an action"
            return response(
                f"Athena needed permission for {action} and could not ask anyone here, "
                "so it was refused.",
                ResponseKind.NOTICE,
            )
        case EventName.VERIFICATION_STARTED:
            return response("Checking that the changes hold up.", ResponseKind.PROGRESS)
        case EventName.RECOVERY_STARTED:
            return response(
                "Something failed; Athena is trying to repair it.", ResponseKind.PROGRESS
            )
        case EventName.VERIFICATION_COMPLETED:
            status = _text(payload, "status") or "unknown"
            wording = {
                "passed": "Verification passed.",
                "failed": "Verification failed.",
                "inconclusive": "Verification was inconclusive: nothing was proven.",
            }.get(status, f"Verification finished: {status}")
            kind = ResponseKind.RESULT if status == "passed" else ResponseKind.ERROR
            return response(wording, kind)
        case EventName.VERIFICATION_FAILED:
            return response("Verification failed.", ResponseKind.ERROR)
        case EventName.RECOVERY_EXHAUSTED:
            return response("Athena ran out of recovery attempts and stopped.", ResponseKind.ERROR)
        case _:  # pragma: no cover - REPORTED_EVENTS and the arms above are one list
            return None


__all__ = [
    "HELP_TEXT",
    "MAX_OBJECTIVE_CHARS",
    "NEW_INTERACTION_TEXT",
    "REPORTED_EVENTS",
    "ChannelAdapter",
    "ChannelCommand",
    "ChannelEventSink",
    "ChannelIdentity",
    "ChannelMessage",
    "ChannelResponse",
    "CommandName",
    "ResponseKind",
    "parse_command",
    "render_event",
]
