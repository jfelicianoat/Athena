# ADR-030: A delegate can be asked again, within its own budget

- Status: **Accepted** — implemented 2026-08-22 in `athena.subagents`, `athena.subagent_provider`, `athena.delegation`, `athena.events`, `athena.run_event_log`, `athena.tools`, `athena.tool_executor`
- Date: 2026-08-22
- Amends: ADR-010 (subagents use isolated contexts and budgets), which assumed one shot
- Extends: ADR-026 (a result has one truth and two projections) — a tool may now declare how long it may take

## Context

ADR-010 assumed a delegate answers once. Cheap to reason about and expensive to use: the
parent gets a report, has an obvious question about it, and the only way to ask was to
delegate again from scratch — paying a second time for everything the child had already
worked out.

`SubagentCapabilities.continuation` had existed as a declared **false** since Phase 2, with
a comment saying it was false *because it did not exist, and claiming it would make the
declaration a wish rather than a fact*. This phase is what makes it a fact.

## Decision

**A delegate can be asked again, keeps its identity, and shares its original budget.**

| | Delegating again | Following up |
| --- | --- | --- |
| Identity | a new session id | **the same one** |
| What it knows | nothing | everything it already reported |
| Budget | a fresh one | **what is left of the first** |
| Bound | the parent's own limits | `max_follow_ups`, and it stops when the run does |

Identity is what makes the durable log (ADR-025) keep telling the truth: with a new name
the record would show two agents where there was one, and the shared budget would not
reconcile with anything visible.

The shared budget is the whole safety property. A delegate that renewed its allowance every
question would be "as many agents as you like, counted as one" — the limit bypassed from
the inside, without anyone changing it. A test asserts it, and it caught exactly that: the
first implementation re-registered the session on each follow-up and silently reset the
counter.

`max_follow_ups` defaults to **two for the Explorer and zero for everyone else**. The
Explorer is the delegate you genuinely want to ask again — "you found X, now tell me Y
about it" — and the only one that changes nothing, so asking twice cannot make the
workspace worse. A continuable Coder would be a second instruction about a change already
made, with no new acceptance criterion; that is what ADR-015 refuses. It should ask for
another delegation, with its own criterion.

Continuation is a **separate protocol** from `Delegator`. Remembering a child between calls
is not something every delegator can do — a remote or stateless one cannot — and putting it
in the main protocol would make everyone declare an ability the ones without it would fail
to honour at call time rather than at declaration time.

## Two things a real run found that no test had

**`delegate_task` could never have worked against a real model.** `ToolExecutor` applied a
single 30-second ceiling to every tool. A delegation starts an entire child loop, so it was
cut every time and reported as the delegate timing out — when what timed out was the
caller's clock. Scripted providers return instantly, so the whole suite was green.

`ToolSpec` now declares `timeout_seconds`. One number for every tool is a policy that
cannot be true for all of them: a file read taking 30 seconds is broken, and a delegation
that only gets 30 seconds never starts. `bash` declares its own too — a command asking for
120 seconds was being killed at 30 by the same ceiling.

**A successful delegation was returning "(no results)" to the model.** The default
projection picks the first list in a result as the thing to enumerate; for `delegate_task`
that is `files_changed`, which for an Explorer is empty. So after a delegation that came
back with findings, the model was handed an empty list. `DelegateTaskTool` now implements
`project()` — which is exactly what that seam is for — and tells the model what the delegate
reported, and how to ask it again while it still can.

The two compounded: the delegation was cut by the clock, and when it did survive, its answer
arrived blank.

## Consequences

- `subagent.continued` is published and is **durable**. Without it the record shows one
  delegate answering two different things with nothing saying it was asked twice.
- The model is told how many follow-ups remain, and only offered the option while it has
  one. A limit you cannot see is a limit you discover by hitting it, at the cost of a call.
- A follow-up cannot restate the role. Asking for it again would invite changing it, which
  is an indirect escalation: the delegate already exists with the authority it was granted,
  and the profile is deliberately not recomputed.
- Sessions live as long as the runner, which lives as long as the run. Nobody has to
  remember to close them, so nobody can forget.
- Verified against the real broker: the model delegated to an Explorer, read what it
  reported, and asked that same delegate a follow-up by id — one session, `follow_up: 1`,
  budget spend carried across.
