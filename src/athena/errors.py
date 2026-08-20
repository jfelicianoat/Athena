"""Stable, typed error taxonomy for runtime recovery decisions."""

from __future__ import annotations

from typing import ClassVar

from athena.types import JSONObject


class AthenaRuntimeError(Exception):
    """Base for expected runtime failures with machine-readable semantics."""

    code: ClassVar[str] = "runtime_error"
    retryable: ClassVar[bool] = False

    def __init__(self, message: str, *, details: JSONObject | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ToolValidationError(AthenaRuntimeError):
    code = "tool_validation_error"


class PermissionDeniedError(AthenaRuntimeError):
    code = "permission_denied"


class WorkspaceBoundaryError(PermissionDeniedError):
    code = "workspace_boundary_error"


class ToolExecutionError(AthenaRuntimeError):
    code = "tool_execution_error"


class ProcessTimeoutError(ToolExecutionError):
    code = "process_timeout"
    retryable = True


class ProcessCancelledError(ToolExecutionError):
    code = "process_cancelled"


class ToolResultUnavailableError(ToolExecutionError):
    """A tool-result reference outlived its store, or its payload no longer matches."""

    code = "tool_result_unavailable"


class CancellationError(AthenaRuntimeError):
    code = "cancellation_requested"


class ModelTransientError(AthenaRuntimeError):
    code = "model_transient_error"
    retryable = True


class ModelPermanentError(AthenaRuntimeError):
    code = "model_permanent_error"


class ModelStreamingUnsupportedError(ModelPermanentError):
    code = "model_streaming_unsupported"


class ContextOverflowError(ModelPermanentError):
    code = "context_overflow"


class VerificationFailure(AthenaRuntimeError):
    code = "verification_failure"


class BudgetExceededError(AthenaRuntimeError):
    code = "budget_exceeded"


class FatalRuntimeError(AthenaRuntimeError):
    code = "fatal_runtime_error"
