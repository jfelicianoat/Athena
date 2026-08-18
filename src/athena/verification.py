"""Evidence-based completion contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from athena.cancellation import CancellationToken
from athena.state import SessionState
from athena.types import JSONObject


class VerificationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    kind: str
    summary: str
    reference: str | None = None
    metadata: JSONObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VerificationResult:
    status: VerificationStatus
    evidence: tuple[VerificationEvidence, ...]
    summary: str

    @property
    def permits_completion(self) -> bool:
        return self.status is VerificationStatus.PASSED and bool(self.evidence)


@runtime_checkable
class VerificationPolicy(Protocol):
    async def verify(
        self, state: SessionState, cancellation: CancellationToken
    ) -> VerificationResult: ...


class LoopCompletionVerificationPolicy:
    """H1 proof that the loop reached a defined, tool-free terminal response."""

    async def verify(
        self, state: SessionState, cancellation: CancellationToken
    ) -> VerificationResult:
        cancellation.raise_if_cancelled()
        final_response = state.attributes.get("final_response")
        finish_reason = state.attributes.get("finish_reason")
        pending_calls = state.agent.active_tool_call_ids
        passed = (
            isinstance(final_response, str)
            and bool(final_response.strip())
            and finish_reason in ("stop", "done")
            and not pending_calls
        )
        if not passed:
            return VerificationResult(
                VerificationStatus.FAILED,
                (),
                "The loop did not reach a defined terminal response.",
            )
        return VerificationResult(
            VerificationStatus.PASSED,
            (
                VerificationEvidence(
                    kind="agent-loop",
                    summary="Model stopped with no pending tool calls.",
                ),
            ),
            "Agent loop completion verified.",
        )
