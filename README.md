# Athena

Athena is a provider-neutral autonomous-agent runtime. H1 implements a functional,
read-only repository-investigation loop on top of the contracts frozen in H0. H2 adds
mutation and local execution behind a deterministic permission engine.

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
athena D:\path\to\repository -o "Fix the failing test" --writes ask --exec ask
```

- `off` (default) does not register the tools at all.
- `ask` registers them; every call is confirmed once, on the console.
- `allow` grants the tier by policy; destructive and irreversible actions still ask.

Athena has no push, pull, fetch, merge, rebase, tag, publish or deploy capability, and those
commands are refused by the execution policy, so the model cannot request them.
