# ADR-018: An approval is a decision with a record, not a click

- Status: **Accepted** — implemented 2026-08-19 in Athena (`adapters.service.approvals`) and ChatyGPT (`athena` module, `db`, `AthenaArea`)
- Date: 2026-08-19
- Affects: Athena (approval projection), ChatyGPT (permission UI, audit trail, run persistence)

## Context

ADR-017 opened Athena to an external client and ADR-016's remote prompt carried the three
clocks that keep a slow network from eating a person's thinking time. What neither settled
is what the person actually sees, and what remains once they have answered.

Both matter more than they sound. An approval that shows only `write_file` asks someone to
authorise a category, not an action — and a category is exactly what the permission engine
already decided; the human is there to judge the particular case. Meanwhile the decision
itself is the one moment where a person overrides the deterministic policy. If that leaves
no trace, "who let the agent do this" has no answer.

Five situations arise around a single question that ordinary UI code gets wrong: the
request expires, the session is cancelled, the person clicks twice, the connection drops
and returns, and ChatyGPT is restarted with a run still in flight.

## Decision

**Arguments are sanitised in Athena, never in the UI.** `sanitise_arguments` summarises any
string over 200 characters into `{preview, chars, truncated}` and passes the whole map
through `redact_sensitive`. The runtime holds the original value, so it is the only place
that can decide what is safe to show; a client that received the raw payload and trimmed it
locally would already have leaked it. The client renders what arrived and reconstructs
nothing.

**The approval event is redacted explicitly.** It is published straight to subscribers
rather than through `EventBus.publish`, which made it the one payload in the system that
never met a redactor — and the one most likely to carry a tool's arguments.

**The projection shows what a judgement needs**: tool and operation, action, risk, tier,
reason, resources, workspace, sanitised arguments, possible effects, and the read-only and
destructive flags. Every field comes from `PermissionRequest`; the client derives none of
them.

**Still no standing grant.** Approve once and Reject, as ADR-009 required and as the UI now
says out loud. A blanket "always allow" would move the decision from the person to a
setting they made in a different mood about a different action.

**A withdrawn question is removed, not disabled.** Cancelling, completing or failing a run
clears its pending approvals in the projection, because answering them would do nothing and
leaving them on screen invites the attempt.

**The client retires a request the moment it answers**, before the round trip returns. The
service is single-use and replies 409 `already_resolved` to a replay, but the second click
happens in the gap, and it should never leave.

**Two failures get their own typed errors.** `AthenaRequestGone` (404 on an approval route)
and `AthenaAlreadyResolved` (409) both mean "you were late, nothing happened" — not the
generic conflict or not-found that would alarm someone over a non-event.

**Every decision is audited**, including the ones the service refuses. What the record
answers is who decided what; whether the clock allowed it is a separate fact, kept in
`outcome`. Auditing only the successes would leave gaps precisely in the strange cases.

**ChatyGPT persists the run reference, not the run.** A new `athena_runs` table stores the
run id, objective, workspace and last seen phase, so the area can re-attach after a restart.
Athena remains the source of truth: on startup the client asks the runtime what those runs
are now, closes the ones it no longer knows, and adopts the rest.

## Consequences

The event stream carries more per approval — bounded, since long values are replaced by
their size. `audit_events` gains five types, all presented through the existing viewer.

`athena_runs` is a local index that can disagree with the runtime; the reconciliation on
startup is what keeps it honest, and closing is idempotent so the polling UI does not write
an audit entry per tick.

A person who leaves a request unanswered still gets ADR-016's outcome: denial, and after
three, an abandoned run. The UI now says so instead of offering a button that would fail.

## Alternatives rejected

**Sanitising in the Rust client.** The raw payload would already have crossed the wire.

**A remembered "always allow this tool".** Rejected under ADR-009. It converts a judgement
about one action into a policy about a class, which is the permission engine's job and is
already expressed by the capability tiers.

**Persisting the projection so a restart loses nothing.** That would make ChatyGPT a second
store of agent state, which ADR-017 exists to prevent. Re-attaching costs one request and
cannot go stale.
