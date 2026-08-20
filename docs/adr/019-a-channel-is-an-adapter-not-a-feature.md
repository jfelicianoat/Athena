# ADR-019: A channel is an adapter, not a feature

- Status: **Accepted** — implemented 2026-08-20 in `athena.channels`, `athena.adapters.channel_gateway`, `athena.testing`
- Date: 2026-08-20
- Affects: Athena (new boundary); first concrete channel (`athena_telegram`) added 2026-08-20

## Context

Athena has two ways in: the CLI, and ChatyGPT over the HTTP service adapter (ADR-017). A
chat channel is a third shape, and the request that produces one is always for a specific
service — Telegram this time. Written that way, the first channel becomes the interface: a
messaging SDK appears in the dependency list, chat ids leak into the runtime's vocabulary,
and the second channel is a rewrite disguised as a port.

The runtime already refuses this everywhere it matters. ADR-002 keeps AI_Broker an optional
`ModelProvider`; ADR-007 says interfaces consume events rather than owning agent logic. A
channel is the same problem in a new place, and it arrives with two extra hazards the HTTP
client does not have: anyone who finds a bot can message it, and everything it receives is
free text from a person who may not know what a workspace is.

## Decision

**The boundary exists before any channel does.** `athena/channels.py` defines
`ChannelIdentity`, `ChannelMessage`, `ChannelResponse`, `ChannelEventSink` and
`ChannelAdapter`, imports only `athena.events` and `athena.types`, and mentions no service.
A concrete adapter lives outside the runtime and is handed to the gateway; the runtime never
learns what it is. Two tests enforce this by reading the import statements rather than by
trusting reviewers — one for the whole package against a list of channel SDKs, one for
`channels.py` against reaching sideways into an adapter.

**Both translations narrow.** Inbound, a `ChannelMessage` becomes a `ChannelCommand` from a
closed set — start, cancel, status, list, new, help — and each maps to a method the registry
already exposes. Outbound, a `RuntimeEvent` becomes *at most one* `ChannelResponse`, and most events
become nothing. A chat that relays `tool.progress` is a chat nobody reads.

**Free text is not an objective until a channel earns it.** The default is that only
explicit commands act, because the cost of a false positive is an agent running against
someone's repository because they said hello. A channel opts in via `bare_text_starts_run`
only by removing the ambiguity another way — Telegram does it by refusing group chats and
refusing accounts that are not allow-listed, which leaves a one-to-one conversation with a
known person and nobody to overhear.

**Identity is a grant, not a default.** `ChannelAccessPolicy` is an allow-list keyed by
`channel:user_id`, mapping an identity to a workspace and fixed capability modes. Unknown
identities are refused in the same words every time, because two differently worded
refusals are a way to probe the table. The channel cannot negotiate its own permissions
upward.

**A channel cannot answer a permission prompt.** A chat account is a weak claim of identity,
and ADR-018's approval is a person deciding about one specific action. A run started from a
channel carries the modes its identity was granted, and anything left at ASK meets Athena's
unattended default, which is to refuse. The refusal is reported rather than swallowed:
silence would make a run that refused itself look like a run that did nothing.

**One run at a time per identity.** Otherwise a chat account is an unbounded way to spend
the host's CPU, and no event can be attributed to the run it came from.

**Only Athena's own error taxonomy becomes a reply.** Anything unclassified ends the
gateway instead of being swallowed, which is the position `RecoveryPolicy` already takes
inside the loop. A failed *delivery*, by contrast, is ordinary: the adapter owns its
retries, because it is the only side that knows what its service considers transient.

**`FakeChannelAdapter` satisfies the protocols structurally, not by inheritance.** A real
adapter wraps a third-party client and cannot subclass anything of Athena's; a fake that
only worked by subclassing would be testing a shape nothing real has.

## Consequences

The boundary shipped before any channel did, which is the whole point. `athena_telegram`
followed (2026-08-20) and cost four methods plus its own transport concerns — duplicates,
malformed updates, rate limits — none of which reached the runtime. It lives outside
`athena/`, so the import scan above still passes with a real channel in the repository.

Two things the boundary had to give ground on, both recorded in this ADR's own terms rather
than worked around. `parse_command` gained `bare_text_starts_run`, off by default: plain
text is an objective only where a channel has removed the ambiguity itself — a private,
one-to-one, allow-listed conversation. And `ResponseKind.PROGRESS` acquired a meaning it
did not have: *unsolicited, and safe to drop*. A reply to a command is never PROGRESS,
because a channel that coalesces on a rate limit would otherwise swallow the answer to
`/cancel` and read as the bot ignoring you.

Writing the boundary first surfaced a defect immediately: the renderer read `error` and
`code` from `agent.failed`, which publishes `error_code` and `message`, so the most
important message of a run rendered as "no detail given". An end-to-end test against the
real registry caught it; the unit test had asserted against an imagined payload.

`ChannelGrant` duplicates a little of what `RunOptions` expresses per run over HTTP. That is
deliberate: over a channel the modes are a property of the identity, decided in advance,
not a per-request argument the caller supplies.

## Alternatives rejected

**Handing the adapter the `EventBus`.** Every channel would then learn Athena's internals
and decide for itself what is worth relaying, which is how one channel ends up silent and
another floods. `ChannelEventSink` receives responses that have already been chosen.

**Treating any message as an objective, with commands as an optional prefix.** Convenient
for exactly one message and dangerous for every other one.

**A Telegram adapter now, generalised later.** The generalisation never happens; the first
implementation becomes the contract. This is the ordering the ADR exists to prevent.

**Chat-based approvals.** Not omitted for effort — it needs an identity story stronger than
a chat account and its own delivery and expiry clocks, which is ADR-018's machinery over a
transport that cannot yet carry it. It deserves its own decision.
