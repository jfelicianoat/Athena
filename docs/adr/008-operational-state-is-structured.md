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
