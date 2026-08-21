# DeepSeek pattern integration — Phase 0 baseline

- Date: 2026-08-21
- Scope: audit only. No architectural change was made in this phase, per its own gate.
- Verdict: **PASS** — baseline green, audit complete, implementation order fixed below.

## 1. Baseline, measured not assumed

Every gate was run before writing a line of this document.

| Gate | Athena | ChatyGPT |
| --- | --- | --- |
| tests | 735 passed | 274 Rust · 239 vitest · 40 unittest |
| lint | `ruff check` clean | `clippy -D warnings` clean |
| format | `ruff format --check` clean | — |
| types | `mypy --strict`, 123 files, clean | `tsc -b` clean |

Two operational facts worth recording, because both cost time to establish:

- `ruff format --check` reports files as unformatted immediately after certain writes on
  Windows purely from line-ending normalisation. `git diff` is the tiebreaker: if it is
  empty, the committed content is fine.
- **ChatyGPT has 21 uncommitted paths, all staged.** That is the baseline as it stands, and
  it mixes several workstreams. Nothing in this phase touched it.

### There is no `AGENTS.md`

The master prompt's working method opens with "read AGENTS.md". Athena does not have one.
Its instructions live in `README.md` (the four gates) and `docs/agent-instructions.md`
(how Athena resolves *workspace* `AGENTS.md` files, which is a different thing).

This matters beyond bookkeeping: `VerificationPlanner` reads verification commands from a
`## Verification` section of the workspace `AGENTS.md`, so **Athena run against its own
repository falls back to detecting commands from `pyproject.toml`**. Worth fixing before
any self-hosted acceptance run, and it is cheap.

## 2. Audit of the eighteen concepts

Classification is by evidence in the tree, not by recollection. `RUNTIME_CONSUMED` means a
non-test module on the real execution path imports and uses it.

| # | Concept | Verdict | Evidence |
| --- | --- | --- | --- |
| 1 | Capability/provider abstractions | **EXISTS** | `models.py:37` `ModelCapabilities`; every provider implements `capabilities()` |
| 2 | Scoped capabilities | **PARTIAL** | `SubagentProfile` (`subagents.py:72`) carries toolset+policy+budget; no named `AgentScope`, no `SubagentCapabilities` |
| 3 | Provider capability negotiation | **PARTIAL** | `provider_router.py:162` *aggregates* capabilities; **no caller checks one before executing** |
| 4 | Canonical Tool output schemas | **PARTIAL** | `ToolSpec.output_schema` declared (`tools.py:30`); `tool_executor.py:76` validates **input only** |
| 5 | Tool model projection | **MISSING** | no projection layer; `ToolResult.output` is used directly |
| 6 | Tool UI projection | **MISSING** | ChatyGPT re-derives presentation from event payloads |
| 7 | Parallel/exclusive execution | **PARTIAL** | `ConcurrencyScheduler` exists and is tested; `agent_loop.py:659` runs `for call in calls:` — strictly sequential |
| 8 | Durable execution / event log | **PARTIAL** | `EventBus` is live-only; `EventCheckpoint` (`session_store.py:45`) is a bounded milestone list, not an append-only log with monotonic `seq` |
| 9 | Reconstructible model context | **MISSING** | no `ContextSnapshot`; `ContextBuilder` composes and discards |
| 10 | Goal revisioning | **MISSING** | no `revision` outside `git_tools` (unrelated sense) |
| 11 | Durable state vs live activation | **PARTIAL** | `mark_interrupted()` exists in three stores and in `TaskGraph`; the distinction is implemented but not named as a model |
| 12 | Profiles | **PARTIAL** | `SubagentProfile` is a *role* profile; no `AthenaProfile`, no registry, no non-developer path |
| 13 | Continuable subagents | **MISSING** | no lifetime, no child id, no followup |
| 14 | Child reports | **PARTIAL** | `SubagentResult`/`TaskEvidence` carry structured evidence; no `SubagentReport` with delivery semantics |
| 15 | Visibility vs authority | **EXISTS (subagents) / PARTIAL (elsewhere)** | `registry_for()` (`subagents.py:83`) is structural visibility; `PolicyPermissionEngine` is authority. Untested at the deferred-tool, MCP and composite-dispatch entries |
| 16 | Fail-loud capability enforcement | **MISSING** | capabilities are declared and never required |
| 17 | DecompositionPolicy | **EXISTS + CONSUMED** | `planning.py`; `assess` and `assess_plan` both called from `orchestration.py` |
| 18 | GraphExecutor real | **EXISTS + CONSUMED** | `graph_executor.py`, driven from `adapters/service/orchestration.py` |

## 3. The islands

This is the finding that should set the order of work. Measured by import from any
non-test runtime module:

| Subsystem | Consumed by | Status |
| --- | --- | --- |
| `graph_store` | 4 modules | **consumed** |
| `channels` | 4 modules | **consumed** |
| `project_memory` | 2 modules | **consumed** |
| `identity` | 2 modules | **consumed** |
| `diagnosis` | `agent_loop.py` | **consumed** |
| `scouting` | `orchestration.py` | **consumed** |
| `delegation` | `orchestration.py` — `confine` only | **partial**: `DELEGATE_TASK_SPEC` reaches no registry |
| `isolation` | `integration.py` only | **island of two** |
| `checkpoints` | `rollback.py` only | **island of two** |
| `metrics` | nothing | **island** |
| `concurrency` | nothing | **island** |
| `rollback` | nothing | **island** |

Five modules are `IMPLEMENTED=YES, TESTED=YES, RUNTIME_CONSUMED=NO`. By the master
prompt's own rule they are not finished. Note that `metrics` is the one the TFM's
experimental comparison depends on: nothing currently attaches a collector to the service's
event bus, so **no run started from ChatyGPT is being measured at all**.

## 4. What must not be duplicated

Athena already owns working implementations that the DeepSeek patterns would tempt a
rewrite of. Reuse, do not replace:

- **`PolicyPermissionEngine` stays the single authority gate.** X2 adds entries that must
  route through it; it does not add a second decision-maker.
- **`ToolRegistry` already implements visibility structurally** (deferred loading, per-role
  registries). `AgentScope` should *compose* it, not supersede it.
- **`ProviderRouter` already routes with a primary and fallbacks.** X1 adds a required
  capability check in front of selection; it does not introduce a second router.
- **`CancellationSource`/`chained_source` already implement RUN/SUBGRAPH/TASK scopes.**
  Phase 1.7 is a test-and-verify job, not an implementation job.
- **`SubagentProfile.registry_for()` already gives structural tool visibility.** Phase 2's
  `AgentScope` should absorb it rather than parallel it.
- **`EventBus` stays live.** Phase 6's `RunEventLog` is a second, durable sink — replacing
  the bus with event sourcing is explicitly out of scope.

## 5. ADR position

Ten ADRs are requested. Five topics are already decided and recorded; writing new ADRs for
them would duplicate.

| Requested ADR | Position |
| --- | --- |
| Decomposition Policy | **Covered** — ADR-022, ADR-024 |
| Capability enforcement | **New** — nothing records this decision |
| Capability seams | **New** |
| Agent-scoped capabilities | **New** |
| Canonical Tool results | **Extends ADR-003** (tools are capability contracts) |
| Durable execution provenance | **New** |
| Goal optimistic concurrency | **New** |
| Athena Profiles | **New**, and partially conflicts with ADR-015's "three delegates, not a swarm" — the role profiles there are not the same concept as an `AthenaProfile`, and the ADR should say so explicitly to stop the two merging |
| Continuable subagents | **New**, must amend ADR-010's one-shot assumption |
| Visibility vs Authority | **Extends ADR-011 and ADR-014** |

**Deviation, stated deliberately:** these ADRs are not being written in Phase 0. An ADR
records a decision that has been made; drafting ten of them before the design work would
be inventing decisions and then implementing to fit the document. Each will be written in
the phase that makes its decision, which is also when its "Implementation status" section
can be truthful.

## 6. Risks

1. **The parallel worktree.** ChatyGPT has 21 staged uncommitted paths and Athena received
   an entire desktop-service module mid-session. Any phase touching `athena_service.py`,
   `athena_desktop/` or the ChatyGPT frontend will collide. Sequence those late, or agree a
   freeze.
2. **Phase 5 is the widest blast radius.** Tool Contract v2 touches every tool and every
   consumer of `ToolResult`. It should land after the seams (Phases 1–3) exist to absorb it,
   and behind a documented legacy adapter.
3. **Phase 8's non-developer profile is the real test of the core.** The audit found no
   hard-coded git/pytest assumption in `AgentLoop` itself, but `VerificationPlanner`,
   `RepositoryScout` and `_WRITING_ROLES` all reason in software terms. Expect that phase to
   surface coupling the others hide.
4. **Capability enforcement can break working runs.** Turning declarations into requirements
   means providers that quietly worked now fail loudly. That is the point, but it needs the
   fallback path (Phase 10) working first or a degraded broker takes every run down.
5. **Model cost.** Phases with real end-to-end scenarios need the broker; a single coder
   turn measured 549s on this hardware. Budget accordingly and prefer scripted providers
   for everything except the acceptance scenarios.

## 7. Implementation order

Deliberately different from the prompt's numbering, for reasons stated:

1. **Phase 1** (GraphExecutor + DecompositionPolicy) — largely done; reduces to wiring
   `ConcurrencyScheduler`, adding the missing `graph.frontier.ready` / `task.scheduled` /
   `task.blocked` events, and the cancellation test matrix.
2. **Phase 12 (metrics) brought forward.** It is an island, it is cheap, and every later
   phase wants evidence. Measuring before changing is worth more than measuring after.
3. **Phase 3 X2** (visibility vs authority) before X1. It is the security-critical one and
   it needs no new provider machinery.
4. **Phase 2** (subagent seam) + **Phase 3 X1** (capability enforcement) together — X1's
   `SubagentCapabilities` has no meaning without the seam.
5. **Phase 4** (`delegate_task`) — small once 2, X1 and X2 exist; it is the piece that
   makes `delegation.py` stop being an island.
6. **Phase 10** (provider fallback) before broad capability enforcement, so fail-loud has
   somewhere to fall.
7. **Phase 5** (Tool Contract v2), **Phase 6** (RunEventLog), **Phase 13** (diagnosis).
8. **Phase 8** (Profiles) with the mandatory non-developer fixture.
9. **Phase 7** (goal revisioning) — needs both channels, so it wants the ChatyGPT tree
   settled.
10. **Phases 9, 11, 14, 15, 16** — continuable subagents, project memory hardening, client
    completion, worktrees.
11. **Phase 17** consolidation and the ten E2E scenarios.

## 8. Gate

- Baseline suites: **green** (735 / 274 / 239 / 40).
- Audit: **complete**, eighteen concepts classified with file-level evidence.
- Architectural changes in this phase: **none**, as required.

**PASS.** Ready for Phase 1 on request.
