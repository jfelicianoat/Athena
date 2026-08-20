# ADR-004: Permissions are deterministic

- Status: Accepted
- Date: 2026-08-18

## Context

A probabilistic model cannot be the authority for its own requested side effects.

## Decision

Tools produce structured `PermissionRequest` values. A `PermissionEngine` alone returns
`ALLOW`, `ASK`, or `DENY` from deterministic policy and trusted user decisions.

## Consequences

Model output never grants authority. Permission evaluation is independently testable and
auditable.

## Implementation status (H2, 2026-08-18)

H2 implements the decision as `PolicyPermissionEngine`, with the capability tiers described in [ADR-011](011-capability-tiers-gate-mutation-and-execution.md). An ASK is
resolved by a `PermissionPrompt` owned by the interface and is valid for a single call.

## Implementation status (H5, 2026-08-19)

Hooks and skills were added without weakening this decision: a hook can only BLOCK,
never ALLOW, and a skill grants no capability at all. The `PermissionEngine` remains
the single authority. See [ADR-014](014-extensions-restrict-but-never-grant.md).
