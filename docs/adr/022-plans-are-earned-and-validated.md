# ADR-022: A plan has to be earned, and then it has to survive validation

- Status: **Accepted** — implemented 2026-08-20 in `athena.planning`
- Date: 2026-08-20
- Affects: Athena core (new layer); `AgentLoop` unchanged

## Context

Athena executes one objective at a time on `AgentLoop`, and for most objectives that is the
right shape. Some are not: several independently checkable outputs, real dependencies
between them, different specialisms, several subsystems. Those benefit from being divided
before being attempted.

The obvious way to add this is the wrong way. A planning layer that owns execution replaces
the working part of the runtime with the new part, and every objective — including "fix the
failing test in calc.py" — starts paying for machinery it does not use.

There is a second hazard, specific to asking a model for structure. A model asked for a task
graph will produce one containing a cycle, a dependency on a task it forgot to emit, two
tasks sharing an id, or eleven microtasks where two would do. Not occasionally: these are
the normal failure modes, and every one of them produces a plan that looks fine until
something tries to run it.

## Decision

**The loop still executes. The graph only plans.** `athena.planning` builds and validates;
it starts nothing and runs nothing. `AgentLoop` is untouched by this change.

**The first question is whether to plan, and the default answer is no.**
`DecompositionPolicy` applies the six criteria deterministically and needs two gates to
pass. The one that carries the weight is the verifiable-outputs gate: with a single output
there is a single thing to check at the end, so a graph adds hand-offs between steps that
were never independent and a second place for state to be wrong, in exchange for nothing.
Risk does not override it — risk is a reason to verify harder, not to split. Splitting one
output into three tasks produces three ways to be told it went fine and still one thing to
check.

**The decision costs no model call.** Asking a model whether something needs a plan is most
of the cost of planning it, and the answer is usually no. `Planner.plan` returns `None` —
"run this as it is" — before touching the provider.

**There is no unchecked path to a graph.** `TaskGraph.build` is the only constructor, and
model-written and hand-written plans go through it identically. A parser that assembled a
graph its own way would be a second definition of "valid", and the model's plan is exactly
the one that would find the difference between them.

**Validation refuses whole plans rather than repairing them.** Duplicate ids, unknown
dependencies, unknown parents, cycles, depth, breadth and size are each their own typed
error. Cycles name the tasks in the loop, because "there is a cycle" sends a person reading
the entire plan.

**A leaf must be checkable.** No acceptance criteria means the task will be reported
finished on the model's word, which is what ADR-012 exists to refuse. Interior nodes are
headings and are exempt; requiring criteria of them would make every plan one level deep.

**Single children are collapsed, not tolerated.** A parent with exactly one child records no
decision — nothing was divided, so there is nothing to conquer. Left in, they accumulate
with every replan until the graph is mostly structure. The child inherits the parent's
position, dependencies and inputs, so everything that pointed at the parent still resolves.

**Redundancy is caught where it is decidable.** Two sibling leaves promising the same output
are one task written twice, and that is checkable. "A step so small that naming it costs
more than doing it" is not decidable from the text, and a guess at it would reject good
plans as confidently as bad ones — so that half is not attempted.

**Status transitions are a closed table.** Nothing reaches `COMPLETED` except from
`RUNNING`: a path into it that skips the work is a path to a plan that lies. An attempt is
counted on entering `RUNNING`, not on finishing, because a task that crashed still spent
one — counting only successes lets a failing task loop forever under a budget it never
appears to touch.

**A failure blocks its dependents transitively.** Leaving them `PENDING` would have
`ready()` silently never return them, with no stated reason.

**Replanning is scoped to the affected subgraph.** `affected_subgraph` is the failed task
plus what was going to consume its output — not the sibling that succeeded on another
subsystem, not the dependency that produced what it promised. A replan may not reach outside
it, and replacements arrive `PENDING` with no verification: carrying a status across from
the plan that failed would let a rewritten task inherit a result it never earned.

**Nothing here imports a provider.** `Planner` holds the `ModelProvider` port and never
learns what is behind it; a deployment with no model can still build, validate and reason
about a graph it was handed. A test reads the imports rather than trusting the claim.

**An unrecognised role is refused, not defaulted.** Quietly turning an invented specialism
into "coder" would hand a write-capable toolset to a task the plan meant to be read-only.

## Consequences

`PlanStatus` is separate from `TaskState` in `athena.tasks`. They look similar and mean
different things: one describes a position in a plan, the other a running process with
`killed` and `recovery_pending`. Merging them would lose the distinction between "the plan
says this cannot start yet" and "the process was killed", which is the argument `tasks.py`
already makes for its own seven states.

`ready()` is where fan-out and fan-in come from, rather than being separate features.
Several tasks depending on one completed task become ready together; a task with several
dependencies appears once all of them are done. A caller free to run the ready set
concurrently gets parallelism without this layer knowing what concurrency is.

Writing the tests found a case where two rules meet: a chain of single children under a
tight depth limit. The collapse runs first and wins, which is right — a plan that is deep
only because nothing was divided is over-nested, not too deep. The first version of that
test asserted the limit would fire and was wrong about which rule should apply.

## Not implemented

**Execution of a graph.** Nothing here submits tasks to `TaskManager` or spawns subagents.
That is the next decision and a real one — it has to answer what a task's workspace is, how
a subagent's evidence becomes a parent's verification, and what cancelling a subgraph means.
Building it now, on the grounds that the model anticipates it, is what this ADR's own
`max_depth` exists to discourage in plans.

## Alternatives rejected

**Letting the model decide whether to decompose.** It is the party with an interest in
saying yes, and the criteria are checkable without it.

**Repairing an invalid plan instead of rejecting it.** A repaired plan is one nobody
designed: the model did not write it and no person approved it.

**One status enum shared with `athena.tasks`.** Convenient, and it would erase the
distinction that makes either enum worth having.
