# Athena V0.2 — Hierarchical Execution

- Date: 2026-08-20
- Baseline: V0.1 (PASS 15/15), which remains accepted and unchanged in behaviour
- Verdict: **PASS** — the twelve phases of the integration report are closed
- Gates: 614 tests, ruff, ruff format, mypy `--strict`, all green

## What changed, in one sentence

V0.1 was a monoagent runtime with four advanced subsystems sitting beside it, built and
tested and used by nothing. V0.2 is those subsystems inside the execution path.

## Definition of done

| # | Capability | Status | Evidence |
|---|------------|--------|----------|
| 1 | The V0.1 loop still works | PASS | Every H0–H7 suite unchanged and green |
| 2 | A simple goal avoids the graph | PASS | `DecompositionPolicy` says no by default; no model call is spent asking |
| 3 | A complex goal produces a validated DAG | PASS | `TaskGraph.build` is the only constructor; cycles, unknown deps, duplicate ids and limits all refuse |
| 4 | The graph actually executes | PASS | `GraphExecutor` drives `ready()` → `TaskManager` → `SubagentRunner` → `AgentLoop` |
| 5 | Explorer, Coder and Verifier are really used | PASS | Acceptance run observes the roles the plan named, in order |
| 6 | Fan-out and fan-in | PASS | Readers overlap (`peak_in_flight > 1`); a join waits for every dependency |
| 7 | Writers do not overlap | PASS | `peak_in_flight == 1` for two coders on a shared workspace |
| 8 | Child permissions ⊆ parent permissions | PASS | `narrow` is an intersection, checked in all six combinations |
| 9 | Delegation is permissioned | PASS | Tier follows the delegate's authority; a read-only explorer is R0, a coder is ASK |
| 10 | Cancellation reaches every level | PASS | Run → graph → task → subagent → loop → provider, no orphaned tasks or processes |
| 11 | Cancelled is not failed | PASS | `ExecutionOutcome` separates them; one classifier, no `isinstance` pairs |
| 12 | Task verification ≠ goal verification | PASS | Every task passes and the goal fails, on a repository whose tests really fail |
| 13 | Failures are diagnosed before repair | PASS | `FailureDiagnosis` on real pytest output; a missing package stops the cycle |
| 14 | `INCONCLUSIVE` is reachable beyond an empty plan | PASS | `InconclusiveReason` distinguishes missing dependency, broken machine, unrunnable tool |
| 15 | Provider fallback has a consumer | PASS | `ProviderRouter` is itself a `ModelProvider`; the loop never learns it exists |
| 16 | Memory persists and stays honest | PASS | `SqliteProjectMemory`; everything enters `PROPOSED`, supersede rather than overwrite |
| 17 | Runs can be measured and compared | PASS | `MetricsCollector` from the event bus; monoagent vs hierarchical is one query |
| 18 | Parallel writers can be isolated | PASS | Worktree per writer, no automatic merge, overlaps reported |
| 19 | Work can be undone, attributably | PASS | `RollbackLedger` restores only files this run wrote and reports what it declined |
| 20 | ChatyGPT resumes rather than resynchronises | PASS | `Last-Event-ID` sent and observed; plan drawn as a graph |
| 21 | Telegram sees the plan without being flooded | PASS | Graph events relayed, task events aggregated, `/tasks` renders the plan |

## What Athena is still structurally unable to do

Everything V0.1 listed remains true — it cannot declare its own work correct, cheat its way
to green, invent a verification command, escalate its own permissions, act irreversibly, or
retry blindly. V0.2 adds four:

- **A subagent cannot exceed its parent.** `narrow` computes the intersection; there is no
  code path that widens a policy, and a delegate cannot be handed a tool its parent's
  registry never held.
- **A plan cannot bypass validation.** Model-written and hand-written graphs go through one
  constructor. A plan that fails is rejected whole rather than repaired into something
  nobody designed.
- **A rollback cannot touch what Athena did not write.** Attribution is recorded as the
  writes happen, never inferred from a diff.
- **A merge cannot happen by default.** Isolated writers produce diffs and a report of
  which files overlap. Integrating them is a task with its own verification.

## Defects found by writing this

Six, all real, all fixed:

1. **A cooperative task was recorded as `FAILED`.** A body that stopped when asked raised
   `CancellationError`, which the typed-error branch filed as a failure — punishing the task
   for doing what it was told.
2. **`TaskManager.cancel` left uncooperative bodies in `running` for ever.** Asking is now
   followed by a bounded wait and then force, and the two endings stay distinguishable.
3. **`Response.body` vs `payload`.** The idempotency path read a field that does not exist,
   so every request created a new run. A "cannot happen" guard was swallowing it; the guard
   was the bug.
4. **`agent.failed` payload keys.** The channel renderer read `error` and `code`; the
   runtime publishes `error_code` and `message`. The most important message of a failed run
   rendered as "no detail given".
5. **Duplicate subagent events.** `GraphExecutor` published `SUBAGENT_*` that `SubagentRunner`
   already published. The graph level got its own vocabulary instead.
6. **`subagents_spawned` never counted on a parent run.** A delegate's events carry the
   child's session id, which is the correct attribution — so the run-level rate is computed
   from delegation instead.

## Known limitations

- **Integration of parallel writers is detection only.** Overlapping files are reported; no
  `IntegrationTask` runs the merge and verifies it. That is the next decision, not a gap in
  what is claimed here.
- **The planner's decomposition signals are supplied by the caller.** Nothing yet derives
  them from a repository automatically, so "does this goal need a plan" is answered with
  evidence somebody else gathered.
- **Diagnosis is pattern-based and openly imperfect.** Unrecognised output routes to the
  same undirected repair as before rather than to a confident wrong answer.
- **The graph is not persisted.** A restart loses the plan; the session store still holds
  the run. Recovering a half-executed graph is unimplemented.
- **`PlanBoard` is in-memory and bounded.** A plan older than the last thirty-two is gone.
- **Acceptance uses a scripted provider.** Deliberately: a real model would make these tests
  measure whether a small LLM had a good day. V0.1's real-provider run against
  `granite4.1:3b` stands as the evidence that the port works.

## Gates

```
pytest -q                  614 passed
ruff check .               All checks passed
ruff format --check .      129 files already formatted
mypy --strict src tests    no issues in 102 source files
```

ChatyGPT, separately: 257 Rust tests, clippy `-D warnings` clean with no module-wide
`allow`, 225 vitest, `tsc -b` and `vite build` clean.
