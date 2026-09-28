"""Quien autoriza ejecutar las comprobaciones del proyecto.

Una comprobacion es codigo del proyecto: `pytest` importa sus modulos, `npm test`
ejecuta un script que el proyecto escribio. Que un comando se clasifique como R2 dice
que tipo de accion es, no que alguien la haya autorizado. Antes la verificacion llamaba
a `run_process` directamente, asi que un run con la ejecucion local **desactivada**
ejecutaba igualmente los tests del repositorio dos veces —linea base y verificacion—
sin preguntar a nadie (auditoria A01).

Ahora la verificacion pide permiso por la misma via que las herramientas: el mismo
`PermissionEngine` decide y el mismo `PermissionPrompt` pregunta. Con la ejecucion
desactivada no se pregunta ni se ejecuta. Con `ask` se pregunta **una vez por run**,
enseñando la lista exacta de comandos del plan: el plan se fija al construir la
politica y el modelo no puede añadir nada, asi que la aprobacion cubre esos comandos y
ningun otro. Preguntar por cada comando en la linea base y otra vez en cada ciclo de
reparacion convertiria la aprobacion en ruido que se acepta sin leer.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from athena.async_utils import await_cancellable
from athena.cancellation import CancellationToken
from athena.events import EventBus, EventName, PermissionEvent
from athena.permissions import (
    DenyingPermissionPrompt,
    PermissionDecision,
    PermissionEngine,
    PermissionPrompt,
    PermissionRequest,
    RiskLevel,
    RiskTier,
)
from athena.types import JSONObject
from athena.verification.contratos import VerificationCheck
from athena.workspace import Workspace

#: El nombre con el que la peticion llega a las interfaces. No es una tool del modelo:
#: el modelo no puede pedirla ni verla en su catalogo.
VERIFICATION_TOOL_NAME = "verification"


@runtime_checkable
class CheckAuthorizer(Protocol):
    """Decide si se pueden ejecutar las comprobaciones del plan en este run."""

    async def authorize(
        self,
        checks: Sequence[VerificationCheck],
        workspace: Workspace,
        cancellation: CancellationToken,
        *,
        session_id: str,
    ) -> bool: ...


class ChecksAlwaysAuthorized:
    """Para pruebas y para quien ya concedio la ejecucion de forma explicita.

    Tiene nombre propio a proposito: pasar esto es una decision visible en el codigo,
    no un defecto que se hereda sin mirarlo.
    """

    async def authorize(
        self,
        checks: Sequence[VerificationCheck],
        workspace: Workspace,
        cancellation: CancellationToken,
        *,
        session_id: str,
    ) -> bool:
        del checks, workspace, session_id
        cancellation.raise_if_cancelled()
        return True


class ChecksNeverAuthorized:
    """La ejecucion local esta desactivada: no se pregunta ni se ejecuta nada."""

    async def authorize(
        self,
        checks: Sequence[VerificationCheck],
        workspace: Workspace,
        cancellation: CancellationToken,
        *,
        session_id: str,
    ) -> bool:
        del checks, workspace, session_id
        cancellation.raise_if_cancelled()
        return False


class PermissionCheckAuthorizer:
    """La misma autoridad que las herramientas, aplicada al plan de verificacion.

    `enabled=False` es la ejecucion desactivada: ni siquiera se consulta al motor, igual
    que un run sin ejecucion no recibe `bash` en su catalogo. Con `enabled=True` decide
    el motor (ALLOW con `allow`, ASK con `ask`) y un ASK lo contesta la persona.

    La decision se recuerda solo para este plan y este run. Es la unica memoria, y es
    segura porque el plan no cambia despues de construirse.
    """

    def __init__(
        self,
        engine: PermissionEngine,
        *,
        enabled: bool,
        prompt: PermissionPrompt | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self.engine = engine
        self.enabled = enabled
        self.prompt = prompt or DenyingPermissionPrompt()
        self.event_bus = event_bus
        self._decided: bool | None = None

    async def authorize(
        self,
        checks: Sequence[VerificationCheck],
        workspace: Workspace,
        cancellation: CancellationToken,
        *,
        session_id: str,
    ) -> bool:
        cancellation.raise_if_cancelled()
        if not self.enabled or not checks:
            return False
        if self._decided is not None:
            return self._decided
        commands = tuple(check.rendered for check in checks)
        request = PermissionRequest(
            tool_name=VERIFICATION_TOOL_NAME,
            operation="run_project_checks",
            workspace=workspace,
            risk=RiskLevel.MEDIUM,
            tier=RiskTier.R2_LOCAL_EXECUTION,
            is_read_only=False,
            is_destructive=False,
            action="Comprobar el trabajo con los comandos del proyecto",
            reason=(
                "Athena solo da un trabajo por bueno con evidencia. Estos comandos son "
                "los que declara el propio proyecto; no los eligió el modelo."
            ),
            possible_effects=(
                "Ejecuta código del proyecto (tests, lint o build) en tu equipo",
                "Se ejecutan antes de cambiar nada y otra vez al terminar",
                *(f"$ {command}" for command in commands),
            ),
            resources=(str(workspace.root),),
            arguments={"commands": list(commands)},
        )
        await self._publish(
            EventName.PERMISSION_REQUESTED,
            session_id,
            {
                "tool_name": VERIFICATION_TOOL_NAME,
                "risk": request.risk.value,
                "tier": request.tier.value,
                "action": request.action,
                "reason": request.reason,
                "possible_effects": list(request.possible_effects),
            },
        )
        decision = self.engine.decide(request)
        asked = decision is PermissionDecision.ASK
        if asked:
            answer = await await_cancellable(self.prompt.confirm(request), cancellation)
            decision = answer if answer is PermissionDecision.ALLOW else PermissionDecision.DENY
        await self._publish(
            EventName.PERMISSION_RESOLVED,
            session_id,
            {"tool_name": VERIFICATION_TOOL_NAME, "decision": decision.value, "asked": asked},
        )
        self._decided = decision is PermissionDecision.ALLOW
        return self._decided

    async def _publish(self, name: EventName, session_id: str, payload: JSONObject) -> None:
        if self.event_bus is None:
            return
        await self.event_bus.publish(PermissionEvent(name, session_id, payload))


__all__ = [
    "VERIFICATION_TOOL_NAME",
    "CheckAuthorizer",
    "ChecksAlwaysAuthorized",
    "ChecksNeverAuthorized",
    "PermissionCheckAuthorizer",
]
