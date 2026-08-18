# ADR-005: Workspace is a security boundary

- Status: Accepted
- Date: 2026-08-18

## Context

Filesystem-like capabilities are unsafe without an explicit allowed scope.

## Decision

Every tool context and permission request carries an explicit workspace identity and root.
Future resource resolution must reject paths outside that boundary before execution.

## Consequences

Ambient process directories are not authority. Real filesystem access and boundary
enforcement are deferred beyond H0.

## Implementation status (H1, 2026-08-18)

H1 implemented canonical resolution and boundary enforcement in `athena/workspace.py`,
including symlink-escape rejection. The deferral recorded above applied to H0 only.
