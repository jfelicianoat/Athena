# ADR-027: "Not verified" is not "verified wrong"

- Status: **Accepted** — implemented 2026-08-22 in `athena.errors`, `athena.recovery`, `athena.agent_loop`, `athena.graph_executor`, `athena.adapters.service.orchestration`; the classification it consumes was already in `athena.diagnosis`
- Date: 2026-08-22
- Extends: ADR-012 (verification owns completion and recovery is explicit)
- Affects: what a run reports when it did not complete, and what a client can say about it

## Context

`InconclusiveReason` and `inconclusive_reason()` had been in `athena.diagnosis` for some
time, with a docstring stating the distinction exactly:

> A run whose checks could not execute has not failed verification — it has failed to
> verify, and reporting the first as though it were the second blames a change for a
> broken machine.

**Nothing called them.** One test referenced them; no runtime path did. Meanwhile every
run whose verification could not conclude ended with `error_code: verification_failure`,
which says the change is wrong.

Real runs against the broker made it concrete. A hierarchical run where the delegate found
the bug, fixed it, and its task completed ended as:

```
agent.failed  {"error_code": "graph_incomplete", "message": "The plan did not finish"}
```

Two false statements in one event. The plan *did* finish — every task completed. And what
went wrong had nothing to do with the plan: the fixture defined no command the policy would
run, so nothing could be checked. A person following that message would go looking for the
failure in the only part of the run that worked.

## Decision

**Three endings, named apart, where there was one.**

| What happened | Error code | Carries |
| --- | --- | --- |
| A task failed | that task's own code | the task's summary |
| Everything finished, verification said it is wrong | `verification_failure` | the evidence, and a repair cycle |
| Everything finished, nothing could be checked | `verification_inconclusive` | an `InconclusiveReason` |

`VerificationInconclusive` **deliberately does not inherit from `VerificationFailure`**.
Inheritance would make `RecoveryPolicy` treat them identically without anyone choosing
that: a verification failure is answered by returning evidence for a repair cycle, and
here there is no evidence to return. Spending repair cycles against a missing package or a
project with no checks is how a run exhausts its budget looking busy. Its recovery action
is `STOP`, stated explicitly, as ADR-012 requires of every typed error.

The reason travels as **data, in `details` and in the event payload** — not inside a
sentence. Nothing that counts can read a sentence, and a client that had to regex a message
to tell "your change is broken" from "I could not check" would get it wrong on the first
rewording.

`verification.completed` now always carries `inconclusive_reason`, `null` when it does not
apply. Always present rather than sometimes: a field that appears and disappears makes
every client distinguish absent from empty, and nobody gets that right twice running.

### What this does not change

A run that could not be verified still does not complete. ADR-012 stands: completion needs
evidence, and an absence of evidence is not a pass. The change is in what is *reported*,
not in what is *allowed* — the alternative, treating "no checks defined" as good enough,
would make every project without checks a project where Athena always succeeds.

The `_ending()` function in the orchestration adapter is deliberately pure and separate
from the coroutine that publishes: which of three endings this was is the whole decision,
and it should be testable without standing up a graph run.

## Consequences

- The mapping from `FailureKind` to `InconclusiveReason` is pinned by a test that asserts
  over the **complete** enum, so a new failure kind cannot enter without someone deciding
  which side of the line it falls on. That decision is who gets blamed when something does
  not pass; leaving it implicit is how it would drift.
- Verified against the broker on both paths — the loop and the graph. The durable log from
  ADR-025 made the before and after readable in a single query, which is the first time
  that log paid for itself on a question it was not built for.
- ChatyGPT can now distinguish the two cases. Nothing forces it to yet: unknown fields are
  ignored by contract (ADR-024), so `reason` is available whenever the client wants it.
- `graph_incomplete` survives for the case it actually describes — a plan that stopped
  before the end — and no longer stands in for endings it never meant.
