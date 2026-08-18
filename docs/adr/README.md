# Architecture Decision Records

These accepted records freeze Athena's H0 architectural boundaries. Changing an accepted
decision requires a superseding ADR; implementation convenience alone is not sufficient.

The decisions are frozen; their wording is not a status report. Where a later milestone
implemented what H0 deferred, the ADR carries an "Implementation status" section instead
of rewritten history.

- [ADR-001: Athena owns the agent runtime](001-athena-owns-agent-runtime.md)
- [ADR-002: AI_Broker is a ModelProvider](002-ai-broker-is-a-model-provider.md)
- [ADR-003: Tools are capability contracts](003-tools-are-capability-contracts.md)
- [ADR-004: Permissions are deterministic](004-permissions-are-deterministic.md)
- [ADR-005: Workspace is a security boundary](005-workspace-is-a-security-boundary.md)
- [ADR-006: Completion requires verification](006-completion-requires-verification.md)
- [ADR-007: Runtime communicates through events](007-runtime-communicates-through-events.md)
- [ADR-008: Operational state is structured](008-operational-state-is-structured.md)
- [ADR-009: Large outputs are externalized](009-large-outputs-are-externalized.md)
- ADR-010:
  [Subagents use isolated contexts and budgets](010-subagents-use-isolated-contexts-and-budgets.md)
- ADR-011:
  [Capability tiers gate mutation and execution](011-capability-tiers-gate-mutation-and-execution.md)
