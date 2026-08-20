# ADR-016: Parallelism is earned, not assumed

- Status: Accepted
- Date: 2026-08-19

## Context

Long tasks and parallel work are where a runtime stops being a loop and starts being a
system. Both bring the same three questions: what may run at once, what happens to a
process when its owner goes away, and how to undo something that went wrong.

The tempting answer to the first is "different tools, so go ahead". It is wrong. Two
different tools can be writing the same file, and a read scheduled after a write is not
independent of it. Tool identity is not evidence of safety.

## Decision

**Concurrency is granted by the tool's own declaration, then by resources.** A pair of
calls may share a wave only when *both* return true from `is_concurrency_safe()` for those
exact arguments, and — if either writes — their resources do not overlap. Two writes to
different files are still serialised, because both tools said they were not safe to run
alongside anything and a pair of distinct paths is not a stronger claim than that. A
command or a git mutation takes the whole workspace exclusively, since its arguments do not
bound its effects. A predicate that raises is read as "unsafe": a broken tool must not be
able to widen concurrency. Order is preserved throughout, so a call never overtakes one it
conflicts with.

**Seven task states, because collapsing them destroys information.** A task that was
*killed* is not one that *failed*, and one the runtime lost when the process died is
neither — that is `recovery_pending`, mirroring H4's rule that the unknown case resolves
towards "needs a human", never "finished".

**Budgets cover every dimension a long task can run away in**: iterations, tool calls,
wall clock, and — only where the provider actually reports them — tokens and cost. A limit
that cannot be measured is left unset rather than pretended.

**Cancellation is hierarchical and processes do not outlive their owners.** A child task's
token is chained to its parent's, so cancelling a parent cancels the subtree. A background
process registered against a task is killed as a *tree* when that task ends, and a
supervisor starting cold can tell a process that exited from one that vanished.

**Checkpoints are copies, never commits.** A checkpoint copies the files an operation is
about to touch to a directory outside the workspace. Athena does not write to a user's git
history to protect itself: a commit is a public act with a message, an author and
consequences for everyone on the branch, and taking one "just in case" would mean the
safety net changes the thing it protects. Restoring is explicit, because an automatic
rollback would discard work a human might have wanted to see.

**Workspace isolation is an abstraction with one implementation.** `SharedWorkspaceStrategy`
is the only strategy built, and it is adequate *because* conflicting writes are serialised.
Worktree, container and remote strategies exist as declared kinds that refuse clearly when
asked — a strategy that silently fell back to the shared workspace would be worse than a
missing one, because a caller asking for isolation would believe it had some.

**Worktrees stay unbuilt until parallel writing tasks demonstrably need them.** A worktree
per task buys isolation and costs a checkout, a merge, and a second copy of every build
artefact. Writes are serialised today, so the isolation buys nothing and the cost is real.
The trigger to revisit is evidence that two write tasks genuinely must run at once — not
the observation that worktrees exist.

## Consequences

Parallelism is narrow on purpose: independent reads go together and almost nothing else
does. That leaves throughput on the table, and it is the right trade, because a wrong
parallel decision corrupts a repository while a wrong serial decision only costs time.

The unimplemented isolation strategies are honest placeholders rather than dead code: each
records the specific condition that would justify building it.
