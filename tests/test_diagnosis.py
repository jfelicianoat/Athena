"""Reading a failure before asking anyone to fix it.

The repair loop used to hand a wall of pytest output to a small model and hope. These tests
are about the cases where hoping goes wrong: a missing package that looks like a code bug, a
machine problem that looks like a test problem, a failure that was already there before
Athena touched anything.

The last of those is the one that matters most. A run that spends its budget fixing
somebody else's pre-existing breakage has done real work and achieved nothing.
"""

from __future__ import annotations

import pytest

from athena.diagnosis import (
    FailureDiagnosis,
    FailureKind,
    InconclusiveReason,
    diagnose,
    inconclusive_reason,
)
from athena.verification import (
    CheckKind,
    CheckOutcome,
    VerificationEvidence,
    VerificationResult,
    VerificationStatus,
)


def check(
    name: str = "tests",
    *,
    passed: bool = False,
    output: str = "",
    exit_code: int | None = 1,
) -> CheckOutcome:
    return CheckOutcome(
        name=name,
        kind=CheckKind.TEST,
        command=f"pytest {name}",
        passed=passed,
        exit_code=exit_code,
        duration_seconds=1.0,
        output_tail=output,
    )


def failed(summary: str = "a check failed") -> VerificationResult:
    return VerificationResult(
        status=VerificationStatus.FAILED,
        evidence=(VerificationEvidence("check", summary),),
        summary=summary,
    )


# ------------------------------------------------------------------- telling kinds apart


def test_an_assertion_failure_is_a_code_error() -> None:
    outcome = check(
        output="E       AssertionError: assert 1 == 2\nFAILED tests/test_calc.py::test_add"
    )

    diagnosis = diagnose(failed(), [outcome])

    assert diagnosis.kind is FailureKind.CODE_ERROR
    assert "not change the test" in diagnosis.guidance


def test_a_missing_package_is_not_a_code_error() -> None:
    """The misreading that costs the most.

    `ModuleNotFoundError` looks like a broken import, and a model told "fix the code" will
    obligingly edit an import statement that was correct all along.
    """
    outcome = check(output="ModuleNotFoundError: No module named 'requests'")

    diagnosis = diagnose(failed(), [outcome])

    assert diagnosis.kind is FailureKind.DEPENDENCY_ERROR
    assert "Editing source will not fix this" in diagnosis.guidance
    assert not diagnosis.is_worth_repairing


def test_a_dependency_signature_outranks_a_code_signature() -> None:
    # Real output contains both: the import fails, then a hundred tests report errors.
    # Reading it as a code error sends the model to the wrong file.
    outcome = check(
        output=(
            "ImportError: cannot import name 'settings' from 'app.config'\n"
            "E       AttributeError: module has no attribute 'settings'\n"
            "12 failed"
        )
    )

    diagnosis = diagnose(failed(), [outcome])

    assert diagnosis.kind is FailureKind.DEPENDENCY_ERROR


def test_a_permission_problem_is_the_machine_not_the_change() -> None:
    outcome = check(output="PermissionError: [Errno 13] Permission denied: '/var/log/app.log'")

    diagnosis = diagnose(failed(), [outcome])

    assert diagnosis.kind is FailureKind.ENVIRONMENT_ERROR
    assert not diagnosis.is_worth_repairing


def test_a_missing_interpreter_is_an_environment_problem() -> None:
    outcome = check(output="'pytest' is not recognized as an internal or external command")

    diagnosis = diagnose(failed(), [outcome])

    assert diagnosis.kind is FailureKind.ENVIRONMENT_ERROR


def test_a_broken_test_file_is_not_broken_code() -> None:
    outcome = check(output="ERROR collecting tests/test_auth.py\nfixture 'client' not found")

    diagnosis = diagnose(failed(), [outcome])

    assert diagnosis.kind is FailureKind.TEST_ERROR
    assert "test file itself" in diagnosis.guidance
    assert diagnosis.is_worth_repairing


def test_a_check_that_never_ran_is_a_tool_failure() -> None:
    """A check that ran and disagreed is not the same as one that could not start.

    The exit code is the only place that distinction survives.
    """
    outcome = check(output="", exit_code=None)

    diagnosis = diagnose(failed(), [outcome])

    assert diagnosis.kind is FailureKind.TOOL_FAILURE


def test_unrecognised_output_says_so_instead_of_guessing() -> None:
    """Confident nonsense is worse than the undirected repair it would replace."""
    outcome = check(output="something went wrong in a way nobody has seen before")

    diagnosis = diagnose(failed(), [outcome])

    assert diagnosis.kind is FailureKind.UNKNOWN
    assert "not recognised" in diagnosis.guidance
    assert diagnosis.is_worth_repairing, "unknown still routes to the old behaviour"


# -------------------------------------------------------------------- the baseline wins


def test_a_failure_that_predates_the_change_is_not_the_change_s_fault() -> None:
    """The most expensive misreading.

    A run that spends its budget fixing somebody else's pre-existing breakage has done
    real work and achieved nothing towards its objective.
    """
    outcome = check("legacy", output="E       AssertionError: assert 1 == 2")

    diagnosis = diagnose(failed(), [outcome], preexisting=["legacy"])

    assert diagnosis.kind is FailureKind.PREEXISTING_FAILURE
    assert not diagnosis.attributed_to_change
    assert not diagnosis.is_worth_repairing


def test_the_baseline_outranks_every_pattern() -> None:
    # Whatever the output looks like, a check that was already failing is not evidence
    # about the change.
    outcome = check("legacy", output="ModuleNotFoundError: No module named 'x'")

    diagnosis = diagnose(failed(), [outcome], preexisting=["legacy"])

    assert diagnosis.kind is FailureKind.PREEXISTING_FAILURE


def test_one_new_failure_among_old_ones_is_still_the_change_s_fault() -> None:
    """The case that makes the rule safe.

    "All of them were already broken" is very different from "one of them is new", and
    excusing the second would let a change hide behind existing breakage.
    """
    old = check("legacy", output="E       AssertionError: old")
    new = check("fresh", output="E       AssertionError: assert 1 == 2")

    diagnosis = diagnose(failed(), [old, new], preexisting=["legacy"])

    assert diagnosis.kind is FailureKind.CODE_ERROR
    assert diagnosis.attributed_to_change
    assert set(diagnosis.failing_checks) == {"legacy", "fresh"}


# ------------------------------------------------------------- proving nothing vs failing


def test_an_inconclusive_result_is_not_a_failure() -> None:
    """A run whose checks could not execute has not failed verification.

    It has failed to verify, and reporting the first as the second blames a change for a
    broken machine.
    """
    result = VerificationResult(
        status=VerificationStatus.INCONCLUSIVE,
        evidence=(VerificationEvidence("plan", "no checks"),),
        summary="the project defines no checks",
    )

    diagnosis = diagnose(result, [])

    assert diagnosis.kind is FailureKind.INSUFFICIENT_EVIDENCE
    assert "nothing is proven" in diagnosis.guidance
    assert not diagnosis.is_worth_repairing


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("ModuleNotFoundError: No module named 'x'", InconclusiveReason.DEPENDENCY_MISSING),
        ("PermissionError: [Errno 13] denied", InconclusiveReason.ENVIRONMENT_INCOMPLETE),
        ("E       AssertionError: assert 1 == 2", None),
    ],
    ids=["missing-package", "bad-machine", "genuinely-broken"],
)
def test_inconclusive_is_reachable_for_more_than_an_empty_plan(
    output: str, expected: InconclusiveReason | None
) -> None:
    """It used to be reachable only through an empty plan, which made it look like a
    configuration problem. A half-installed environment leaves the same hole in the
    evidence and is not the change's fault either."""
    diagnosis = diagnose(failed(), [check(output=output)])

    assert inconclusive_reason(diagnosis) is expected


def test_a_check_that_could_not_run_leaves_no_evidence_either_way() -> None:
    diagnosis = diagnose(failed(), [check(exit_code=None)])

    assert inconclusive_reason(diagnosis) is InconclusiveReason.TOOL_UNAVAILABLE


# ------------------------------------------------------------------ what the model sees


def test_the_repair_request_leads_with_the_diagnosis() -> None:
    """Diagnosis first, then evidence.

    A model that reads the conclusion before the wall of output is being told what to look
    for rather than asked to find it.
    """
    outcome = check(output="E       AssertionError: assert add(1, 2) == 3")

    rendered = diagnose(failed(), [outcome]).render()

    assert rendered.startswith("Verification failed:")
    assert "Do not change the test" in rendered
    assert "AssertionError" in rendered
    assert rendered.index("Do not change the test") < rendered.index("AssertionError")


def test_the_excerpt_keeps_the_end_where_the_summary_is() -> None:
    # A test runner puts its summary last. The first two thousand characters of a pytest
    # run are almost always the part nobody needs.
    outcome = check(output="noise\n" * 2_000 + "FAILED tests/test_calc.py::test_add")

    rendered = diagnose(failed(), [outcome]).render()

    assert "FAILED tests/test_calc.py::test_add" in rendered
    assert len(rendered) < 4_000


def test_the_diagnosis_names_which_checks_failed() -> None:
    diagnosis = diagnose(
        failed(),
        [check("lint", output="E AssertionError"), check("types", output="E AssertionError")],
    )

    assert set(diagnosis.failing_checks) == {"lint", "types"}


def test_a_failure_with_no_named_check_is_not_invented_into_one() -> None:
    diagnosis = diagnose(failed("something failed"), [])

    assert diagnosis.kind is FailureKind.UNKNOWN
    assert diagnosis.failing_checks == ()


def test_a_diagnosis_is_serialisable_for_an_event() -> None:
    payload = diagnose(failed(), [check(output="E AssertionError: x")]).to_json()

    assert payload["kind"] == FailureKind.CODE_ERROR.value
    assert isinstance(payload["failing_checks"], list)
    assert payload["attributed_to_change"] is True


def test_only_things_a_model_could_fix_are_worth_another_cycle() -> None:
    """Spending a repair cycle on a full disk is how a run burns its budget looking busy."""
    fixable = {FailureKind.CODE_ERROR, FailureKind.TEST_ERROR, FailureKind.UNKNOWN}

    for kind in FailureKind:
        diagnosis = FailureDiagnosis(kind=kind, summary="", guidance="")
        assert diagnosis.is_worth_repairing is (kind in fixable)
