from __future__ import annotations

import pytest

from athena.errors import (
    AthenaRuntimeError,
    BudgetExceededError,
    ContextOverflowError,
    FatalRuntimeError,
    ModelPermanentError,
    ModelTransientError,
    PermissionDeniedError,
    ProcessCancelledError,
    ProcessTimeoutError,
    ToolExecutionError,
    ToolValidationError,
    VerificationFailure,
    WorkspaceBoundaryError,
)


@pytest.mark.parametrize(
    "error_type",
    [
        ToolValidationError,
        PermissionDeniedError,
        WorkspaceBoundaryError,
        ToolExecutionError,
        ProcessTimeoutError,
        ProcessCancelledError,
        ModelTransientError,
        ModelPermanentError,
        ContextOverflowError,
        VerificationFailure,
        BudgetExceededError,
        FatalRuntimeError,
    ],
)
def test_typed_errors_share_machine_readable_base(
    error_type: type[AthenaRuntimeError],
) -> None:
    error = error_type("failure", details={"operation": "test"})

    assert isinstance(error, AthenaRuntimeError)
    assert error.code != "runtime_error"
    assert error.message == "failure"
    assert error.details == {"operation": "test"}


def test_only_explicitly_transient_errors_are_retryable() -> None:
    assert ModelTransientError.retryable
    assert ProcessTimeoutError.retryable
    assert not ModelPermanentError.retryable
    assert not ToolExecutionError.retryable
