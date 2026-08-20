# ADR-014: Extensions restrict, but never grant

- Status: Accepted
- Date: 2026-08-19

## Context

Athena needs to be extensible — hooks for policy, skills for procedure, MCP for third-party
capability — without becoming a pile of integrations wearing a runtime as a hat. Every
extension mechanism is also an attack surface: each one is a new way for something outside
the reviewed core to influence what Athena does.

There is a second, quieter cost. A model shown every tool schema on every turn pays for all
of them forever. `ToolSpec` has carried `load_policy` and `search_hint` since H0 precisely
so this could be fixed later without touching a single tool.

## Decision

**A hook can refuse; it cannot approve.** `HookResult` has exactly two decisions, CONTINUE
and BLOCK. There is no ALLOW, so no hook can rescue a call the `PermissionEngine` refused.
Adding a restriction is always safe; a hook able to remove one would be a second,
unaudited permission system. A *blocking* hook that itself crashes fails closed — a guard
you can disable by breaking it is not a guard — while an observational hook that crashes is
recorded and stepped over.

**A skill is knowledge, not capability.** A `SkillManifest` carries instructions;
`required_toolsets` is a precondition, not a request. A skill whose toolsets are absent is
dropped, never accommodated: the answer to "this skill needs a tool you do not have" is to
not use the skill. Selecting one registers nothing and widens no tier.

**Deferred tools are discovered, not preloaded.** `ToolRegistry.definitions()` returns core
tools plus whatever this run has revealed. `ToolSearchTool` searches only deferred tools —
returning a core tool would spend a turn revealing what was never hidden — and what it
finds becomes visible on the next turn, for that run only.

**MCP lives behind an adapter and is trusted accordingly.** `athena.mcp` contains no
transport; `McpClient` is a Protocol. Every remote tool is wrapped so it is subject to the
same rules a native one is: its declared schema is enforced locally before anything leaves
the process, it gets a `PermissionRequest` defaulting to R3 (so every call is an ASK), it
runs under a mandatory timeout, it honours cancellation, and an oversized result is
externalized like any other. Athena's own error taxonomy passes back through untouched —
a cancelled MCP call must stay a cancellation — while anything the server raises is wrapped
as a typed tool error, because a remote process is not trusted to speak our error language.

## Consequences

Extending Athena cannot make it more powerful by accident: hooks only narrow, skills only
advise, MCP only asks. The cost is that genuinely-needed capability must be added
deliberately, by registering a tool and choosing its tier — which is the point.

Deferred loading costs a turn: the model must search before it can call. That is the trade
for not paying for every schema on every turn, and it is why `search_hint` exists.

## Postscript: a defect this work surfaced

Building the hook tests exposed an intermittent failure that had nothing to do with hooks.
A `.pyc` header stores the source mtime truncated to whole seconds. Athena edits quickly and
often leaves a file the same length, so a bytecode cache written by one verification run
could still look valid to the next — meaning a check could pass judgement on the *previous*
version of the code. For a runtime whose entire claim is evidence-based completion, that is
worse than not verifying at all. Verification now runs with `PYTHONDONTWRITEBYTECODE=1`, so
Athena can never lay that trap for itself, and two regression tests pin the behaviour.
