# ADR-003: Tools are capability contracts

- Status: Accepted
- Date: 2026-08-18

## Context

Tools mediate effects and therefore need more than a callable name and prompt description.

## Decision

Every tool declares schemas, validation, permission requirements, risk, cancellation,
read/destructive/concurrency behavior, result-size policy, and load metadata. The deferred
load policy is contractual only in H0.

## Consequences

The runtime can inspect and govern tools without executing them. Concrete tools remain out
of scope.

## Implementation status (H1, 2026-08-18)

H1 added concrete read-only tools (`athena/repository_tools.py`) that implement this
contract. Deferred loading is still declared and not implemented, so the H0 statement
about `load_policy` remains accurate; "concrete tools remain out of scope" describes
H0 only.
