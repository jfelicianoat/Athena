# Athena V0.2 — Hierarchical Execution

- Date: 2026-08-20
- Baseline: V0.1 (PASS 15/15), which remains accepted and unchanged in behaviour
- Verdict: **PASS** — the twelve phases of the integration report are closed
- Gates: 665 tests, ruff, ruff format, mypy `--strict`, all green

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
| 22 | Parallel writers are integrated, not just detected | PASS | `git apply --check --3way` before anything is touched; conflicts reported, clean patches applied, result verified |
| 23 | A plan survives a restart | PASS | `SqliteGraphStore`; a task that was running comes back `RECOVERY_PENDING` and never resolves itself |
| 24 | Decomposition evidence is derived, not supplied | PASS | `RepositoryScout` measures paths, subsystems, file size and the project's own checks — and names what it could not establish |

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

Seven, all real, all fixed:

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
6. **Patches were written as text on Windows.** `Path.write_text` translates newlines, so
   a diff whose lines already ended `
` came back out as `
` and git rejected it
   with "patch does not apply" — indistinguishable from a real conflict, which is what made
   it worth finding.
7. **`subagents_spawned` never counted on a parent run.** A delegate's events carry the
   child's session id, which is the correct attribution — so the run-level rate is computed
   from delegation instead.

## Known limitations

- **Integration replays patches; it does not rebase or resolve.** Conflicting work is
  reported intact and left for a person or a replan. Resolving a conflict is a judgement
  about intent, and git is the wrong place to look for one.
- **Two signals still cannot be derived.** `RepositoryScout` establishes four of the six
  and names the two it cannot: whether outputs genuinely depend on each other, and whether
  the work needs more than one specialism. Both are claims about intent that a filesystem
  does not contain, so they keep their neutral value and are reported as assumed rather
  than filled in.
- **Diagnosis is pattern-based and openly imperfect.** Unrecognised output routes to the
  same undirected repair as before rather than to a confident wrong answer.
- **A recovered plan needs a decision, and nothing makes it automatically.** The store
  brings the graph back and marks what was running as `RECOVERY_PENDING`; deciding whether
  to re-run, skip or fail each of those is a person's call, deliberately.
- **`PlanBoard` is in-memory and bounded.** A plan older than the last thirty-two is gone.
- **Acceptance uses a scripted provider.** Deliberately: a real model would make these tests
  measure whether a small LLM had a good day. The real-provider evidence is below instead.
- **AI_Broker cannot drive Athena's agentic loop, and should not.** Its task API returns
  text; tool calling is offered through `execution.agent.client_tools`, where the *broker*
  runs the iteration loop with its own `max_iterations` and asks the client to execute
  tools. Adopting that would put a second agent loop above Athena's, which ADR-001 exists
  to prevent. `AiBrokerModelProvider` therefore declares `tool_calls=False` and is useful
  for completions that do not need tools; the agentic path uses an OpenAI-compatible
  endpoint directly.
- **Model-level counters land on the child session.** `model_calls` and `tool_calls` are
  zero on a hierarchical parent run for the same reason `subagents_spawned` is: a
  delegate's events belong to the delegate. Run-level totals across a graph would need the
  collector to walk the parent chain, which it cannot do from an event alone.

## Real-provider evidence

A hierarchical run, on a repository whose test genuinely fails, against
`qwen3.8:27b` served over an OpenAI-compatible endpoint:

```
Antes:  def add(a, b): return a - b

Plan: 0 de 2 tareas hechas
○ T01 [explorer] — di por qué falla test_add
  ○ T02 [coder] — corrige add en calc.py

T01 [explorer] completed
   "En calc.py, la función add hace return a - b (resta), no return a + b.
    Por eso test_add falla: add(1, 2) devuelve -1 en lugar de 3."
T02 [coder] completed
   "The function add(a, b) was computing a subtraction… I used edit_file"
   ficheros: ('calc.py',)

Plan: 2 de 2 tareas hechas
Después: def add(a, b): return a + b
Resultado del objetivo: completed
Verificación: passed — All project checks pass.
```

The Explorer diagnosed by reading, the Coder fixed it with `edit_file`, and the verdict
came from running the project's own pytest — not from either of them saying so.

`AiBrokerModelProvider` was separately verified against the live broker: a completion
returned `listo` from `essentialai/rnj-1`, with the broker choosing the model.

## Gates

```
pytest -q                  665 passed
ruff check .               All checks passed
ruff format --check .      139 files already formatted
mypy --strict src tests    no issues in 109 source files
```

ChatyGPT, separately: 257 Rust tests, clippy `-D warnings` clean with no module-wide
`allow`, 225 vitest, `tsc -b` and `vite build` clean.
