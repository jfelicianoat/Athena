# ADR-033: A run that repeats itself is abandoned, and a missing path is not a boundary crossing

- Status: **Accepted** — implemented 2026-08-23 in `athena.progress`, `athena.workspace`, `athena.errors`, `athena.recovery`, `athena.agent_loop`, `athena.adapters.service.approvals`
- Date: 2026-08-23
- Extends: ADR-005 (the workspace is a security boundary) and ADR-012 (recovery is explicit per typed error)
- Affects: how a run ends when nothing is advancing, and what `workspace_boundary_error` is allowed to mean

## Context

A real run on 2026-08-22 asked for a desktop kanban application. Thirty minutes later it
died of `budget_exceeded`, having produced a scaffold for an unrelated neural network
library. The transcript shows four separate mechanisms failing to do what they said:

1. The model asked for `src/main.py` and `package.json`, which did not exist in that
   workspace. `Workspace.resolve` answered *"Workspace path is unavailable"* — the boundary
   error — and the recovery policy aborted the action. The model was never told the real
   problem, so it could not correct it, and it guessed again five more times.
2. Those same failures published `permission.resolved: deny` with no matching
   `permission.requested`, because the executor treated a boundary error as a refusal.
3. `ApprovalAbandonedError` was raised three times, each carrying the sentence *"abandoning
   the run rather than spending its budget refusing itself"*. The run then spent its entire
   budget doing exactly that: nothing was watching for the error.
4. Iterations 7, 8 and 9 issued identical tool calls and received byte-identical results.
   Nothing noticed.

The common shape is the one this project keeps rediscovering: a decision is written down
somewhere, and no code reads it. `RecoveryDirective.ends_run` already existed and already
listed ABORT. The tool-failure path returned `{"ok": false, "recovery": "abort"}` to the
model and carried on.

## Decision

### A boundary error means somebody tried to cross the boundary

`Workspace.resolve` now answers two questions in a fixed order: first whether the path
escapes the root, and only then whether it exists. A path inside the workspace that is not
there raises `WorkspacePathNotFoundError`, which is **not** a `PermissionDeniedError` and
whose recovery is `INFORM_MODEL`.

The order is deliberate. A path outside the root that also does not exist is answered as an
escape, never as "not found": saying "it is not there" about something outside already
describes what is outside.

ADR-005 is not weakened by this — it is the reason for it. A signal that fires on typos as
well as on traversal cannot be used to detect traversal.

### A run that repeats itself is told, and then abandoned

`athena.progress` fingerprints each turn as the multiset of `(tool, arguments, result)`.
The result is part of the fingerprint on purpose: repeating a call whose answer changes is
legitimate work, and repeating one whose answer does not is not.

What the fingerprint must **not** include is anything that changes by construction. The
first implementation hashed the whole result payload, and that payload carries the
`call_id` the model mints fresh every turn — so two identical turns never produced the same
hash and the detector never fired once. It survived its own tests because the scripted
provider reused one id across turns, and it was caught by a real run:
`nemotron-3.5-lightning:30b` issued `glob **/test_cola.py` six times in a row, identical
call and identical answer, and burned its whole budget unnoticed. `call_id` and
`reference_uri` are stripped before hashing, and the tests now mint a new id per turn.

The response is in two steps, not one. On the third identical turn the model is told, in
the history and in plain words, that it has just repeated itself and that another repeat
will end the run — small models very often break out as soon as it is named. Measured: in
a real run `nemotron-3.5-lightning:30b` was repeating `pytest`, got the warning, and on the
very next turn made the edit that fixed the bug. On the fourth identical turn the run ends.

**Before it ends, the work is verified once.** A model that loops on `pytest` after fixing
the code has finished the job; what it cannot do is say so. Throwing that away reports a
good, checkable result as a failure — measured on the same run, which left the tests green
and was abandoned without ever verifying. So a run about to be abandoned for stagnation
gets one pass through the *same* `_run_verification` the normal path uses, and completes
only if that evidence permits completion. It is not a repair cycle and not a second chance
for the model: it is reading once what is already on disk before discarding it. A run that
modified no file skips even that — there is nothing to check. This does not weaken ADR-006:
completion still rests on evidence and never on the model saying "done", and here nothing
says "done" at all.

`no_progress` is deliberately not `budget_exceeded`. They end the same way and lead to
opposite conclusions: an exhausted budget suggests raising the limit, and a stuck run says
that raising it only buys more identical laps.

### `ends_run` is read where it is produced

`RecoveryDirective.ends_run` is now honoured for tool failures: the error is re-raised and
the run closes with its own code. `ToolExecutionError` and `PermissionDeniedError` still
inform the model and continue, which is what makes a refused write survivable; escapes,
abandonment, stagnation and unclassified errors do not.

### An answer is an answer, whatever it says

`RemotePermissionPrompt` counted consecutive silences and reset only on ALLOW. A person who
denies is present, and a run must not be killed for "nobody is coming back" while somebody
is saying no. Any decision that arrives from a human resets the count; only a timeout
raises it.

## Consequences

- A model that guesses a filename gets told the filename does not exist and can list the
  directory. That was already possible; the runtime was answering the wrong question.
- `workspace_boundary_error` in an event log now means an attempted escape and nothing
  else, so it is worth alerting on.
- The service answers 404, not 403, for a path that is inside the workspace and missing.
- A stuck run ends in about four iterations instead of consuming the whole budget, and says
  which tools it was repeating.
- A stuck run whose work already passes the project's checks completes instead of failing,
  with an answer that says plainly it was abandoned for repeating itself. The stagnation is
  still announced on the event stream — it is not hidden because the ending was lucky.
- `ApprovalAbandonedError` moved from `adapters/service/approvals.py` to `athena.errors`.
  It is re-exported from its old home, so nothing importing it breaks. The core recovery
  policy must be able to name it, and the core does not import from `adapters/`.

## Alternatives rejected

**Make `WorkspacePathNotFoundError` a subclass of `WorkspaceBoundaryError`** so that every
existing `except` clause keeps working. That keeps the two meanings fused where it matters
most — in `isinstance` checks and in the event log — for the sake of not editing four call
sites.

**Kill the run on the first repeated turn.** Cheaper to implement and it throws away runs
that would have recovered on their own. The warning costs one iteration and is what makes
the abandonment defensible.

**Detect repetition from the tool calls alone, ignoring results.** Simpler, and wrong: it
would flag a legitimate re-read of a file that is being written.
