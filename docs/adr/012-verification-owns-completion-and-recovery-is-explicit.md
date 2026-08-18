# ADR-012: Verification owns completion, and recovery is explicit per error

- Status: Accepted
- Date: 2026-08-18

## Context

ADR-006 established that completion requires verification, but H0 and H1 could only prove
that the loop reached a terminal response — that the model stopped talking, not that the
work was correct. Meanwhile every failure path was handled ad hoc inside the loop: model
retries were a hardcoded counter, tool errors were uniformly reported back, and nothing
distinguished an error worth retrying from one that should abort.

Two further problems appear as soon as an agent can change files. A repository that was
already failing would make every run look like Athena's fault. And the cheapest way to make
a red suite green is to delete the test.

## Decision

**Completion is a verdict, not a claim.** `CommandVerificationPolicy` runs the checks the
project itself declares, and a run completes only when `permits_completion` holds — a
`passed` status *and* non-empty evidence. `INCONCLUSIVE` never completes automatically.

**Verification commands are discovered, never invented.** They come from explicit
configuration, an `AGENTS.md` `## Verification` section, or the project's own config, and
every candidate is classified by `CommandPolicy`. Only plain local execution (R2) survives,
so an instruction file is untrusted input rather than an escape hatch.

**Blame is attributed against a baseline.** The plan runs once before the agent starts. A
check that was already failing and still fails is `pre_existing` and does not fail the run;
one that was passing and now fails is `introduced`. With no baseline, a failure is
`unattributed` and fails verification — the safe direction.

**Weakening the checks is not a repair.** `ChangeIntegrityPolicy` inspects the diff for
deleted tests, added skips, net-removed assertions and added suppressions, and fails
verification unless explicitly authorized. Counting is net, so a rename is not a deletion.

**Failure is fed back, not swallowed.** A failed verification starts a bounded repair
cycle: the evidence digest goes to the model, which tries again. After `max_repair_cycles`
the run fails with a diagnosis.

**Every typed error maps to exactly one `RecoveryDirective`.** There is no
`except Exception: retry`; an unclassified error aborts.

To carry this, `VerificationPolicy.verify` now takes the workspace as well as the session
state. This is a deliberate evolution of the H0 contract: verification is inherently about
a workspace, and threading it through the session's attributes would have meant smuggling a
non-serialisable object through structured state.

## Consequences

Athena can be wrong and recover, which is what makes it an agent rather than a generator.
The costs are real: a baseline run doubles the check time on the first pass; integrity
detection is textual and needs git; and a project with no runnable checks cannot complete a
run at all. Each of those is a deliberate trade in favour of not lying about correctness.
