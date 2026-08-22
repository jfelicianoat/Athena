# ADR-015: Three delegates, not a swarm

- Status: Accepted
- Date: 2026-08-19
- Implementation status (2026-08-22): the `SubagentProfile` decided here is a **role**
  profile — it divides authority inside a run. ADR-028 adds `AthenaProfile`, which
  says what kind of work the whole run is. The word collides and the concepts do not:
  a documents run still delegates to a Coder. The two compose; they do not merge.

## Context

ADR-010 reserved subagents behind isolated contexts and finite budgets, and left the
implementation out. The temptation when finally building it is to write a general
delegation mechanism: any agent may spawn any agent for any reason. That produces a system
whose cost and behaviour nobody can predict, because the thing deciding how many agents to
create is the same thing that is bad at estimating how much work is left.

## Decision

**Task first, agent second.** Delegation exists because a piece of work has a recognisable
shape, not because spawning is available. So there are exactly three profiles and no
mechanism to define a fourth at runtime:

| Profile | May | May not |
| --- | --- | --- |
| Explorer | read, search, read git history | write anything, run anything |
| Coder | read, edit, write, run local checks, see the diff | commit, crawl history |
| Verifier | read, run checks, see the diff | edit or write, ever |

**Authority is structural before it is policy.** `SubagentProfile.registry_for()` builds a
registry containing *only* that profile's toolset. An Explorer asking to write does not get
refused by a correctly-configured policy; it gets an unknown-tool error, because the tool
was never there. The permission policy is the second line, not the first.

**Isolation is the default, not an option.** A delegate receives a brief — objective,
acceptance criteria, relevant files, the previous step's findings, constraints — and
nothing else. Not the parent's conversation, not its working memory, not its session store,
and not project memory. It gets a fresh session id and an empty transcript.

**Delegates cannot delegate.** No profile carries a delegation tool. Recursion is how a
bounded task becomes an unbounded bill.

**Every delegate carries its own limits.** Iterations, tool calls and timeout come from the
profile and can be tightened per call. A child's cancellation token is chained to the
parent's, so cancelling the parent cancels the children.

**A delegate proves it answered; the parent decides what the answer is worth.** Children
use the light completion policy rather than running the project's checks, because verifying
the same repository once per child would triple the cost of one task. The Verifier *runs*
the checks as its job and reports what it saw; the parent's own `VerificationPolicy` still
owns completion.

**An unattended delegate that meets an ASK stops.** The default prompt denies, because a
child with nobody watching must not guess at a human's answer.

## Consequences

Delegation is predictable: three shapes, fixed authority, fixed limits. The cost is that
work which fits none of the three shapes is not delegated at all — the parent does it. That
is the intended trade, and adding a fourth profile should require this file to change.

## Postscript: a defect this work surfaced

The first Explorer test failed for the right reason and the wrong cause. Writing was
correctly refused, yet `working_state.files_modified` still listed the file: the runtime
recorded tool use *before* execution, so it recorded intent rather than fact. That state
feeds verification's attribution, recovery, and whatever a human reads after a crash — all
of which would have been told a file changed when it had not. Tool use is now recorded only
after a call succeeds, with a regression test for a refused write.
