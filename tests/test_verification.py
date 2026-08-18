from __future__ import annotations

from athena.verification import (
    VerificationEvidence,
    VerificationResult,
    VerificationStatus,
)


def test_completion_requires_both_pass_and_evidence() -> None:
    no_evidence = VerificationResult(VerificationStatus.PASSED, (), "claimed")
    verified = VerificationResult(
        VerificationStatus.PASSED,
        (VerificationEvidence(kind="test", summary="12 tests passed"),),
        "verified",
    )

    assert not no_evidence.permits_completion
    assert verified.permits_completion
