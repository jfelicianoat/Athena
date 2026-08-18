# ADR-001: Athena owns the agent runtime

- Status: Accepted
- Date: 2026-08-18

## Context

Delegating orchestration to a provider SDK would make Athena's control flow, recovery,
cancellation, and verification depend on that provider.

## Decision

Athena owns its future `AgentLoop` and all runtime policy. Providers perform inference only.
No functional loop is implemented in H0.

## Consequences

Provider replacement cannot change orchestration semantics. Athena must implement and test
its own loop in a later milestone.

## Implementation status (H1, 2026-08-18)

H1 implemented that loop inside Athena (`athena/agent_loop.py`), with its own
budget, retry, cancellation, and verification policy. The decision is unchanged: the
sentence above records the H0 state, not the current one.
