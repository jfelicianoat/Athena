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


class ToolContractError(ToolExecutionError):
    """La tool devolvio algo que no es lo que declaro devolver.

    Es un fallo de ejecucion y no de validacion: los argumentos estaban bien, quien
    incumplio fue la tool. Distinguirlo importa porque la recuperacion de un argumento
    malo es reformular la llamada, y aqui reformularla no arreglaria nada.
    """

    code = "tool_contract_error"


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


class VerificationInconclusive(AthenaRuntimeError):
    """No se pudo comprobar nada, ni a favor ni en contra.

    Deliberadamente NO hereda de `VerificationFailure`. Un fallo de verificacion dice que
    el cambio esta mal y se responde devolviendo evidencia para que alguien lo arregle;
    esto dice que no hay evidencia, y devolver la que no existe no arregla nada. Si
    heredase, la politica de recuperacion las trataria igual y gastaria ciclos de
    reparacion sobre una maquina rota o un proyecto sin checks.
    """

    code = "verification_inconclusive"


class BudgetExceededError(AthenaRuntimeError):
    code = "budget_exceeded"


class FatalRuntimeError(AthenaRuntimeError):
    code = "fatal_runtime_error"
