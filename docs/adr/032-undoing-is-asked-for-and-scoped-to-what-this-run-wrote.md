# ADR-032: Undoing is asked for, and scoped to what this run wrote

- Status: **Accepted** — implemented 2026-08-22 in `athena.rollback`, `athena.subagents`, `athena.adapters.service.orchestration`, `athena.adapters.service.runs`, `athena.adapters.service.server`, `athena_service`
- Date: 2026-08-22
- Extends: ADR-011 (the security model) and ADR-016 (parallelism is earned)
- Affects: what a person can undo after a run, and what they cannot

## Context

`checkpoints.py` has been able to copy and restore files since H2 and nothing called it —
its own docstring said so. `rollback.py`, the layer that decides when a copy is worth taking
and what an undo may touch, existed complete, argued and tested, and **no other file
imported it**. Two whole layers, and a task that broke the workspace left it broken.

## Decision

**The runtime leaves the material; a person asks for the undo.**

Nothing rolls back automatically. That was already decided in `checkpoints.py` — an
automatic rollback would discard work a human might have wanted to inspect — and this phase
does not change it. What it changes is that the decision is now one somebody can exercise,
rather than a module nobody imported.

### The copy is taken immediately before an edit

This is the part that took two attempts. Hooking the copy to the start of a task, using the
files the plan said it would touch, produces nothing: a real plan almost never names files,
so exactly the runs the model drove on its own — the ones most worth protecting — got no
copy at all. A broker run proved it by fixing a bug and leaving not a single point to
return to.

`PRE_EDIT` knows the specific file and fires before the write. Those are the two properties
required, and it is the runtime's own extension point.

The hook is **observational, never blocking**. A copy that could not be made is a missing
safety net; turning it into a veto over the edit would mean a full disk stops work, which is
worse and surprising.

### It must reach the delegates, because that is where writes happen

The second attempt still produced nothing for hierarchical runs: `SubagentRunner` builds its
own `ToolExecutor`, so hooks that lived only in the parent loop saw no writes at all — and
in a planned run *every* write is a delegate's. The hooks are handed down. Any new path that
constructs a child executor has to do the same.

### What an undo refuses to touch

Scoped to files this run wrote, and nothing else. A rollback that reverted the workspace
wholesale would take a person's uncommitted work along with the agent's mistake, and that
person would have no way to find out: no event, no diff, nothing. So a file the run did not
write is left alone even when it stands between the workspace and a clean state, and it is
reported as `protected` rather than silently skipped.

Scopes mirror cancellation's — task, subgraph, run — for the same reason: undoing one task
must not undo the one beside it.

## Consequences

- `GET /v1/runs/{id}/rollback` lists what could be undone without undoing anything;
  `POST` does it. A deployment with no checkpoint store answers 404 and says so rather than
  pretending it has copies.
- Copies live in the state directory, outside the workspace. A backup kept inside what it
  protects disappears with it, which is the quietest way to have no backup. And they are
  still not commits: Athena does not write to a person's git history to protect itself.
- One ledger per run, created on demand. A shared one would not know where one run ends, so
  a run-scoped undo could reach into another.
- A test asserts that nothing under `athena/` calls `roll_back` except the module that
  defines it and the HTTP handler. The property is that undoing is always asked for, and it
  is worth defending structurally.
- Verified against the real broker: a hierarchical run fixed a bug, left one rollback point
  naming `calc.py`, and a requested rollback restored it while leaving a file written by a
  person afterwards untouched.
