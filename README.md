# Athena

Athena is a provider-neutral autonomous-agent runtime. H1 implements a functional,
read-only repository-investigation loop on top of the contracts frozen in H0.

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
python -m mypy src tests
```

## Development CLI

The default endpoint matches a typical local OpenAI-compatible server. Configuration stays
outside the runtime core.

```text
set ATHENA_BASE_URL=http://localhost:1234/v1
set ATHENA_MODEL=local-model
athena D:\path\to\repository --objective "Explain the authentication flow"
```

The recommended investigation sequence is `Glob → Grep → ReadRange`. The H1 runtime never
offers write, shell, Git mutation, or network tools to the model.
