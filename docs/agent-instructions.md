# AGENTS.md resolution in H1

Athena resolves project instructions without reading outside the explicit workspace:

1. Load `<workspace>/AGENTS.md` when present.
2. For every file or directory discovered through a validated tool call, walk from the
   workspace root to that target's parent and load each `AGENTS.md` encountered.
3. Deduplicate instruction files by canonical path.
4. Present instructions root-first and closest-to-target last; later, more specific
   instructions take precedence when they conflict.
5. Never follow an instruction file reached through a symlink that resolves outside the
   workspace.
6. Stop adding instruction text at the configured character limit. Repository contents are
   never loaded wholesale as context.

## Declaring verification commands

Athena reads verification commands from a `## Verification` section of the workspace
`AGENTS.md`. Each non-empty line, optionally inside a fenced block, is one command:

```text
## Verification

```
python -m pytest -q
python -m ruff check .
```
```

Rules:

1. A command is parsed into argv and classified by `CommandPolicy`. Only plain local
   execution (R2) is accepted; anything else is silently discarded.
2. Shell metacharacters are rejected, so one command per line and no pipelines.
3. Explicit operator configuration takes precedence over `AGENTS.md`, which takes
   precedence over commands detected from `pyproject.toml` or `package.json`.
4. If no command survives, verification is `INCONCLUSIVE` and the run cannot complete.
