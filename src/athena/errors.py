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


class WorkspacePathNotFoundError(AthenaRuntimeError):
    """Una ruta que esta DENTRO del workspace pero no existe.

    Deliberadamente NO hereda de `WorkspaceBoundaryError`. La regla 8 hace del workspace
    un limite de seguridad, y un error de limite tiene que significar que alguien intento
    cruzarlo; si tambien significa «te equivocaste de nombre de fichero», la senal deja de
    distinguir un escape real de una errata y la politica de recuperacion aborta las dos
    igual. Una ruta que no existe es un hecho sobre el arbol, y el modelo puede corregirlo
    en cuanto se le cuente la verdad.
    """

    code = "workspace_path_not_found"


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


class ModelAuthenticationError(AthenaRuntimeError):
    """Access must be restored before inference can continue; do not retry or reroute."""

    code = "model_authentication_required"


class ModelAuthenticationBackendError(ModelAuthenticationError):
    """The provider's credential store is unavailable; changing the token cannot help."""

    code = "model_authentication_backend_unavailable"


class ModelStreamingUnsupportedError(ModelPermanentError):
    code = "model_streaming_unsupported"


class ContextOverflowError(ModelPermanentError):
    code = "context_overflow"


class VerificationFailure(AthenaRuntimeError):
    code = "verification_failure"


class GoalConflict(AthenaRuntimeError):
    """Alguien reviso el objetivo sobre una version que ya no era la vigente.

    No se fusiona ni se pisa. Fusionar dos encargos escritos en prosa no lo sabe hacer
    nadie, y pisar convierte el trabajo de otro en un cambio que nunca vio. Se devuelve el
    conflicto con el objetivo actual, y quien llego tarde decide con eso delante.
    """

    code = "goal_conflict"


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


class ApprovalAbandonedError(AthenaRuntimeError):
    """Peticiones de aprobacion seguidas sin respuesta: no hay nadie al otro lado.

    Vive en el nucleo, no en el adaptador que la lanza, porque la politica de recuperacion
    tiene que poder nombrarla y el nucleo no importa de `adapters/`. Que la levante el
    prompt remoto es un detalle del transporte; lo que significa —«nadie va a contestar»—
    es una decision de runtime.
    """

    code = "approval_abandoned"


class NoProgressError(AthenaRuntimeError):
    """El run repite el mismo turno y recibe el mismo resultado: no avanza.

    Deliberadamente NO es `BudgetExceededError`. Los dos acaban el run, pero cuentan cosas
    distintas y llevan a arreglos distintos: quedarse sin presupuesto sugiere subir el
    limite, y estancarse dice que subirlo solo compraria mas vueltas iguales.
    """

    code = "no_progress"


class FatalRuntimeError(AthenaRuntimeError):
    code = "fatal_runtime_error"
