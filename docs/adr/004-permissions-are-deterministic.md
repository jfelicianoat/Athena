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
