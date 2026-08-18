# ADR-008: Operational state is structured

- Status: Accepted
- Date: 2026-08-18

## Context

Conversation text is incomplete, ambiguous, and unsuitable for recovery or enforcement.

## Decision

Session, agent, active operation, budget, and failure state use typed structures. Required
state will be persisted by a later storage design; chat history is contextual input only.

## Consequences

Recovery and interfaces can reason over explicit state. Persistent memory remains out of
scope for H0.

## Implementation status (H3, 2026-08-18)

`WorkingState` holds the objective, constraints, plan, current step, facts, files
examined and modified, commands run, decisions, typed errors, verification outcome and
remaining work. Updates go through validated methods, and the state is serialised into
the session rather than being reconstructed from the transcript. Persistence across
processes remains out of scope.
