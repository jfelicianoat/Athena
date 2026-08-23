# Current implementation state

Reconciled with the source tree on **2026-08-23**.

This file is the current-state index. Accepted ADRs define architectural decisions;
acceptance reports and integration reports are dated evidence, not rolling status pages.

## Runtime

Athena is a provider-neutral autonomous-agent runtime. It owns the agent loop, bounded
budgets, tool registry, permission decisions, workspace boundary, event log, verification,
repair, delegation, durable runs and recovery. AI Broker is one optional `ModelProvider`;
it never owns Athena's tools or completion rules.

## Interfaces

- `athena`: development CLI.
- `athena-desktop`: native Tk desktop client.
- `athena-service`: loopback HTTP/SSE service used by ChatyGPT.
- `athena_telegram`: channel adapter; a channel projects the same runtime and does not
  create a second agent implementation.

The service wire version is 1. Its current surface includes health and credential checks,
runs, event replay, history, goal revision, approvals, cancellation, resume, rollback,
profiles, optional model catalogue, optional project memory, metrics, result artifacts and
identity linking.

## Model selection

`ATHENA_ALLOWED_MODELS` defines the models this deployment offers. `ATHENA_PREFERRED_MODEL`
defines the default and is included in the offered set. `POST /v1/runs` may select one of
those names. An explicit run choice disables Broker fallback; an omitted choice remains a
deployment preference and lets the Broker route. Deployments without a catalogue answer
404 `models_fixed` from `/v1/models` and keep the previous fixed/routed behaviour.

## Safety and completion

- Missing paths inside the workspace are reported as not found; canonical escapes remain
  boundary violations.
- Repeated identical tool turns produce a warning and then `no_progress`; they do not burn
  the whole budget silently.
- Human allow and deny decisions both reset approval-abandonment counters; only silence
  counts as absence.
- Completion belongs to verification. A run that changes nothing while required checks
  remain red is inconclusive, not successful.
- Checkpoints and rollback are explicit and scoped to files written by the run; Athena
  never commits, pushes or deploys.

## Memory and recovery

The service uses SQLite stores for runs, event history, project memory and artifacts.
Project memory is implemented: observations are proposed, only verified or explicitly
confirmed knowledge is recalled, and entries can expire, be confirmed or forgotten.
Interrupted live runs become recovery candidates; resuming reconstructs execution from
structured state rather than relying on the transcript.

## Verification gates

```text
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m mypy src tests
```

Manual evidence is still required for provider availability, Windows UI behaviour,
external credentials and a real ChatyGPT-managed service lifecycle.

