# ADR-007: Runtime communicates through events

- Status: Accepted
- Date: 2026-08-18

## Context

CLI, IDE, chat, API, and future interfaces need consistent observations without becoming
part of runtime control flow.

## Decision

The runtime publishes typed lifecycle events through `EventBus`. An ordered in-process bus
is sufficient initially; distributed transport is not required.

## Consequences

Interfaces subscribe and render. They do not own agent logic or call internal phases to
drive execution.
