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
- ADR-012:
  [Verification owns completion, and recovery is explicit per error](012-verification-owns-completion-and-recovery-is-explicit.md)
- ADR-013:
  [Sessions persist outside the conversation](013-sessions-persist-outside-the-conversation.md)
- ADR-014:
  [Extensions restrict, but never grant](014-extensions-restrict-but-never-grant.md)
- ADR-015:
  [Three delegates, not a swarm](015-three-delegates-not-a-swarm.md)
- ADR-016:
  [Parallelism is earned, not assumed](016-parallelism-is-earned-not-assumed.md)
- ADR-017:
  [ChatyGPT as an external Athena client](017-chatygpt-as-an-external-athena-client.md)
- ADR-018:
  [An approval is a decision with a record](018-approval-is-a-decision-with-a-record.md)
- ADR-019:
  [A channel is an adapter, not a feature](019-a-channel-is-an-adapter-not-a-feature.md)
- ADR-020:
  [Identity is claimed, never inferred](020-identity-is-claimed-never-inferred.md)
- ADR-021:
  [Resume by event id, and create runs idempotently](021-resume-by-event-id-and-idempotent-creation.md)
- ADR-022:
  [A plan has to be earned, and then it has to survive validation](022-plans-are-earned-and-validated.md)
- ADR-023:
  [The executor joins what already existed](023-the-executor-joins-what-already-existed.md)
- ADR-024:
  [Execution mode is asked for; the shape is reported](024-execution-mode-is-asked-for-and-the-shape-is-reported.md)
- ADR-025:
  [A fact belongs to a run, not to the session that published it](025-a-fact-belongs-to-a-run-not-to-a-session.md)
- ADR-026:
  [A result has one truth and two projections](026-a-result-has-one-truth-and-two-projections.md)
- ADR-027:
  ["Not verified" is not "verified wrong"](027-not-verified-is-not-verified-wrong.md)
- ADR-028:
  [A profile declares what counts as done](028-a-profile-declares-what-counts-as-done.md)
