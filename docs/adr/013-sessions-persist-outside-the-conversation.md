# ADR-013: Sessions persist outside the conversation

- Status: Accepted
- Date: 2026-08-18

## Context

Until H4 a session lived entirely in one process. Its knowledge was split between a
`WorkingState` object that vanished when the process ended, and a message list that grew
without bound. Both failure modes follow from the same mistake: treating the transcript as
the database. A long session eventually exceeds any context window, and an interrupted one
loses everything it learned.

## Decision

**Three memory levels, with different lifetimes and different rules.**

- `ConversationContext` is the transcript: disposable, bounded, compactable.
- `WorkingMemory` (the `WorkingState` of ADR-008) is the structured operational state of
  one session: objective, constraints, plan, current step, facts, files examined and
  modified, commands run, decisions, errors, verification, remaining work. Durable and
  validated.
- `ProjectMemory` is knowledge that outlives a session. Only the interface exists.
  Nothing writes to it, because a runtime that silently accumulates cross-session beliefs
  is much harder to reason about than one that does not.

**Compaction is safe because the durable facts were never in the transcript.**
`MicroCompaction` does not summarise with a model. It reduces an already-externalized tool
result to its reference, drops repeated identical tool output, truncates oversized
messages, and always keeps the most recent turns verbatim. Everything the spec requires to
survive lives in `WorkingMemory`, which the context builder re-renders on every request.

**`ContextWindowManager` selects; it never concatenates.** When the window still does not
fit after compaction it falls back to the most recent turns — which is only acceptable
because the working memory carries the rest.

**A session that was live when the process died becomes `recovery_pending`.** Never
`completed`. The runtime does not know whether the work finished, and guessing optimistically
is how an agent claims success it never achieved. `SqliteSessionStore.mark_interrupted()`
runs at startup and performs exactly that transition.

**A damaged row degrades; it does not disappear and it does not raise.** Unreadable working
memory is replaced by a placeholder objective that says so, the record is flagged
`degraded`, and an unrecognised status is read as `recovery_pending`. The safe direction is
always "needs a human", never "finished".

**Checkpoints are written where work happens.** The session is persisted after the tool
calls of each turn, not only at the end of an iteration: a crash between a file edit and
the next model response must not lose the record of that edit.

**Tool-result references state their lifetime.** `SqliteToolResultStore` keeps payloads for
a documented retention window (seven days by default) and raises
`ToolResultUnavailableError` when a reference has expired, is missing, or no longer matches
its checksum. A reference that quietly resolves to nothing is worse than one that fails.

## Consequences

Athena can be restarted and continue: `AgentLoop.resume()` rebuilds a run from stored
working memory alone, with an empty conversation. That is the strongest available evidence
that the transcript was never load-bearing.

The costs: SQLite is now a runtime dependency of any persistent deployment (stdlib, but
still a file to manage); persistence adds a write per turn; and the retention window means
old references eventually stop resolving, which callers must handle. Compaction remains
deliberately unintelligent — it will happily drop context a smarter summariser would have
kept, and the mitigation is that anything worth keeping belongs in working memory.
