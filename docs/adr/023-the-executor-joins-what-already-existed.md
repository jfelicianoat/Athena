# ADR-023: The executor joins what already existed

- Status: **Accepted** — implemented 2026-08-20 across `athena.cancellation`, `athena.state`, `athena.graph_executor`, `athena.delegation`, `athena.provider_router`, `athena.project_memory`, `athena.metrics`, `athena.diagnosis`, `athena.isolation`, `athena.rollback`
- Date: 2026-08-20
- Affects: Athena core; ChatyGPT and Telegram consume the result

## Context

An audit found the project in an unusual state: not missing pieces, but holding four
complete ones that nothing used. `subagents.py`, `tasks.py`, `planning.py` and the
`ProjectMemory` protocol were built, tested, exported — and absent from every execution
path. `CheckpointStore` and the `provider_fallback` directive were in the same position.

That is worse than an absent feature. A recovery directive nothing consumes reads like a
capability and behaves like nothing; a subsystem nobody calls drifts from the runtime it is
supposed to belong to, and the drift is invisible because its own tests keep passing.

The temptation was to build the missing connections as a new layer that owned execution.
That would have replaced the working part of the runtime with the new part, and made every
goal — including "fix the failing test in calc.py" — pay for machinery it does not use.

## Decision

**The loop still executes; the graph only plans.** `GraphExecutor` contains no agent loop.
A task is a brief handed to `SubagentRunner`, which builds an `AgentLoop` exactly as it
already did. There is one loop implementation in Athena and the executor uses it. A test
reads the imports rather than trusting the claim.

**Cancellation became an outcome before anything else was built.** `ExecutionOutcome` and
`classify_outcome` replaced an `except (CancellationError, ProcessCancelledError)` pair that
appeared in the loop twice and in the recovery policy once — and would have gained a copy
for every new level of the hierarchy. `CancellationSource.child` makes the scope
relationship explicit: a stop travels down and never up.

This came first deliberately. Introducing a five-level hierarchy on top of ambiguous stop
semantics multiplies the ambiguity by five, and the resulting bugs are indistinguishable
from scheduler bugs.

**Reads overlap, writes take a lock, and worktrees came later.** Two coders editing one
checkout produce failures that are painful to reproduce; a lock cannot corrupt anything.
Worktrees ship in the same cycle but after the executor worked on a shared workspace,
because debugging a scheduler and a filesystem-isolation layer simultaneously means never
knowing which one is wrong.

**A task passing is not the goal passing.** Each task proves its own small thing; the goal
is proved once, at the end, against the project's real checks. A runtime that reported
success because every part reported success would be trusting a sum of local claims about a
global property — and the acceptance suite contains exactly that case, on a repository
whose tests really fail.

**A child's authority is the intersection of its own and its parent's.** Computed, not
promised. `delegate_task` lets the model ask for a *task*, never for an agent: `spawn_agent`
would invite it to think about infrastructure, which is not its business.

**Every directive now has a consumer.** `ProviderRouter` is itself a `ModelProvider`, so the
loop never learns it exists. It routes between providers rather than models — AI_Broker
already chooses models behind its endpoint, and duplicating that would give two components
an opinion about one decision.

**Nothing is remembered automatically.** `SqliteProjectMemory` exists, and `propose` is the
only way in; it hard-codes `PROPOSED`. An agent that could write `VERIFIED` would be grading
its own homework. Corrections supersede rather than overwrite, so "we used to think X"
survives for whoever is debugging a wrong decision.

**Measurement is a subscriber and can never fail a run.** Everything the collector knows
comes from events already published, so there is no instrumentation threaded through the
loop to fall out of step.

**A failure is read before anyone is asked to fix it.** `FailureDiagnosis` says which kind of
problem it is and, as importantly, what not to touch. Failures no edit could address stop
the repair cycle instead of spending it. Unrecognised output routes to the old undirected
behaviour rather than to a confident wrong answer.

**A rollback undoes only what this run wrote.** Attribution is recorded as the writes happen,
because a diff cannot tell the agent's edit from the person's, and guessing permissively is
how a rollback eats somebody's afternoon.

## Consequences

Athena is one system. A goal is assessed, decomposed if it earns it, executed through the
existing loop by specialists whose authority is bounded by their parent's, verified against
the project's own checks, and measured — and every one of those steps is reachable from
ChatyGPT and from Telegram.

The runtime gained seven event names at the graph and task level. That is not decoration:
a task uses a subagent and is not one, and a view that conflated them could not draw a plan.

Six defects surfaced during the work, listed in `V0_2_ACCEPTANCE_REPORT.md`. Two are worth
repeating here because they are about method rather than code. The idempotency path read
`Response.body` where the field is `payload`, and a "cannot happen" guard swallowed the
resulting `None` — the guard was the bug, and removing it is what made the test meaningful.
And `subagents_spawned` never counted on a parent run because a delegate's events carry the
child's session id; the attribution was right and the metric was wrong.

Two tests changed meaning rather than mechanism. "The event vocabulary is frozen" now
forbids removal rather than addition, since every milestone since H0 has added names. "There
is no project memory" became "nothing is remembered automatically", which is what H4 was
actually protecting.

## Not implemented

**Integration of parallel writers.** Overlaps are detected and reported; no task merges
them. Two diffs that both apply cleanly can still be wrong together, so the merge needs its
own verification — which is a decision, not a detail.

**Persisting a graph across a restart.** The session store holds the run; the plan is
in memory. Recovering a half-executed graph means deciding what a half-finished task
becomes, and that is the same question `recovery_pending` answers for sessions.

**Deriving decomposition signals automatically.** The policy is deterministic and the
evidence is supplied. Something has to gather it; nothing does yet.

## Alternatives rejected

**A planning layer that owns execution.** It would bet the working part of the runtime on
the new part.

**Worktrees first.** Isolation before a working scheduler makes every scheduler bug look
like a filesystem bug.

**Automatic memory of the model's conclusions.** A memory that grows more confident every
session regardless of whether it was right is worse than no memory.

**A model router inside Athena.** AI_Broker exists and does that.
