# Athena

Athena is a provider-neutral autonomous-agent runtime. H1 implements a functional,
read-only repository-investigation loop on top of the contracts frozen in H0. H2 adds
mutation and local execution behind a deterministic permission engine. H3 makes
completion conditional on evidence, and lets Athena repair its own broken changes.

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
