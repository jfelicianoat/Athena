# ADR-002: AI_Broker is a ModelProvider, not Athena runtime

- Status: Accepted
- Date: 2026-08-18

## Context

AI_Broker may add routing value, but making it mandatory would prevent standalone Athena
deployments and couple core code to an external system.

## Decision

AI_Broker may be implemented only as an optional adapter behind `ModelProvider`. Athena core
contains no AI_Broker SDK dependency and remains usable with other providers.

## Consequences

All provider-specific configuration and translation stay outside the core package.
