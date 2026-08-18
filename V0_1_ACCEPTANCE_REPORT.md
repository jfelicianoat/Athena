# Athena V0.1 — Acceptance Report

- Date: 2026-08-18
- Version: `athena-agent-runtime` 0.1.0
- Verdict: **PASS** (15 of 15 required capabilities)

Athena V0.1 is an autonomous coding-agent runtime that investigates a repository, changes
it under a deterministic permission engine, proves its changes with the project's own
checks, and repairs itself when that proof fails.

## Definition of done

Every capability below was exercised end to end against a real git repository, not
asserted in isolation. The scripted run uses `FakeModelProvider`; the real-provider run
uses `granite4.1:3b` served by Ollama through the OpenAI-compatible adapter.

| # | Capability | Result | Evidence |
| --- | --- | --- | --- |
| 1 | Open a workspace | PASS | Canonical root resolved and enforced as the boundary |
| 2 | Receive an objective | PASS | Objective recorded in structured working state |
| 3 | Explore with Glob/Grep/Read | PASS | `glob`, `grep`, `read_file` all executed |
| 4 | Edit a file | PASS | `edit_file` applied an atomic, diffed change |
| 5 | PermissionEngine active | PASS | 2 ASK prompts raised and resolved per call |
| 6 | Run verification | PASS | 3 check executions (1 baseline + 2 verification runs) |
| 7 | Detect an incorrect change | PASS | `verification.failed` emitted, failure attributed as `introduced` |
| 8 | Self-repair | PASS | `recovery.action` = `return_evidence`; file restored to a correct state |
| 9 | Verify again | PASS | Second verification returned `passed` |
| 10 | Obtain a git diff | PASS | `git_diff` executed as an R0 read |
| 11 | Complete with evidence | PASS | Completion carried a non-empty `VerificationResult` |
| 12 | Support cancellation | PASS | Cancel mid-inference ended the run as `cancelled` |
| 13 | Work with FakeProvider | PASS | Full scripted run completed |
| 14 | Work with a real provider | PASS | `granite4.1:3b` completed a verified run, loop code unchanged |
| 15 | Keep large outputs out of context | PASS | 40 000-char payload externalized; longest inline fragment 460 chars, delivered as `athena-result://` reference |

## The self-correction cycle, in full

The scripted run deliberately makes Athena wrong before it is right:

1. It explores the repository (`glob` → `grep` → `read_file`).
2. It edits `calc.py`, changing `a + b` into `a - b`.
3. It declares the work finished.
4. `CommandVerificationPolicy` runs the command the repository itself declares in its
   `AGENTS.md` `## Verification` section, and the test suite fails.
5. The failure is compared against the baseline captured before any change: the check was
   passing before, so it is attributed as **introduced**, not blamed on the repository.
6. `verification.failed` and `recovery.action` are emitted; a bounded evidence digest goes
   back to the model with an explicit instruction not to weaken any check.
7. The model repairs the edit, requests `git_diff`, and finishes again.
8. Verification passes, and only then is the run allowed to complete.

## What Athena is structurally unable to do

- **Declare its own work correct.** Completion requires
  `VerificationResult.permits_completion`, which demands both a `passed` status and
  non-empty evidence. A model that says "done" with no plan gets `inconclusive`, and
  `inconclusive` never completes.
- **Cheat its way to green.** `ChangeIntegrityPolicy` inspects the diff for deleted tests,
  added skips or `xfail`s, net-removed assertions, and added lint or type suppressions.
  Any of these fails verification unless explicitly authorized. Net counting means a
  rename or refactor is not mistaken for a deletion.
- **Invent a verification command.** Commands come only from explicit configuration,
  `AGENTS.md`, or the project's own config, and each candidate is classified by
  `CommandPolicy`. Anything that is not plain local execution (R2) is discarded, so an
  instruction file cannot smuggle in `curl … | sh`.
- **Escalate its own permissions.** The `PermissionEngine` owns ALLOW / ASK / DENY. R4 is
  never even offered to a human. Approval is single-use.
- **Act remotely or irreversibly.** There is no push, pull, merge, rebase, publish or
  deploy tool, and those commands are classified R4.
- **Retry blindly.** There is no `except Exception: retry`. Every typed error maps to one
  explicit `RecoveryDirective`; an unclassified error aborts.

## Recovery model

| Error | Action |
| --- | --- |
| `ToolValidationError` | `inform_model` |
| `PermissionDeniedError` | `no_retry` |
| `WorkspaceBoundaryError` | `abort` |
| `ProcessTimeoutError` | `limited_retry` |
| `ProcessCancelledError` / `CancellationError` | `cancelled` |
| `ModelTransientError` | `retry_backoff` (bounded) |
| `ModelPermanentError` | `abort`, or provider fallback when configured |
| `ContextOverflowError` | `compact_context` |
| `VerificationFailure` | `return_evidence` |
| `BudgetExceededError` | `stop` |
| `FatalRuntimeError` | `abort` |
| anything unclassified | `abort` |

## Gates

```text
pytest -q             116 passed
ruff check .          All checks passed!
ruff format --check   49 files already formatted
mypy (strict)         Success: no issues found in 49 source files
```

## Known limitations

- **Baseline cost.** Capturing a baseline runs the full check suite before the agent
  starts. On a large repository that is slow; it can be disabled with
  `AgentLoopConfig(capture_baseline=False)`, at the cost of attribution — a failure with no
  baseline is reported as `unattributed` and fails verification rather than being excused.
- **Integrity detection is diff-based and heuristic.** It requires a git repository; in a
  non-git workspace the integrity check is skipped rather than guessed. It reasons about
  textual patterns, so a sufficiently creative weakening (rewriting an assertion to a
  tautology) is not caught.
- **`INCONCLUSIVE` is currently reachable only through an empty plan.** A project that
  defines no runnable checks cannot be auto-completed at all, which is deliberate but
  blunt.
- **Repair cycles are bounded and undirected.** The model receives the evidence digest and
  is trusted to act on it; the runtime does not diagnose the failure for it.
- **`ProcessCancelledError` is modelled as a `ToolExecutionError`** that both the loop and
  the recovery policy special-case. The taxonomy would be cleaner with a dedicated
  cancellation branch.
- **Single provider per run.** `provider_fallback` exists in the recovery policy as a
  directive, but no multi-provider router is wired into the loop.
