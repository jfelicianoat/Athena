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

- **R2 read**: `ls`, `cat`, `grep`, `git status`, `git diff`, `git log`, `pip list`, …
- **R2 build**: `pytest`, `ruff`, `mypy`, `python -m <allow-listed module>`, `npm test`,
  `cargo build`, …
- **R3**: installs, migrations, `rm`, `mv`, `chmod`, `git commit`, `git add`, …
- **R4**: `sudo`, `curl`, `wget`, `ssh`, `git push`, `git pull`, `git merge`, `git reset`,
  `git clean`, shells, and anything not covered by the policy.

An unknown executable is R4. The default is refusal, not permission.

A deployment can classify additional executables without editing the module:

```python
CommandPolicy(
    build_commands=("gradle", "bazel"),
    subcommands={"just": {"test": "build", "deploy": "forbidden"}},
)
```

The deny list is checked first and always wins, so an extension can add restrictions but
can never turn a forbidden command into an allowed one.

Command strings are split into argv per platform: POSIX-mode `shlex` elsewhere, non-POSIX
mode on Windows, because a POSIX split treats the backslash as an escape and would silently
turn `C:\repo\run.py` into `C:reporun.py`.

Timeouts are mandatory and bounded. Cancellation kills the whole process tree — the process
group on POSIX, `taskkill /T` on Windows — so a cancelled command leaves no orphan behind.

## What Athena cannot do

There is no push, pull, fetch, merge, rebase, tag, publish, pull-request or deploy tool, and
those commands are classified R4. The capability does not exist, so the model cannot request
it and no human can be persuaded to approve it through Athena.

`git_commit` exists and is R3: it stages the named workspace paths and records one local
commit, only after an explicit approval, and it cannot publish the result.
