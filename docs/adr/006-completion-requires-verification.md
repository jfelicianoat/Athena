# ADR-006: Completion requires verification

- Status: Accepted
- Date: 2026-08-18

## Context

A model's claim that work is complete is not evidence that the requested outcome exists.

## Decision

A `VerificationPolicy` produces a structured `VerificationResult`. Completion is permitted
only for a passing result containing evidence.

## Consequences

Later agent loops must enter verification before completion and retain evidence for users
and interfaces.
