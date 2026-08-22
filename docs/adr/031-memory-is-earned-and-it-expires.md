# ADR-031: Memory is earned, and it expires

- Status: **Accepted** — implemented 2026-08-22 in `athena.project_memory`, `athena.adapters.service.orchestration`, `athena.adapters.service.runs`, `athena.adapters.service.server`
- Date: 2026-08-22
- Extends: ADR-013 (sessions persist outside the conversation) — this is the layer that outlives the session
- Affects: what Athena knows at the start of a run that it was not told

## Context

`project_memory.py` shipped with three rules stated well and argued properly: a model's
conclusion is not a fact, everything carries provenance and a date, and correcting means
superseding rather than overwriting. All three correct. None of them exercised, because
**nothing ever wrote to the memory.**

`remember_command` existed and had no caller. `approve`, `forget`, `update` and `is_stale`
were unreachable from outside the module. So `VERIFIED` was never awarded, `USER_CONFIRMED`
was an unreachable state with a name, nothing ever aged, and Athena read an empty store at
the start of every run and rediscovered the same commands each time.

The subsystem was the shape of a good design with nothing running through it.

## Decision

**Athena learns only from evidence, marks what it knows with an age, and cannot promote
anything to "a person said so".**

### One automatic writer, and it writes what actually happened

The only thing written without a human is a **verification command that was executed in
this workspace and passed**. It enters as `VERIFIED` — not as a courtesy promotion: the
runtime ran it and it worked, which is literally "something checked it". That is the
highest rung Athena can award itself.

A check that failed is not remembered. Storing it would turn the memory into a list of
things to try, and the next session would start repeating this one's mistake with the
confidence of a recollection.

Nothing else is written automatically. Letting a run store its conclusions would make the
memory a second, staler copy of what the model thought it understood — the failure mode the
module's own docstring warns about.

### Age is per kind, and it is said rather than hidden

`STALE_AFTER` gives each kind its own shelf life: thirty days for a command, a year for an
architecture decision. One global number would have to be the volatile one — throwing away
what still holds — or the stable one — keeping lies.

A stale item is **labelled, not dropped**. Old is not false, and hiding it would lose a hint
that often still works. Handing it over undated would present it as though it certainly
holds, which is the only way this memory can do harm.

### The top rung needs a person, so a person needs a way in

`USER_CONFIRMED` means somebody stood behind it. Athena cannot award it, and a test asserts
that no module outside `project_memory.py` and the HTTP handler even names the constant.
A person cannot stand behind what they cannot see, so `GET /v1/memory`, `POST
/v1/memory/{id}/confirm` and `DELETE /v1/memory/{id}` exist. Without them the rung was
unreachable by construction.

The listing reports `stale` as a computed boolean rather than only a date: whoever is
looking wants to know what to trust, and an ISO timestamp makes every client decide for
itself when something is old — and disagree with the others.

## Consequences

- Learning cannot take down a run that already finished. What is lost when the store fails
  is a memory; what would be saved by failing there is nothing.
- Demotion is still refused. A belief that turns out wrong is superseded or forgotten, both
  of which keep the record; silently downgrading would leave an item that once looked
  trustworthy with no trace of why it stopped.
- Verified against the real broker with two consecutive runs on one repository: the first
  fixed a bug, its verification ran `python -m pytest -q` and passed, and that command was
  stored as `verified` with `source: run:<id>`. A person then confirmed it over HTTP and it
  became `user_confirmed`. Before this phase the store was empty after both runs.
- The retrieval side was already selective and stays that way. Nothing here hands back the
  whole store; a context builder that loaded all of memory would spend the window on things
  the current task has no use for.
