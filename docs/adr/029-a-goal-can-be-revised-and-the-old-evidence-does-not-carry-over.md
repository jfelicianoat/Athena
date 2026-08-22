# ADR-029: A goal can be revised, and the old evidence does not carry over

- Status: **Accepted** — implemented 2026-08-22 in `athena.goals`, `athena.errors`, `athena.events`, `athena.agent_loop`, `athena.run_event_log`, `athena.adapters.service.runs`, `athena.adapters.service.server`
- Date: 2026-08-22
- Affects: what a client may change about a run in flight, and what stops being true when it does

## Context

The objective was a `str`, passed once and never looked at again. That is right while a run
lasts seconds. It stops being right the moment a run lasts minutes and the person watching
realises they asked for the wrong thing — at which point the only option was to cancel and
start over, throwing away everything already found out.

## Decision

**A goal has revisions, each written against a stated base, and a revision invalidates what
was proven under the previous one.**

Three separate problems, kept separate on purpose:

### 1. Who wins when two people revise at once

Every revision names the revision it is written against. If the goal has moved on, it is
refused with **409 and the current goal in the body**, so whoever arrived late decides with
it in front of them rather than having to ask again.

Not merged, and not overwritten. Merging two commissions written in prose is something
nobody knows how to do, and overwriting turns somebody else's work into a change they never
saw. `base_revision` is **required and has no default**: an implicit "the latest" would make
every revision a silent stomp, which is exactly what the number exists to prevent.

A revision whose text is identical to the current one is not a revision. Creating one would
stop the loop to be told what it already knew, and would leave a change in the record that
was not one.

### 2. When it applies

**Only between iterations.** A goal that changed with a tool call in flight would leave the
model holding a result asked for by one commission and a question asked by another.

Which means written is not applied, and the API says so: the response carries
`"applied": false`. The loop picks the revision up on its next turn and announces it with
`goal.revised`. Telling the client it is already being worked on would be convenient and
false — the loop can be inside a model call.

The model is told the new goal **and** that the previous one no longer applies. A model
given only the new instruction tends to do both: the old one is still in its transcript and
nothing said it had stopped counting.

### 3. What happens to what was already done

**Evidence obtained under one revision does not prove the next.** A verification that passed
against yesterday's goal says nothing about today's, so it is dropped rather than inherited.
Inheriting it would be the cheapest possible way to sign off work nobody asked for.

The working state follows the goal too — it is what verification, recovery and anyone
reading the run afterwards look at, and a state still naming the old objective would have
all three judging the work against something nobody wanted any more.

## Consequences

- `goal.revised` carries the new revision **and the one it supersedes**, text included.
  Without the second, a reader cannot tell what everything earlier in the run was done for.
- It is in `DURABLE`. That was missed at first and a real broker run caught it: the
  revision applied, was published, and left no trace in the log — so the stored history
  described work that did not match the objective it opened with, and said nothing about
  why. A run that ended up doing something else is only explicable if the change is on the
  record.
- A finished run's goal cannot be revised. There is nothing left to redirect, and accepting
  the write would report success for a change that will never take effect.
- The board lives on the run in the service, not inside the loop: whoever revises is
  talking to the service, and the loop may be mid-call when the change lands. One board,
  shared — two copies would be two goals, with the client revising one nobody reads.
- Verified against the real broker: a documents run was redirected mid-flight, the stale
  revision was refused with 409, and the model obeyed the new goal — it wrote the summary
  and skipped the section the revision had cancelled.
- **ChatyGPT is not updated here.** The Rust client has uncommitted work in the user's tree
  and this phase deliberately stops at the Athena boundary. The client needs: a control to
  revise, `base_revision` echoed from the state frame, and a 409 handler that shows the
  current goal instead of a generic error.
