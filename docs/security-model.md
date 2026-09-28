# Athena security model

The model may request an action. It can never authorize one. Every decision is made by
`PermissionEngine` from the request alone, and the interface — not the agent — resolves an
ASK.

## Capability tiers

| Tier | Meaning | Decision |
| --- | --- | --- |
| R0 | Read-only local access | ALLOW |
| R1 | Write inside the workspace | ALLOW when granted by policy, otherwise ASK |
| R2 | Local execution | ALLOW when granted by policy, otherwise ASK |
| R3 | External, irreversible, cost-bearing, or history-recording | ASK, always |
| R4 | Outside policy | DENY, always |

Two invariants hold regardless of policy:

1. A request that declares R0 while also declaring that it writes or destroys is refused.
   R0 is the only unconditional ALLOW, so it must be honest.
2. A destructive request escalates to ASK even when its tier has been granted.

An R4 request is never shown to a human. Offering it would turn a policy boundary into a
question, and questions get answered "yes" when people are tired.

## What a permission request carries

`PermissionRequest` gives an interface everything it needs to render an informed prompt:
the tool, the concrete action, the relevant arguments, the workspace, the risk level and
tier, a reason, the possible effects, and whether the action is read-only, destructive or
concurrency-safe.

Approval is single-use. There is deliberately no "always allow": a standing grant would
move the security boundary from the engine to whatever the model happened to ask for first.

## Workspace boundary

Mutation and execution resolve every path and working directory through
`Workspace.resolve`, which canonicalises and then requires the result to stay under the
workspace root. Traversal (`../`), absolute paths outside the root, and symlinks or
junctions that escape are rejected before anything runs — for writes exactly as for reads.

`resolve` answers two questions, and in this order: first whether the path escapes, and
only then whether it exists. A path that escapes raises `WorkspaceBoundaryError` and aborts
the run. A path inside the workspace that is simply not there raises
`WorkspacePathNotFoundError`, which is not a permission error at all: the model is told the
path does not exist so it can list the directory and ask for a real one. A path outside the
root that also does not exist is still answered as an escape — saying "not found" about
something outside already describes what is outside. The split is what keeps
`workspace_boundary_error` worth alerting on; see ADR-033.

## Writes

- `write_file` refuses to replace an existing file unless `overwrite=true`, and refuses an
  empty payload unless `allow_empty=true`.
- A rewrite that discards more than half of an existing file is reported as destructive, so
  it escalates to ASK even under a standing write grant. This is the guard against a
  truncated model response silently emptying a file.
- `edit_file` replaces an exact literal string and requires the match count to equal
  `expected_occurrences`, so an ambiguous edit fails instead of guessing.
- Both write through a sibling temporary file and `os.replace`, so an interrupted write
  never leaves a partially written file, and both emit `file.changed` with a unified diff.

## Execution

`BashTool` never spawns a shell. A command containing shell metacharacters
(`;`, `&`, `|`, `>`, `<`, backtick, `$(`, newline) is rejected during validation, because a
shell makes the argv meaningless and the classification worthless.

The remaining argv is classified by executable, arguments and working directory:

- **R2 read**: `ls`, `cat`, `grep`, `git status`, `git diff`, `git log`, `git branch`
  (listing only), `git remote` (listing only), `pip list`, …
- **R2 build**: `pytest`, `ruff check`, `ruff format --check`, `mypy`,
  `python -m <allow-listed module>`, `npm test`, `npm run test|lint|build|check|typecheck`,
  `uv run <an R2 command>`, `cargo build`, … These run code the project defines.
- **R3**: installs, migrations, `rm`, `mv`, `chmod`, `git commit`, `git add`,
  `git branch -D|-m|<name>`, `git remote add|remove|set-url`, any other `npm run <script>`,
  and verifying tools with a writing flag (`ruff check --fix`, `ruff format`, `black`,
  `prettier --write`, `eslint --fix`, `git diff --output`), …
- **R4**: `sudo`, `curl`, `wget`, `ssh`, `npx`, `git push|pull|fetch|merge|reset|clean|tag|config`,
  `git -c …`, `git --ext-diff|--textconv`, `find -exec|-delete`, `rg --pre`,
  `python -c`, `uv run python -c`, shells, and anything not covered by the policy.

An unknown executable is R4. The default is refusal, not permission. A wrapper is
classified by what it runs: `uv run X` is X.

**Arguments are confined.** Every argument that names a path —including the value of
`--option=value`, `-o<value>` and `git -C <dir>`— is resolved with the same canonical
resolution as the file tools, symlinks included. One that lands outside the workspace makes
the command R4, whatever the executable. `python -m json.tool ../outside.json` is refused.

**What this is not.** The policy classifies argv; it does not sandbox the process. Once a
test suite or a build script is authorized, it runs with the user's permissions and can
read or write whatever the user can. For a hard guarantee, run Athena inside an operating
system boundary (a restricted account or a container). That is why every command that
runs project code needs execution authority explicitly, and why "build" is never "read".

A deployment can classify additional executables without editing the module:

```python
CommandPolicy(
    build_commands=("gradle", "bazel"),
    subcommands={"just": {"test": "build", "deploy": "forbidden"}},
)
```

The deny list is checked first and always wins, and an override of a built-in subcommand
only takes effect when it is stricter: `subcommands={"git": {"push": "read"}}` is ignored.
An extension can add restrictions and new subcommands; it can never loosen a built-in rule.

## Verification runs project code

The project's own checks (`AGENTS.md`, `pyproject.toml`, `package.json`) are code of the
project. Running them needs the same execution authority as `bash`: with execution `off`
nothing runs —not even the baseline— and the run is reported as inconclusive with reason
`execution_not_authorized`; with `ask` the exact commands are shown once per run, before
anything changes. The plan is fixed when the run starts and the model cannot add to it.

Command strings are split into argv per platform: POSIX-mode `shlex` elsewhere, non-POSIX
mode on Windows, because a POSIX split treats the backslash as an escape and would silently
turn `C:\repo\run.py` into `C:reporun.py`.

Timeouts are mandatory and bounded. Cancellation kills the whole process tree — the process
group on POSIX, `taskkill /T` on Windows — so a cancelled command leaves no orphan behind.

## What Athena cannot do

There is no push, pull, fetch, merge, rebase, tag, publish, pull-request or deploy tool, and
those commands are classified R4 (including `git tag`, which used to be R3). The capability does not exist, so the model cannot request
it and no human can be persuaded to approve it through Athena.

`git_commit` exists and is R3: it stages the named workspace paths and records one local
commit, only after an explicit approval, and it cannot publish the result.
