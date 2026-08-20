"""Reading a verification failure before asking the model to fix it.

The repair loop has been undirected since H3: verification fails, the evidence digest goes
back to the model, and the model is trusted to work out what happened. That is a lot to ask
of a small model looking at a wall of pytest output — and when it guesses wrong, it edits
the wrong thing and the next cycle looks identical.

This classifies the failure first. Not to solve it, and not to write the fix: to say which
*kind* of problem it is, so the repair request can be specific about what to look at and
what not to touch. "The import failed" and "the assertion failed" call for different next
moves, and a runtime that can tell them apart can say so.

Classification is deterministic, pattern-based and openly imperfect. Every rule is a
recognisable signature in real tool output; anything unrecognised comes back as
`UNKNOWN`, which routes to the same undirected repair that happened before. Guessing
confidently would be worse than the problem it replaces.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from athena.types import JSONObject
from athena.verification import (
    CheckKind,
    CheckOutcome,
    VerificationResult,
    VerificationStatus,
)


class FailureKind(StrEnum):
    """What went wrong, in the terms a next step would be chosen in."""

    #: The change under test is wrong. The ordinary case, and the one repair is for.
    CODE_ERROR = "code_error"
    #: The test is wrong, or tests something that deliberately changed.
    TEST_ERROR = "test_error"
    #: The machine, not the code: a missing interpreter, a permission, a full disk.
    ENVIRONMENT_ERROR = "environment_error"
    #: Something is not installed. Editing code will not help.
    DEPENDENCY_ERROR = "dependency_error"
    #: Already broken before Athena touched anything.
    PREEXISTING_FAILURE = "preexisting_failure"
    #: The check itself could not run.
    TOOL_FAILURE = "tool_failure"
    #: Nothing ran, so nothing was proven either way.
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    #: Recognised as a failure and nothing more. Routes to undirected repair.
    UNKNOWN = "unknown"


#: Signatures, in priority order. First match wins, so the more specific patterns come
#: first — a `ModuleNotFoundError` is a dependency problem before it is a code problem,
#: and reading it the other way sends the model editing an import that was fine.
_SIGNATURES: tuple[tuple[FailureKind, re.Pattern[str], str], ...] = (
    (
        FailureKind.DEPENDENCY_ERROR,
        re.compile(
            r"ModuleNotFoundError|ImportError: cannot import name|"
            r"No module named|could not be resolved|"
            r"npm ERR!.*(?:ENOENT|missing)|error: package .* not found|"
            r"unresolved import",
            re.IGNORECASE,
        ),
        "something the code imports is not installed",
    ),
    (
        FailureKind.ENVIRONMENT_ERROR,
        re.compile(
            r"PermissionError|Errno 13|Access is denied|No space left on device|"
            r"command not found|is not recognized as an internal or external command|"
            r"OSError: \[Errno|connection refused|could not connect",
            re.IGNORECASE,
        ),
        "the machine could not do what the check asked of it",
    ),
    (
        FailureKind.TEST_ERROR,
        re.compile(
            r"fixture .* not found|"
            r"ERROR collecting|"
            r"INTERNALERROR|"
            r"in test_\w+.*\n.*(?:SyntaxError|IndentationError)",
            re.IGNORECASE,
        ),
        "the test itself did not run correctly",
    ),
    (
        FailureKind.CODE_ERROR,
        re.compile(
            r"AssertionError|assert \w|"
            r"TypeError|ValueError|AttributeError|KeyError|IndexError|"
            r"NameError|ZeroDivisionError|"
            r"FAILED .*::|\d+ failed",
            re.IGNORECASE,
        ),
        "the code under test did not behave as the checks expect",
    ),
)

#: What to tell the model to do about each kind, and — as importantly — what not to.
_GUIDANCE: dict[FailureKind, str] = {
    FailureKind.CODE_ERROR: (
        "Read the failing assertion and fix the code it exercises. Do not change the "
        "test to match the code."
    ),
    FailureKind.TEST_ERROR: (
        "The test could not run as written. Look at the test file itself before touching "
        "the code it covers."
    ),
    FailureKind.DEPENDENCY_ERROR: (
        "Something is missing from the environment, not from the code. Editing source "
        "will not fix this; report what is missing."
    ),
    FailureKind.ENVIRONMENT_ERROR: (
        "This is the machine, not the change. Report it rather than working around it."
    ),
    FailureKind.PREEXISTING_FAILURE: (
        "This was already failing before any change was made. Leave it alone unless it is "
        "part of the objective, and say so in the result."
    ),
    FailureKind.TOOL_FAILURE: (
        "The check could not be executed at all. Report the command and its output."
    ),
    FailureKind.INSUFFICIENT_EVIDENCE: (
        "Nothing ran, so nothing is proven. Do not treat this as either success or failure."
    ),
    FailureKind.UNKNOWN: (
        "The failure was not recognised. Read the output below and decide what it means "
        "before changing anything."
    ),
}


@dataclass(frozen=True, slots=True)
class FailureDiagnosis:
    """One reading of a failure, with the evidence that produced it."""

    kind: FailureKind
    summary: str
    guidance: str
    failing_checks: tuple[str, ...] = ()
    excerpt: str = ""
    #: True when the failure predates the change, from the baseline rather than a pattern.
    attributed_to_change: bool = True
    signals: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_worth_repairing(self) -> bool:
        """Whether another repair cycle could plausibly help.

        A missing package and a full disk are not things the model can fix by editing
        code, and spending a cycle letting it try is how a run burns its budget looking
        busy.
        """
        return self.kind in (
            FailureKind.CODE_ERROR,
            FailureKind.TEST_ERROR,
            FailureKind.UNKNOWN,
        )

    def to_json(self) -> JSONObject:
        return {
            "kind": self.kind.value,
            "summary": self.summary,
            "guidance": self.guidance,
            "failing_checks": list(self.failing_checks),
            "attributed_to_change": self.attributed_to_change,
            "signals": list(self.signals),
        }

    def render(self) -> str:
        """What the repair cycle actually sends. Diagnosis first, then the evidence."""
        parts = [f"Verification failed: {self.summary}", self.guidance]
        if self.failing_checks:
            parts.append("Failing checks: " + ", ".join(self.failing_checks))
        if self.excerpt:
            parts.append("Output:\n" + self.excerpt)
        return "\n\n".join(parts)


#: How much failing output the model sees. Enough to read the error; not so much that the
#: repair request costs more context than the work.
_EXCERPT_CHARS = 2_000


def diagnose(
    result: VerificationResult,
    checks: Sequence[CheckOutcome] = (),
    *,
    preexisting: Sequence[str] = (),
) -> FailureDiagnosis:
    """Read a failed verification and say what kind of failure it is.

    `preexisting` comes from the baseline and outranks every pattern: a check that was
    already failing before the change is not evidence about the change, whatever its
    output looks like. Reading it as a code error would send the model fixing somebody
    else's bug on Athena's budget.
    """
    if result.status is VerificationStatus.INCONCLUSIVE:
        return FailureDiagnosis(
            kind=FailureKind.INSUFFICIENT_EVIDENCE,
            summary=result.summary or "Nothing was proven either way.",
            guidance=_GUIDANCE[FailureKind.INSUFFICIENT_EVIDENCE],
        )

    failing = [check for check in checks if not check.passed]
    if not failing:
        return FailureDiagnosis(
            kind=FailureKind.UNKNOWN,
            summary=result.summary or "Verification failed without naming a check.",
            guidance=_GUIDANCE[FailureKind.UNKNOWN],
        )

    already_broken = set(preexisting)
    if already_broken and all(check.name in already_broken for check in failing):
        return FailureDiagnosis(
            kind=FailureKind.PREEXISTING_FAILURE,
            summary="Every failing check was already failing before the change.",
            guidance=_GUIDANCE[FailureKind.PREEXISTING_FAILURE],
            failing_checks=tuple(check.name for check in failing),
            attributed_to_change=False,
        )

    # A check that could not run at all is a different problem from one that ran and
    # disagreed, and the exit code is the only place that distinction survives.
    unrunnable = [check for check in failing if check.exit_code is None]
    if unrunnable and len(unrunnable) == len(failing):
        return FailureDiagnosis(
            kind=FailureKind.TOOL_FAILURE,
            summary="The verification command could not be executed.",
            guidance=_GUIDANCE[FailureKind.TOOL_FAILURE],
            failing_checks=tuple(check.name for check in unrunnable),
            excerpt=_excerpt(failing),
        )

    combined = "\n".join(check.output_tail for check in failing)
    for kind, pattern, description in _SIGNATURES:
        match = pattern.search(combined)
        if match is not None:
            return FailureDiagnosis(
                kind=kind,
                summary=description,
                guidance=_GUIDANCE[kind],
                failing_checks=tuple(check.name for check in failing),
                excerpt=_excerpt(failing),
                signals=(match.group(0).strip()[:120],),
            )

    return FailureDiagnosis(
        kind=FailureKind.UNKNOWN,
        summary=result.summary or "A check failed for a reason Athena does not recognise.",
        guidance=_GUIDANCE[FailureKind.UNKNOWN],
        failing_checks=tuple(check.name for check in failing),
        excerpt=_excerpt(failing),
    )


def diagnose_result(result: VerificationResult) -> FailureDiagnosis:
    """Diagnose from the result alone, which is all a caller usually has.

    The evidence already carries everything needed — the outcome of each check and its
    `attribution`, which is the baseline's verdict on whether the change caused it. Asking
    the caller to supply the baseline separately would invite the two to disagree.
    """
    checks: list[CheckOutcome] = []
    preexisting: list[str] = []
    for item in result.evidence:
        metadata = item.metadata
        name = metadata.get("name")
        if not isinstance(name, str):
            continue
        passed = metadata.get("passed")
        exit_code = metadata.get("exit_code")
        tail = metadata.get("output_tail")
        checks.append(
            CheckOutcome(
                name=name,
                kind=CheckKind.TEST,
                command=str(metadata.get("command", "")),
                passed=bool(passed),
                exit_code=exit_code if isinstance(exit_code, int) else None,
                duration_seconds=0.0,
                output_tail=tail if isinstance(tail, str) else "",
            )
        )
        if metadata.get("attribution") == "pre_existing":
            preexisting.append(name)
    return diagnose(result, checks, preexisting=preexisting)


def _excerpt(checks: Sequence[CheckOutcome]) -> str:
    """The tail of the failing output, bounded.

    The tail rather than the head: a test runner puts its summary last, and the first two
    thousand characters of a pytest run are almost always the parts nobody needs.
    """
    joined = "\n\n".join(
        f"$ {check.command}\n{check.output_tail.strip()}" for check in checks
    ).strip()
    if len(joined) <= _EXCERPT_CHARS:
        return joined
    return "…\n" + joined[-_EXCERPT_CHARS:]


class InconclusiveReason(StrEnum):
    """Why nothing could be proven.

    `INCONCLUSIVE` was previously reachable only through an empty plan, which made it look
    like a configuration problem. It is not: an unavailable service, a half-installed
    environment or a check that timed out all leave the same hole in the evidence, and
    each of them is a fact worth reporting differently.
    """

    NO_CHECKS_DEFINED = "no_checks_defined"
    TOOL_UNAVAILABLE = "tool_unavailable"
    DEPENDENCY_MISSING = "dependency_missing"
    ENVIRONMENT_INCOMPLETE = "environment_incomplete"
    AMBIGUOUS_RESULT = "ambiguous_result"
    PARTIAL_VERIFICATION = "partial_verification"
    EXTERNAL_SERVICE_UNAVAILABLE = "external_service_unavailable"


#: Which diagnoses mean "we could not tell", as opposed to "it is broken".
_INCONCLUSIVE_KINDS: dict[FailureKind, InconclusiveReason] = {
    FailureKind.DEPENDENCY_ERROR: InconclusiveReason.DEPENDENCY_MISSING,
    FailureKind.ENVIRONMENT_ERROR: InconclusiveReason.ENVIRONMENT_INCOMPLETE,
    FailureKind.TOOL_FAILURE: InconclusiveReason.TOOL_UNAVAILABLE,
    FailureKind.INSUFFICIENT_EVIDENCE: InconclusiveReason.NO_CHECKS_DEFINED,
}


def inconclusive_reason(diagnosis: FailureDiagnosis) -> InconclusiveReason | None:
    """Whether a failure is really an absence of evidence, and which kind.

    This is the distinction that matters most for honesty. A run whose checks could not
    execute has not failed verification — it has failed to verify, and reporting the first
    as though it were the second blames a change for a broken machine.
    """
    return _INCONCLUSIVE_KINDS.get(diagnosis.kind)


__all__ = [
    "FailureDiagnosis",
    "FailureKind",
    "InconclusiveReason",
    "diagnose",
    "diagnose_result",
    "inconclusive_reason",
]
