"""Que pide quien arranca un run: capacidades, limites y forma de ejecucion.

Las capacidades se declaran, no se deducen. Un run que no pidio escribir no
recibe la herramienta de escribir, y eso se decide aqui.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from athena.adapters.service.orchestration import (
    ExecutionMode,
)
from athena.errors import ToolValidationError

_SUBSCRIBER_QUEUE_LIMIT = 512

#: How many recent events a run keeps so a client that drops can pick up where it left off.
#:
#: Bounded on purpose. An unbounded journal would make the runtime's memory a function of
#: how long a client stays away, which is not a number the runtime gets to choose. When a
#: client has been gone longer than this, the snapshot is still there — it just costs a
#: full resynchronisation instead of a replay.
_REPLAY_BUFFER_SIZE = 256

#: How long `start` waits for the loop to announce itself before giving up.
_START_TIMEOUT_SECONDS = 10.0


class CapabilityMode(StrEnum):
    OFF = "off"
    ASK = "ask"
    ALLOW = "allow"


#: Las que cambian el workspace, y las que ejecutan algo. Por nombre, porque es lo que el
#: perfil declara: deducirlo de `is_read_only()` mezclaria dos preguntas —que hace una
#: tool y quien puede usarla— que el resto del sistema mantiene separadas a proposito.
def _paths(raw: object) -> tuple[str, ...]:
    """Rutas relativas pedidas por el cliente, saneadas aqui y comprobadas mas tarde.

    Aqui solo se exige que sean cadenas: si estan dentro del workspace lo decide el
    propio workspace al resolverlas, que es el unico sitio donde esa pregunta tiene una
    respuesta fiable.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise ToolValidationError("deliverables must be a list of paths")
    return tuple(item.strip() for item in raw if isinstance(item, str) and item.strip())


_MUTATING = frozenset({"write_file", "edit_file", "git_commit"})
_EXECUTING = frozenset({"bash"})


@dataclass(frozen=True, slots=True)
class RunOptions:
    """What a client may choose about a run. Defaults follow ADR-017 §14: it asks."""

    writes: CapabilityMode = CapabilityMode.ASK
    execution: CapabilityMode = CapabilityMode.ASK
    max_iterations: int = 12
    max_repair_cycles: int = 2
    session_timeout_seconds: float = 900.0
    #: Qué se hace con el objetivo. `auto` —lo normal— deja que decidan las señales del
    #: repositorio y no una casilla de la interfaz; `hierarchical` y `direct` fijan el
    #: camino para quien necesita saber cuál corrió.
    execution_mode: ExecutionMode = ExecutionMode.AUTO
    #: Para que se esta usando Athena en este run. Vacio = el de por defecto del
    #: despliegue. Un nombre desconocido es un 400, no una caida al de por defecto:
    #: quien pide `documents` y recibe el de software no se entera hasta que Athena
    #: intenta ejecutar los tests de una carpeta de textos.
    profile: str = ""
    #: Los entregables que se esperan, si quien encarga el trabajo los sabe nombrar. Sin
    #: ellos la evidencia por artefactos comprueba lo que el run dice haber escrito, que
    #: es mas debil; con ellos comprueba lo que se pidio.
    deliverables: tuple[str, ...] = ()
    #: El modelo pedido para este run. Vacio = el de por defecto del despliegue. Igual que
    #: `profile`: un nombre que el despliegue no ofrece es un 400, no una caida silenciosa
    #: al de por defecto — quien elige un modelo y recibe otro no se entera hasta que el
    #: trabajo sale mal, y para entonces ya ha pagado el run entero.
    model: str = ""

    @classmethod
    def from_json(cls, payload: Mapping[str, object]) -> RunOptions:
        def mode(key: str, default: CapabilityMode) -> CapabilityMode:
            raw = payload.get(key)
            if raw is None:
                return default
            try:
                return CapabilityMode(str(raw))
            except ValueError as exc:
                raise ToolValidationError(f"{key} must be one of off, ask, allow") from exc

        def positive_int(key: str, default: int) -> int:
            raw = payload.get(key, default)
            if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
                raise ToolValidationError(f"{key} must be a positive integer")
            return raw

        raw_mode = payload.get("execution_mode", ExecutionMode.AUTO.value)
        try:
            # `chosen`, no `mode`: ahí arriba `mode` ya es el lector de capacidades, y
            # taparlo dejaba el parseo de writes/exec devolviendo un enum de otra cosa.
            chosen = ExecutionMode(str(raw_mode))
        except ValueError as exc:
            raise ToolValidationError(
                "execution_mode must be one of auto, hierarchical, direct"
            ) from exc

        timeout = payload.get("session_timeout_seconds", 900.0)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ToolValidationError("session_timeout_seconds must be a positive number")

        return cls(
            writes=mode("writes", CapabilityMode.ASK),
            execution=mode("exec", CapabilityMode.ASK),
            max_iterations=positive_int("max_iterations", 12),
            max_repair_cycles=positive_int("max_repair_cycles", 2)
            if payload.get("max_repair_cycles") is not None
            else 2,
            session_timeout_seconds=float(timeout),
            execution_mode=chosen,
            profile=str(payload.get("profile") or ""),
            deliverables=_paths(payload.get("deliverables")),
            model=str(payload.get("model") or "").strip(),
        )
