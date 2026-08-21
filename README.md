# Athena

Athena is a provider-neutral autonomous-agent runtime. H1 implements a functional,
read-only repository-investigation loop on top of the contracts frozen in H0. H2 adds
mutation and local execution behind a deterministic permission engine. H3 makes
completion conditional on evidence, and lets Athena repair its own broken changes. H4
makes a session durable, so a long run can be compacted and an interrupted one resumed.
H5 opens the runtime to extension without letting an extension widen what it may do,
and H6 adds three bounded delegates rather than a general swarm. H7 adds task
management, controlled concurrency, background processes and local checkpoints.

## Architecture

The `athena` package defines boundaries for model inference, tools, deterministic
permissions, cancellation, events, verification, structured state, typed errors, and
large-result storage. All inference crosses `ModelProvider`; all effects cross `Tool` and
`PermissionEngine`; interfaces consume `EventBus` events instead of owning agent logic.

H1 adds:

- an explicit, bounded `AgentLoop` with cancellation, timeout, retry, and verification;
- canonical workspace confinement, including symlink escape rejection;
- `Glob`, `Grep`, `ReadRange`, `ReadFile`, and `ListDirectory` tools;
- correlated tool calls and basic in-memory externalization of oversized results;
- dynamic bounded Git and `AGENTS.md` context;
- a separate OpenAI-compatible provider adapter and an event-driven development CLI.

Decisions and their rationale live in [`docs/adr`](docs/adr/README.md).

## Development

```text
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m mypy src tests
```

All four commands are gates: a change is not finished until tests, lint, formatting and
type checking are clean.

## Development CLI

The default endpoint matches a typical local OpenAI-compatible server. Configuration stays
outside the runtime core.

```text
set ATHENA_BASE_URL=http://localhost:1234/v1
set ATHENA_MODEL=local-model
athena D:\path\to\repository --objective "Explain the authentication flow"
```

The recommended investigation sequence is `Glob -> Grep -> ReadRange`. By default the
runtime offers only read-only tools.

## Desktop application

Athena includes a native desktop interface for Windows. It can select a project, configure
AI_Broker or an OpenAI-compatible endpoint, choose capability permissions, start a run,
show its result and activity, resolve individual approval requests, and cancel work.

Install the project and open the application:

```text
python -m pip install -e .
athena-desktop
```

On Windows, you can also open `iniciar-athena.bat` from File Explorer. The launcher uses
the project's virtual environment when available and otherwise looks for an installed
Python graphical launcher.

Alternatively, from a source checkout:

```text
python -m athena_desktop
```

The desktop window uses Python's native Tcl/Tk component. On Windows, keep the
`Tcl/Tk and IDLE` option enabled in the official Python installer.

Preferences are stored under `%LOCALAPPDATA%\Athena\settings.json`. Tokens are deliberately
excluded: enter one for the current application session, or provide `ATHENA_BROKER_TOKEN`
for AI_Broker and `ATHENA_API_KEY` for OpenAI-compatible providers. AI_Broker receives its
credential as `x-admin-token`; OpenAI-compatible endpoints receive a Bearer token.
For AI_Broker, Athena translates its tool definitions and the model's decisions through a
structured JSON contract. Tool execution and permission decisions remain inside Athena.

Athena Desktop also manages the local service used by ChatyGPT. **Iniciar servicio** starts
it with the provider settings shown in the window. Once Athena is listening, the window
shows both its local URL and its newly generated service token; **Copiar** places that token
on the clipboard. The broker token and the Athena service token are different credentials.
The latter lives only for that service process and disappears from the window when it stops.
If another application already owns a healthy Athena service on the configured port, the
desktop reports it as externally managed instead of starting or stopping a second instance.
Only the application that launched that process can know the one-time startup token.

## ChatyGPT service

ChatyGPT communicates with Athena through its loopback-only HTTP service. The
recommended launcher is `ChatyGPT\Arrancar ChatyGPT.bat`, which configures and starts the
service automatically. For a manual development launch, define
`ATHENA_BROKER_BASE_URL` and `ATHENA_BROKER_TOKEN`, then run:

```text
python -m athena_service
```

Athena generates a fresh bearer token and, only after opening the socket, writes one
machine-readable startup line to stdout:

```text
ATHENA_SERVICE_READY {"base_url":"http://127.0.0.1:8770","token":"..."}
```

A managing application such as ChatyGPT captures that line from the child process instead
of searching logs or reading a credential file. A manual launch displays the same line so
the user can copy it. `ATHENA_SERVICE_TOKEN` remains an optional explicit override, useful
for controlled development, but is no longer required.

The service listens on `127.0.0.1:8770` by default. `ATHENA_SERVICE_PORT` and
`ATHENA_STATE_DIR` may override the port and durable state directory. Do not publish this
endpoint on the LAN; authentication is required even though it is bound to loopback.

## Verification and self-repair

A run completes only when the project's own checks pass. Athena discovers those commands
from an `AGENTS.md` `## Verification` section or from the project configuration; it never
invents one, and a command that is not plain local execution is discarded.

Before the agent starts, the checks run once to capture a baseline, so a repository that
was already failing is not blamed on Athena. When a check that used to pass now fails, the
evidence goes back to the model and a bounded repair cycle begins (`--max-repair-cycles`,
default 2). Deleting a test, skipping one, removing assertions or adding lint suppressions
fails verification instead of passing it.

See [ADR-012](docs/adr/012-verification-owns-completion-and-recovery-is-explicit.md) and
[V0_1_ACCEPTANCE_REPORT.md](V0_1_ACCEPTANCE_REPORT.md).

## Sessions, memory and recovery

Athena keeps three kinds of memory, with different lifetimes:

- the **conversation** is disposable and compactable;
- the **working memory** — objective, constraints, plan, files, decisions, errors,
  verification, remaining work — is structured, validated and persisted;
- **project memory** is an interface only; nothing writes to it yet.

Sessions and externalized tool results are stored in SQLite under `<workspace>/.athena`
(override with `--state-dir`). On startup any session still marked live is moved to
`recovery_pending`: an interrupted run is never resurrected as completed.

```text
athena <repo> --list-sessions
athena <repo> --resume <session-id>
```

Resuming rebuilds the run from stored working memory with an empty conversation, which is
the point: the transcript was never load-bearing. See
[ADR-013](docs/adr/013-sessions-persist-outside-the-conversation.md).

## Tasks, concurrency and checkpoints

`TaskManager` runs work with seven states — `pending`, `running`, `completed`, `failed`,
`cancelled`, `killed`, `recovery_pending` — because collapsing them loses what a human
needs to know. A task carries budgets for iterations, tool calls and wall clock, plus
tokens and cost when the provider reports them. Cancelling a parent cancels its whole
subtree; killing a task tears down the background processes it registered, as a process
tree, so nothing is orphaned. After a restart, anything still live becomes
`recovery_pending`.

Concurrency is granted, not assumed (see
[ADR-016](docs/adr/016-parallelism-is-earned-not-assumed.md)): two calls share a wave only
if **both** tools declare themselves concurrency-safe for those arguments and, when either
writes, their resources do not overlap. Independent reads run together; conflicting edits,
git mutations and commands are serialised. Two writes to different files are still
serialised, because tool identity is not evidence of safety.

Checkpoints copy the files an operation is about to touch to a directory outside the
workspace. They are **not commits** — Athena does not write to your git history to protect
itself — and restoring is always explicit.

Workspace isolation is an abstraction with one implementation: everyone shares the
workspace, which is safe precisely because conflicting writes are serialised. Worktree,
container and remote strategies refuse clearly rather than falling back, and worktrees stay
unbuilt until parallel *writing* tasks are shown to need them.

## Delegation

Three profiles, and no way to define a fourth at runtime (see
[ADR-015](docs/adr/015-three-delegates-not-a-swarm.md)):

| Profile | May | May not |
| --- | --- | --- |
| **Explorer** | read, search, read git history | write anything, run anything |
| **Coder** | read, edit, write, run local checks, see the diff | commit, crawl history |
| **Verifier** | read, run checks, see the diff | edit or write, ever |

Authority is structural before it is policy: a tool a profile may not use is absent from
its registry, so the refusal does not depend on a policy being configured correctly. Each
delegate receives a brief — objective, acceptance criteria, relevant files, the previous
step's findings — and never the parent's conversation, working memory or session store. It
carries its own iteration, tool-call and timeout budgets, and its cancellation token is
chained to the parent's. Delegates cannot delegate.

## Extending the runtime

Four extension points, all of which can narrow Athena's behaviour and none of which can
widen it (see [ADR-014](docs/adr/014-extensions-restrict-but-never-grant.md)):

- **Hooks** observe `SessionStart`, `PreToolUse`, `PostToolUse`, `PreEdit`, `PostEdit`,
  `OnError`, `PreVerify`, `PostVerify` and `SessionEnd`. A hook may BLOCK an action; there
  is no ALLOW, so no hook can override the permission engine. A *blocking* hook that
  crashes fails closed.
- **Skills** are procedural knowledge with a manifest (name, description, version,
  applicable tasks, required toolsets, instructions, metadata). They are selected by
  relevance and injected as instructions. A skill never registers a tool or widens a tier;
  one whose required toolsets are missing is simply not selected.
- **Deferred tools** stay out of the schema list until `tool_search` finds them, using the
  `load_policy` and `search_hint` fields frozen in H0.
- **MCP** lives behind an adapter in `athena.mcp`, outside the core and with no transport of
  its own. Every remote tool is wrapped with a validated schema, a permission tier
  defaulting to R3 (always ASK), a mandatory timeout, cancellation, and result-size limits.

## Capabilities and permissions

Mutation and execution are opt-in per run and gated by capability tiers (see
[ADR-011](docs/adr/011-capability-tiers-gate-mutation-and-execution.md) and
[docs/security-model.md](docs/security-model.md)):

```text
athena D:\path\to\repository -o "Fix the failing test" --writes ask --exec ask
```

- `off` (default) does not register the tools at all.
- `ask` registers them; every call is confirmed once, on the console.
- `allow` grants the tier by policy; destructive and irreversible actions still ask.

Athena has no push, pull, fetch, merge, rebase, tag, publish or deploy capability, and those
commands are refused by the execution policy, so the model cannot request them.
