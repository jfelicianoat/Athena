# ADR-035: A check that is still red is not a check that passed

- Status: **Accepted** — implemented 2026-08-23 in `athena.verification`, `athena.diagnosis`
- Date: 2026-08-23
- Extends: ADR-006 (completion requires verification) and ADR-027 (not verified is not verified wrong)
- Affects: what Athena is allowed to call a completed run

## Context

`AgentsFileVerificationPolicy` compares every check against a baseline captured before the
run, so a repository that was already red does not make every run fail. That attribution is
right and stays. What it did with the verdict was not:

```
summary = "All project checks pass."
if pre_existing:
    summary += " 1 check(s) were already failing before this change and are unchanged: pytest -q."
return VerificationResult(VerificationStatus.PASSED, evidence, summary)
```

Two sentences, the first false and the second true, with the false one first — and a status
that lets the run complete.

The case that makes it serious is the most common objective there is: *"the tests fail, fix
them."* There the check that measures the work is precisely the one that was red at the
start, so it is excluded from the verdict by construction, and any run at all satisfies it.

Measured on 2026-08-23: given a failing test suite and asked to fix it, `granite4.1:30b`
delegated one invented task about `/data/desert\`s`, the delegate died, the model changed
not a single file, and Athena published `agent.completed` with *"All project checks pass. 1
check(s) were already failing before this change and are unchanged: pytest -q."* Every word
after the first sentence was true. The run was still a lie.

## Decision

**Two separate claims, and neither may be dressed as the other.**

*The sentence.* `PASSED` no longer opens with "All project checks pass" when a check is red.
With work done and no regression, what Athena can honestly say is *"This change broke no
check. 1 check(s) were already failing before this change and are unchanged: pytest -q."*
Both sentences true, and the true one first — an interface reads the first.

*The status.* A run that **modified no file** and leaves checks failing is `INCONCLUSIVE`,
not `PASSED`. With nothing changed there is nothing to attribute: the run broke nothing
because it did nothing, and letting that complete turns verification into a rubber stamp.
This is deliberately narrower than "any red check makes the run inconclusive" — see the
alternatives.

`INCONCLUSIVE` and not `FAILED`, also deliberately: there is no regression to report, and
reporting one sends somebody hunting for a break that does not exist. What happened is that
nothing was proven, which is what ADR-027 built `INCONCLUSIVE` for, and
`VerificationResult.permits_completion` already refuses it.

`FailureKind.PREEXISTING_FAILURE` therefore moves to the inconclusive side of the table in
`diagnosis.py`, mapping to `InconclusiveReason.PARTIAL_VERIFICATION`. That table exists so
that moving a failure kind costs a deliberate edit; this is one. Without the move an
`INCONCLUSIVE` result would publish `inconclusive_reason: null`, the incoherence ADR-027 set
out to remove.

## Consequences

- "Fix the failing tests" stops being the one objective Athena could never check. A run that
  does nothing about it can no longer complete.
- A run that does real work in a repository with an unrelated red check still completes, as
  it did before. Only the wording of its verdict changed.
- The attribution guarantee is untouched: `pre_existing` never counts as a regression, and
  the summary says so.
- A run that legitimately modifies nothing — a pure investigation — now ends `unverified` in
  a red repository. That is correct: it produced no evidence about the checks either.

## Alternatives rejected

**Return `FAILED` when anything is red.** Simple, and it blames a run for a repository it
inherited. It is the symmetric error and sends people looking for a regression that is not
there.

**Make any remaining red check `INCONCLUSIVE`, whatever the run did.** Purer, and it was the
first attempt. It overturns a decision this project made on purpose —
`test_a_pre_existing_failure_does_not_block_completion` — and turns every run in a
permanently red repository into an unverified one, which trains people to ignore the status.
The defect measured was narrower than that: a run that changed *nothing*. The fix is scoped
to the defect.

**Keep `PASSED` and only fix the wording.** The sentence was the symptom. The status is what
`permits_completion` reads, and a run that proves nothing must not be able to finish.

**Compare the objective against the failing check to decide whether it was in scope.**
Requires reading intent out of prose, which is exactly the kind of guess this runtime avoids
elsewhere. The check outcome is a fact; the objective is not one Athena can parse reliably.
