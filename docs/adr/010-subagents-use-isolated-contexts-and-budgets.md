# ADR-010: Subagents use isolated contexts and budgets

- Status: Accepted
- Date: 2026-08-18

## Context

Future subagents could otherwise inherit excessive authority, context, or unbounded work.

## Decision

Every future subagent receives an isolated context, explicit workspace/permissions, and a
finite budget. Results cross back through structured runtime contracts and events.

## Consequences

Subagent implementation is intentionally absent from H0. Later work must not treat a
subagent as a shared conversation or an unrestricted child process.

## Implementation status (H6, 2026-08-19)

Implemented as exactly three profiles — Explorer, Coder, Verifier — in
`athena/subagents.py`. Each receives a brief rather than a transcript, a registry
containing only its own toolset, its own permission policy, its own budget and
timeout, and a cancellation token chained to the parent's. No profile can delegate.
See [ADR-015](015-three-delegates-not-a-swarm.md).
