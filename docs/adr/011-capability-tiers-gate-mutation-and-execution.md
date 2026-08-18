# ADR-011: Capability tiers gate mutation and execution

- Status: Accepted
- Date: 2026-08-18

## Context

H2 gives Athena the ability to change files and run commands. A per-tool boolean such as
"is this tool dangerous" is not enough: `git status` and `git push` are the same tool, and
`pytest` and `pip install` are the same executable family. The decision has to depend on
the concrete action, not on the tool's name.

## Decision

Every permission request declares a capability tier, and the `PermissionEngine` maps tiers
to decisions:

- **R0** read-only local access resolves to ALLOW.
- **R1** writing inside the workspace resolves to ALLOW only under an explicit local
  policy grant, and otherwise to ASK.
- **R2** local execution is policy-driven on the same terms.
- **R3** external, irreversible, cost-bearing or history-recording actions always resolve
  to ASK, whatever the policy says.
- **R4** actions outside policy always resolve to DENY, and are never offered to a human.

A request that claims R0 while declaring side effects is refused outright: the only
unconditional ALLOW must be honest about itself. Destructive requests escalate to ASK even
when the corresponding tier has been granted.

`BashTool` classifies the parsed executable, its arguments and its working directory before
any permission question is asked, so `git status`, `git commit` and `git push` reach the
engine as R2, R3 and R4 respectively. Commands containing shell metacharacters are rejected
during validation rather than classified, because a shell would make the argv meaningless.

An ASK is resolved through a `PermissionPrompt` port owned by the interface, and grants a
single use. There is no "always allow".

## Consequences

Adding a capability means classifying it, not adding a flag. Remote and publishing actions
have no tool and no allowed command, so the model cannot request one at all. The cost is a
policy table that must be maintained deliberately: an unknown executable is R4 by default,
which is safe but will occasionally refuse something legitimate until it is classified.
