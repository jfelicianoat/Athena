# ADR-009: Large outputs are externalized

- Status: Accepted
- Date: 2026-08-18

## Context

Injecting unbounded tool output into model context wastes tokens and can cause overflow.

## Decision

Tools declare maximum inline size and result-size policy. `ToolResultStore` returns a typed
reference for externalized content; the runtime will summarize or selectively retrieve it.

## Consequences

Tool results remain addressable without becoming conversation history. A concrete store is
deferred.

## Implementation status (H4, 2026-08-18)

`SqliteToolResultStore` makes references durable for a documented retention window
(seven days by default) and raises `ToolResultUnavailableError` when one has expired,
is missing, or fails its checksum. Compaction reduces an already-externalized result
in the transcript to its reference.
