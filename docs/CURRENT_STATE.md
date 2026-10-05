# Current implementation state

Reconciled with the source tree on **2026-09-28**, after the audit in
`Informes/Athena-2026-09-28/INFORME_ATHENA.md` (27 findings, A01–A27). Every finding and
what was done about it is listed in [`AUDIT_2026-09-28_RESOLUTION.md`](AUDIT_2026-09-28_RESOLUTION.md).

**0.3.0 (2026-10-03)** adds optional System-1 judgments through AI_Broker (goal completion,
context filtering, reviewer gate) and the Client_API §3 authentication errors. Design,
configuration, live measurements and validation: [`SYSTEM1_INTEGRATION.md`](SYSTEM1_INTEGRATION.md).

This file is the current-state index. Accepted ADRs define architectural decisions;
acceptance reports and integration reports are dated evidence, not rolling status pages.

## Runtime

Athena is a provider-neutral autonomous-agent runtime. It owns the agent loop, bounded
budgets, tool registry, permission decisions, workspace boundary, event log, verification,
repair, delegation, durable runs and recovery. AI Broker is one optional `ModelProvider`;
it never owns Athena's tools or completion rules.

## Interfaces

- `athena-desktop`: native Tk desktop client. Since 0.2.0 it runs on the same
  `RunRegistry` as the service (it used to build its own reduced loop).
- `athena-service`: loopback HTTP/SSE service used by ChatyGPT and Agora.
- `athena`: development CLI.
- `athena_telegram`: channel adapter; a channel projects the same runtime and does not
  create a second agent implementation.

The service wire version is 1.

## Capability matrix

`✔` implemented and wired; `—` not offered by that interface. The last column names the
tests that exercise the capability through a real entry point.

| Capability | Desktop | Service | CLI | Tests |
| --- | --- | --- | --- | --- |
| Checks run only with execution authority (off / ask / allow) | ✔ | ✔ | ✔ | `test_auditoria_20260928::test_a01_*`, `test_desktop_auditoria::test_a01_*` |
| Command classification with path confinement | ✔ | ✔ | ✔ | `test_auditoria_20260928::test_a02_*` |
| Evidence profiles: questions / software / documents | ✔ | ✔ | — | `test_desktop_auditoria::test_a04_*`, `test_profiles` |
| A change request needs a successful write (`require_change`) | ✔ | ✔ | — | `test_desktop_auditoria::test_a04_asking_*` |
| Hierarchical runs with the run's model, deadline and goal rules | — | ✔ | — | `test_auditoria_20260928::test_a06_*` |
| Resume with the authorized configuration and project | ✔ | ✔ | ✔ (loop only) | `test_auditoria_20260928::test_a07_*` |
| Project memory keyed by a stable project identity | ✔ | ✔ | — | `test_auditoria_20260928::test_a08_*` |
| Reader/writer exclusion per project folder (in-process) | ✔ | ✔ | ✔ | `test_auditoria_20260928::test_a09_*` |
| Rollback of files the run wrote, surviving restarts | ✔ | ✔ | — | `test_auditoria_20260928::test_a10_*`–`test_a13_*` |
| Goal revision while running | ✔ (direct runs) | ✔ (direct runs) | — | `test_goal_revision`, `test_a06_a_hierarchical_run_refuses_*` |
| Approval with diff / command preview | ✔ | payload only | console | `test_desktop_auditoria::test_a18_*` |
| History of past runs | ✔ | ✔ | `--list-sessions` | `test_desktop_auditoria::test_desktop_history_*` |
| Hierarchical planning | — | ✔ (`ATHENA_PLANNING`) | — | `test_service_orchestration` |
| Delegation (`delegate_task`) | ✔ | ✔ | — | `test_delegation` |

## Safety and completion

- Running the project's checks is running the project's code. It needs the same
  execution authority as `bash`; with execution `off` nothing of the project runs, not
  even for the baseline, and the run ends as `verification_inconclusive` with reason
  `execution_not_authorized`.
- `CommandPolicy` confines every argument that names a path to the workspace and never
  lets an extension loosen a built-in rule. It classifies argv; it is **not** an operating
  system sandbox — an authorized test suite runs with the user's permissions.
- Missing paths inside the workspace are reported as not found; canonical escapes remain
  boundary violations.
- Repeated identical tool turns produce a warning and then `no_progress`.
- Human allow and deny decisions both reset approval-abandonment counters; only silence
  counts as absence. A run waits a few seconds at start for its creating client to attach
  before treating a question as unattended.
- Completion belongs to verification. "All checks pass" and "this change broke no check"
  are reported differently; neither is presented as proof that the objective was met.
- Checkpoints and rollback are explicit and scoped to files the run wrote. A file changed
  after Athena wrote it is reported as a conflict and left untouched. Athena commits only
  through `git_commit` (R3, always asked); it never pushes, tags, merges or deploys.

## Memory and recovery

The service and the desktop use SQLite stores for runs, run manifests, event history,
project memory and artifacts. Connections are closed after every operation and each
database records its schema version. Interrupted live runs become recovery candidates only
when their owning process is gone; resuming reconstructs execution from structured state
and the run manifest (project, options, remaining budget).

## Isolation

Only the shared-workspace strategy is used. `isolation.py` and `integration.py` are a
tested library for worktrees that no runtime path calls yet.

## Verification gates

```text
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m mypy src tests
```

The same four gates run in `.github/workflows/gates.yml`. Manual evidence is still required
for provider availability, external credentials and a real ChatyGPT-managed service
lifecycle.

## Known limits

- Workspace access coordination is per process: the desktop and a separately running
  service do not see each other's locks.
- Telegram deduplication is at-most-once: an update received just before a crash is not
  replayed, and may not be acted on.
- `pyproject.toml` declares `Proprietary` while `LICENSE` contains MIT; the owner has to
  decide which one applies.
