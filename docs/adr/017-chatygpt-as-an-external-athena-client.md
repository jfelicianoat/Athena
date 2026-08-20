# ADR-017: ChatyGPT as an external Athena client

- Status: **Accepted** — validated 2026-08-19; Athena side implemented (`athena.adapters.service`), ChatyGPT side outstanding
- Date: 2026-08-19
- Affects: Athena (new service adapter), ChatyGPT (new Rust client + Athena mode)

## Context

ChatyGPT is a Tauri 2 desktop application whose Rust core already owns external service
access and talks HTTP to AI_Broker. Athena is a Python agent runtime whose interfaces so
far are the CLI and nothing else: `EventBus` is in-process, `AgentLoop` is driven by direct
calls, and no port is open.

ADR-007 said interfaces consume runtime events rather than owning agent logic, and named
ChatyGPT as one such interface. That promise has never been cashed, because the runtime
publishes its events only to objects living in the same Python process.

Two constraints frame everything below. Athena must keep working without AI_Broker
(ADR-002), and ChatyGPT must keep working without Athena: a normal chat is a conversation
with a model, and it has no workspace, no permission engine and no verification. Routing
every chat through an agent runtime would make ordinary conversation pay for machinery it
does not use, and would couple two components that are graded separately.

## Decision

### 1. Two modes, one application

ChatyGPT keeps its existing path untouched:

```
ChatyGPT ──HTTP──> AI_Broker            (normal chat, unchanged)
```

and gains a second, explicitly selected one:

```
ChatyGPT ──HTTP+SSE──> Athena service ──> Athena runtime ──> ModelProvider
```

An **Athena run** is a distinct conversation kind in ChatyGPT's schema, not a flag on a
normal chat. Normal chat never transits Athena.

### 2. Layering

```
React  ──────────── no agent logic, no HTTP, no secrets
  │  Tauri commands / Tauri events
Rust core ───────── AthenaClient, projections, SQLite cache, DPAPI secrets
  │  HTTP + SSE over 127.0.0.1
Athena service ──── adapter package, NOT core
  │  in-process calls
Athena runtime ──── AgentLoop, PermissionEngine, VerificationPolicy, TaskManager
```

React receives projections and emits intents. Every decision that matters — permission,
verification, cancellation, budget — stays in Athena.

### 3. The service is an adapter, never core

Athena's core has `dependencies = []` and keeps it. The HTTP/SSE service lives beside the
OpenAI-compatible adapter, as `athena.adapters.service`, and is installed through an
optional extra. Core gains no transport, no framework and no knowledge that a UI exists.

The service binds to `127.0.0.1` only and requires a bearer token generated at service
start — the same shape the user already accepts from AI_Broker, and ChatyGPT already stores
a Broker credential with DPAPI in `secrets.rs`.

### 4. Transport: SSE for events, POST for intents

Athena has no API today, so this is a choice rather than a discovery.

| Option | Verdict |
| --- | --- |
| **HTTP + SSE** | **Chosen.** One long-lived connection, auto-reconnect with `Last-Event-ID`, no new core dependency, trivial through localhost. |
| WebSocket | Rejected for now. Buys bidirectionality that approvals — rare and low-rate — do not need, and costs a dependency or a hand-rolled framing layer on both sides. |
| Polling | Rejected as the primary path. Kept as the documented fallback if SSE proves unreliable inside WebView2. |

`RuntimeEvent` already carries `event_id`, `session_id`, `correlation_id` and
`occurred_at`, and payloads are already redacted by `InMemoryEventBus.publish`. Serialising
it is a mapping, not a design.

### 5. Reconnection is snapshot-then-tail, not an event log

> **Amended by ADR-021 (2026-08-20).** A bounded in-memory replay buffer now lets a client
> that sends `Last-Event-ID` be caught up event by event. The reasoning below still holds
> and still governs the fallback: the buffer is not persisted, is bounded by a constant,
> and is never the source of truth.

Athena deliberately persists **structured state**, not a transcript (ADR-008, ADR-013).
Building an event journal to replay would contradict that and duplicate the session store.

So on connect or reconnect the client receives:

1. a `state` frame carrying the current `SessionRecord` projection — status, working
   memory, verification, tool references, checkpoints;
2. then the live event tail.

The client's view is therefore always reconstructible from Athena alone. Missing events
during a disconnect are not a correctness problem, because the snapshot supersedes them.

### 6. AgentRun reference

The identity crossing the boundary is Athena's own session id:

```
AgentRunRef { run_id, workspace_id, service_base_url }
```

ChatyGPT stores the reference and a **projection** for offline display. It never stores
authoritative run state. On any disagreement Athena wins; the projection is a cache that
may be discarded and refetched. This is what "Athena remains source of truth" means
concretely.

### 7. Human approval

Athena already has the seam: `PermissionPrompt` is an async port returning a single-use
ALLOW or DENY, and `ConsolePermissionPrompt` is one implementation. The service adds
`RemotePermissionPrompt`:

1. the engine returns ASK;
2. the prompt publishes `permission.requested` with a server-generated, single-use
   `request_id` and the full `PermissionRequest` — tool, concrete action, tier, reason,
   possible effects;
3. it awaits `POST /v1/runs/{run_id}/approvals/{request_id}`;
4. it resolves, and the run continues or refuses.

Non-negotiables carried over unchanged: approval is single-use, there is no "always allow",
R4 is never offered to a human, and a hook can still only narrow. Three additions specific
to being remote:

- **the `request_id` is consumed on first use**, so a replayed or duplicated POST cannot
  approve a second action;
- **an approval that times out is a DENY**, matching the unattended default;
- **an approval arriving after cancellation is refused**, not applied.

#### Timing: three clocks, not one

An approval timeout measures *how long a human takes to decide*. It is not a network
timeout. AI_Broker running on another machine makes the model slower; it does not make the
user read a diff any slower. Charging network latency against the human's thinking time
would produce false denials that waste an entire run, so the two are kept apart:

| Situation | Window | Reason |
| --- | --- | --- |
| No client attached | deny immediately | This is the unattended case Athena already denies. Nobody is going to answer. |
| Client attached, has not acknowledged display | 30 s (`delivery_timeout`) | Covers delivery and rendering. If the prompt never reaches a screen, waiting longer helps nobody. |
| Client acknowledged it is showing the prompt | 300 s (`approval_timeout`) | Long enough to read a diff and think; short enough that an abandoned run does not hold a workspace all night. |

Both windows are configurable. The client sends an explicit acknowledgement when the
prompt is on screen, which is what starts the human clock — so a slow model or a slow link
delays the *request*, never the answer.

The remaining time is sent to the client so the countdown is visible. A human watching a
timer run out is making a decision; a human discovering a silent denial afterwards is
debugging one.

#### What a timeout does to the run

Nothing special, and that is deliberate. A timeout is a DENY, and a denial is already a
solved case: `RecoveryPolicy` maps `PermissionDeniedError` to `NO_RETRY`, the executor
returns a structured error to the model, and the model adapts — finds another route, or
finishes by saying it could not proceed. If what it produces is not good enough,
`VerificationPolicy` refuses completion, exactly as it would for any other shortfall.

So the answer to "does the run fail or continue?" is neither: it continues, informed, and
completion still has to be earned. That is the runtime deciding per task rather than a
global policy guessing.

One bound is added, because an abandoned run would otherwise grind through its whole budget
denying itself: **`max_consecutive_approval_timeouts` (default 3) aborts the run.** Nobody
is coming back, and burning a budget to prove it is waste.

### 8. Artifacts

Two distinct things, deliberately not conflated:

- **Externalized tool results** already exist as `athena-result://<key>` with a documented
  retention window. The service exposes `GET /v1/results/{key}`, returning `410 Gone` when
  `ToolResultUnavailableError` says the reference expired. Payloads are raw and *not*
  redacted — the token is the access control, and this must be stated in the ADR rather
  than discovered.
- **File changes** arrive as `file.changed` events already carrying a bounded unified diff.
  Those are what the UI renders as a diff view.

### 9. Recovery

ChatyGPT must never present an interrupted run as finished. On service start Athena calls
`SessionStore.mark_interrupted()`, moving anything live to `recovery_pending`. The client:

- `GET /v1/runs?status=recovery_pending` on connect;
- surfaces them as *needing a decision*, visually distinct from completed and failed;
- offers Resume, which calls `POST /v1/runs/{run_id}/resume` and rebuilds the run from
  stored working memory with an empty conversation (H4 behaviour, unchanged).

### 10. Task progress

Progress is **derived in Rust**, never computed in React and never invented by Athena as a
percentage. The inputs already exist: `agent.started`, per-iteration checkpoints,
`tool.started` / `tool.completed`, `verification.check.*`, `subagent.*`, and `TaskManager`
states with their budgets. The Rust core folds them into a projection; React renders it.

### 11. One writer per run

A run has exactly one controlling client; further connections are observers that receive
events and may not send intents. Two UIs approving the same permission request is a race
with a security consequence, and the cheapest correct answer is to make it impossible.

Intents travel on their own connection, so the controlling client has to prove itself:
the SSE `state` frame hands it a `subscriber_id`, and every intent echoes that value in an
`X-Athena-Subscriber` header. A request without it is refused with `not_controller` while
somebody holds control. This is a rule that is easy to state and easy to forget to
implement on the client side — the first end-to-end run of the service failed on exactly
this, which is why it is spelled out here rather than left implicit.

### 12. Service lifecycle: attach first, spawn second, block never

ChatyGPT supports both a service it starts and one the user started, and it never blocks on
either.

1. **Probe** the configured base URL. If it answers, **attach** as an *external* service.
2. If it does not, optionally **spawn** a *managed* child process.
3. Either way the work happens off the UI thread. React sees a connection state —
   `disconnected`, `starting`, `connected`, `unavailable` — and never a modal spinner.

The distinction between external and managed is load-bearing at shutdown: **ChatyGPT kills
only what it spawned.** A service the user started, possibly to drive Athena from the CLI
in another window, is not ChatyGPT's to terminate. A managed child is killed as a process
tree, so nothing is orphaned.

Athena being unavailable disables Athena runs and nothing else. Normal chat continues
straight to AI_Broker, because the two paths share no dependency.

### 13. Workspace selection reuses `authorized_folders`

The workspace is chosen per run from ChatyGPT's existing authorized-folder mechanism. A
folder must already be authorized before a run can target it; Athena's own `Workspace`
boundary then canonicalises and enforces it independently. Two independent checks, same
defence-in-depth pattern as everywhere else in this runtime: ChatyGPT decides what the user
offered, Athena decides what is reachable.

### 14. A run from ChatyGPT asks

Athena runs launched from ChatyGPT start in `--writes ask` and `--exec ask`. The tools are
registered, and **every** mutation and every command is confirmed once, by a human, through
the approval flow above. This is H2's `ask` mode unchanged — no new mechanism, no new
default, and no standing grant.

`allow` remains available for a user who deliberately opts into it per run. It is never the
default, because a desktop UI makes an approval cheap to answer and a mistake expensive to
undo.

## What this requires building

**In Athena** (new, all outside core):

- `athena.adapters.service`: HTTP + SSE, token auth, localhost binding.
- `RemotePermissionPrompt` implementing the existing port.
- A run registry mapping `run_id` to a live `AgentLoop` and its `CancellationSource` —
  `TaskManager` already provides most of this.
- An `EventBus` subscriber that fans out to connected clients with per-client queues and
  backpressure.
- `RuntimeEvent` and `SessionRecord` JSON projections.

**In ChatyGPT**:

- `AthenaClient` in the Rust core: base URL, DPAPI-stored token, SSE stream with
  reconnection, intent POSTs, typed mapping.
- A service supervisor: probe, optional spawn, connection state machine, tree-kill of a
  managed child on exit and never of an external one.
- Athena-run tables in SQLite: reference plus projection, migration in the existing series.
- Tauri commands (start, cancel, resume, approve, fetch artifact) and Tauri events for the
  stream, following the existing frontend↔Tauri contract that `test_frontend_contract.py`
  already checks statically.
- React views: run timeline, approval prompt, diff viewer, recovery list. No agent logic.

## Consequences

ADR-007's promise finally holds: an interface consumes events and services without owning
any agent logic, and the same runtime serves CLI, ChatyGPT and later Telegram without
change.

The costs are real. Athena grows an attack surface it did not have — mitigated by localhost
binding, a per-start token, and the fact that every dangerous decision still goes through
the permission engine rather than the transport. The service must be running for Athena
mode to work, so ChatyGPT needs a clear "Athena unavailable" state rather than a hang. And
approvals now traverse a network boundary, which is why request ids are single-use and
timeouts deny.

## Implementation notes (Athena side, 2026-08-19)

The service exists as `athena.adapters.service`, stdlib only, and Athena's core still
declares no dependencies. Routes:

```
GET  /v1/health                                    (no token)
GET  /v1/runs?status=...
POST /v1/runs                                      {objective, workspace, writes, exec, ...}
GET  /v1/runs/{id}
GET  /v1/runs/{id}/events?control=1                SSE: state frame, then the tail
POST /v1/runs/{id}/cancel
POST /v1/runs/{id}/resume                          {workspace}
POST /v1/runs/{id}/approvals/{request_id}/ack      starts the human clock
POST /v1/runs/{id}/approvals/{request_id}          {decision: allow|deny}
GET  /v1/results/{key}                             410 Gone once retention expires
```

Two things the build changed about the design:

- **`AgentLoop.run` now accepts an optional `session_id`.** The loop used to mint its own,
  which meant a service could not name a run before it started and had to guess the
  identity from the first event. Naming it up front is a one-line core change that removes
  a race rather than papering over it.
- **`start` waits for `session.persisted`, not `agent.started`.** The loop announces itself
  *before* it writes, so returning on the earlier event would hand a client an id that
  answers 404 for its first few milliseconds.

## Resolution of the open questions

| Question | Resolution | Decided by |
| --- | --- | --- |
| Approval timeout | Three clocks: deny at once with no client, 30 s for delivery, 300 s for a human who is looking. Both configurable. | Delegated to the architect; see §7. |
| What a timeout does to the run | Nothing new — it is a DENY, and denials are already handled by `RecoveryPolicy` and verification. Bounded by 3 consecutive timeouts. | Delegated to the architect; see §7. |
| Service lifecycle | Both supported: attach to an external service, or spawn a managed child. Never blocks the UI. ChatyGPT kills only what it spawned. | User |
| Workspace selection | Per run, reusing `authorized_folders`, with Athena's `Workspace` boundary as the second check. | User |
| Capability defaults | `--writes ask` and `--exec ask`. Every mutation and command confirmed once. | User |

The two delegated decisions are the architect's, not the user's, and are recorded as such
so that overruling either is a change to this file rather than an argument about what was
agreed.
