# ADR-025: A fact belongs to a run, not to the session that published it

- Status: **Accepted** — implemented 2026-08-22 in `athena.run_event_log`, wired through `athena.adapters.service.runs`, `athena.adapters.service.server` and `athena_service`; the naming seam it depends on is in `athena.subagents`
- Date: 2026-08-22
- Affects: what survives a restart, and what a run can be asked about afterwards

## Context

Athena had two records of a run and neither answered "what happened".

`EventBus` is live. Whoever was not listening does not find out, which is correct for what
it does — telling interfaces about a run while it runs — and useless the moment the process
restarts or a client arrives late. `SessionStore` keeps **state**: where a run is, not how
it got there. Between them there was no way to answer, after the fact, why a run ended as
it did, which tasks were attempted, or who ran a given tool call.

The gap showed up as soon as runs became hierarchical. A run with delegates publishes on
one bus from several sessions: the run's own, and one per child. Anything keyed by
`session_id` would therefore split a single run's history into as many disconnected pieces
as agents took part, each one silent about what it belonged to. Worse, a delegate's session
id was only announced when the delegate **finished** — so everything it did while working
arrived unattributable, which is exactly the window somebody is watching.

## Decision

**A third record: `RunEventLog`, append-only, storing facts in order, each one carrying who
produced it and on whose behalf.**

This is not event sourcing and does not replace either of the other two. State remains the
source of truth for where a run is; the log is the source for how it got there. Nothing is
rebuilt from it and no decision is taken by replaying it.

### Little is kept, on purpose

`DURABLE` is a closed set. `model.started`, `tool.progress` and stream deltas describe the
path rather than the result: keeping them would make the log grow with how talkative the
model is instead of with what the agent did, and every run would pay to write it. A fact is
durable if somebody would need it to **explain** the run tomorrow — decisions, outcomes,
permissions, verification, and the lifecycle of tasks and delegates.

### Provenance is four fields, and the run id is the root

| Field | Meaning |
| --- | --- |
| `run_id` | the **root** run the fact belongs to — not the session that published it |
| `session_id` | who published it |
| `task_id` | the graph task it happened inside, or `None` |
| `actor` | the role that acted: `run` for the root, the delegate's role otherwise |

`run_id` being the root is what gives a hierarchical run one history instead of one per
agent. `session_id` is kept beside it rather than replaced, because "the run did this" and
"a delegate of the run did this" are different claims and collapsing them would let a
child's failure read as the run's.

`seq` is assigned by the log, monotonically **per run**. Letting the emitter choose would
have two concurrent components pick the same number, and an order that is not an order.
`event_id` from the bus is preserved, so the same fact seen live and read back later is one
fact and not two.

### Lineage is learned from the events, never configured

Two events carry the whole tree: `task.started` says which task belongs to which run, and
`subagent.started` says which child session belongs to which parent. The log reads both and
remembers, under the same lock as the write — resolving provenance outside it would let two
concurrent events consult a half-built lineage and attribute themselves to the wrong run.
The lineage is persisted too, so a delegate that started before a restart still belongs to
its run after one.

**A session with no known lineage is its own root.** Adopting it into the most recent run
would be an invented attribution, and an invented attribution is worse than a missing one
because it reads exactly like a verified one.

### Consequence for delegation: a delegate is named before it works

The lineage rule above only holds if the child's session id exists when the child starts.
`SubagentRunner.delegate` therefore generates the id up front, announces it in
`subagent.started`, and passes it to `AgentLoop.run`. This is a change to the runtime, not
to the log: previously the id was knowable only from `subagent.completed`, which meant no
observer — durable or live — could attribute a delegate's work while it was happening.

### Reading it is a different endpoint from watching it

`GET /v1/runs/{id}/events` is the stream and does not exist for whoever arrives late.
`GET /v1/runs/{id}/history` is the record, and exists precisely for them. It accepts
`?after=` as a cursor and `?task=` to ask what happened inside one task, and it returns the
derived summary alongside the facts so that every client does not have to agree separately
on how to read them.

A run with nothing recorded answers **404**, not `200` with an empty list. Either it never
existed or it predates the log, and an empty list would pass off the absence of history as
a complete history.

## Consequences

- The log needs a file. `:memory:` is refused outright: every operation opens its own
  connection, so an in-memory database would accept writes and return nothing — failing in
  the shape of a datum, which is the worst shape to fail in.
- `RunRegistry` subscribes like any other observer and schedules its writes rather than
  awaiting them, so persisting a fact never becomes latency on the action that produced it.
  `shutdown()` drains what is in flight, because the facts still being written at shutdown
  are the ones saying how the run ended.
- A write that fails is suppressed at the subscriber, not raised. Instrumentation that can
  take down the thing it measures is a liability, and this one measures everything.
- `replay()` reports only what the facts support, and only the root decides the run's
  status. A delegate that failed is not a run that failed: the parent may retry the task or
  judge it dispensable, and believing the child would declare finished something still
  running.
- The log is additive with respect to ADR-021: resuming by event id still reads the live
  registry's buffer. The history endpoint answers a different question — what happened —
  and does not make a run resumable.
