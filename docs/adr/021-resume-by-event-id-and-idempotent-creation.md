# ADR-021: Resume by event id, and create runs idempotently

- Status: **Accepted** — implemented 2026-08-20 in `athena.adapters.service`
- Date: 2026-08-20
- Affects: Athena service adapter; amends ADR-017 §5
- Amends: **ADR-017 §5** ("Reconnection is snapshot-then-tail, not an event log")

## Context

The external interface itself already existed: HTTP for commands, SSE for progress,
loopback-only, bearer token per start, create-and-return-an-id rather than a request held
open for the length of a run. An audit against what an external application actually needs
found three things that were claimed rather than true.

**Event ids were emitted and ignored.** Every SSE frame carried `id:`, so a browser — or
any conforming client — would send `Last-Event-ID` on reconnect, and the server did nothing
with it. Worse than useless: it looks like resume support.

**`POST /v1/runs` was not idempotent.** A client that retried a timed-out create got two
agents on one workspace, which is precisely the outcome the retry was meant to avoid, and
starting a run is not the sort of thing that can be undone by noticing afterwards.

**Internal exception class names reached the wire.** The catch-all handler answered
`{"code": "internal_error", "message": "KeyError"}`. That tells a caller about the inside of
the process and tells them nothing they can act on.

## Decision

**A bounded replay buffer, which is not the journal ADR-017 refused.** Each live run keeps
its last 256 events in memory. On reconnect with `Last-Event-ID`, a client close enough
behind is caught up event by event and keeps whatever it had derived; a client that has been
away longer gets the snapshot, exactly as before.

ADR-017 rejected an event journal because Athena persists structured state, not a transcript,
and a journal would duplicate the session store. That reasoning stands and this does not
violate it: the buffer is in memory, dies with the process, is bounded by a constant rather
than by how long a client stays away, and is never the source of truth. Snapshot-then-tail
remains the fallback, and is what every client that does not resume still gets.

**`None` and `()` are different answers.** "You are up to date" and "that id fell out of the
window" must not look alike; collapsing them would let a client believe it had missed
nothing. `replay_after` returns `None` for the second, and the stream falls back to a
snapshot.

**Subscribe first, replay second, de-duplicate.** The stream subscribes before reading the
buffer, so the window between the two is covered twice rather than not at all, and ids
already replayed are skipped when draining the queue. A client that counts things must not
count them twice.

**Recorded before delivery.** `_fan_out` appends to the buffer before pushing to
subscribers, so an event a slow consumer dropped is still one it can replay. That is what
makes dropping a slow subscriber survivable rather than lossy.

**`Idempotency-Key` creates at most one run.** The in-flight case uses a future rather than
a "seen" set, because the window that matters is exactly the one where the first call has
not returned — a check-then-act across `await registry.start(...)` lets both callers miss. A
replay answers 200 with `idempotent_replay: true`; the original answers 201. A failed
attempt withdraws its key and cancels the future, so a concurrent waiter falls through and
does the work instead of blocking on a promise nobody will keep.

**Internal errors are logged, not published.** The client gets `internal_error` and a
sentence; the class name and traceback go to the log.

## Consequences

`ServiceConfig` and the route table are unchanged for existing clients. ChatyGPT's Rust
client does not send `Last-Event-ID` today, so it keeps getting snapshot-then-tail — the
change is additive, and teaching that client to resume is a separate, optional piece of
work.

Writing the idempotency path surfaced a defect in it: `Response` stores its JSON under
`payload`, not `body`, so reading `response.body.get("run_id")` always produced `None`. The
first version had a "cannot happen" guard that swallowed exactly that case and quietly
disabled the feature while the tests still passed on the happy path. The guard was the bug;
it now raises, and the concurrent-retry test is what proved the difference.

## Not implemented, and why

**A tasks endpoint.** `TaskManager` exists from H7, but no run wires one in — `AgentLoop`
does not use it. Exposing `/v1/runs/{id}/tasks` would mean inventing the wiring first, which
is implementing a contract's anticipation rather than a need. `GET /v1/runs/{id}` and
`/v1/health` already answer "what is the status", which is the part of that requirement that
is real today.

**WebSocket.** Progress is one-way, and commands are ordinary POSTs that must work from a
different connection anyway — the controlling client proves itself with
`X-Athena-Subscriber` rather than by owning the socket. A duplex transport would add a
framing layer and a second reconnect story to buy nothing.
