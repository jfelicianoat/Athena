"""El bucle de agente: de un objetivo a un resultado verificado.

Partido por fase, que es como se lee y como se cambia:

- `tipos`         — configuracion, resultado y estado vivo de la ejecucion.
- `base`          — dependencias inyectadas y transformaciones puras.
- `sesion`        — hooks, persistencia, habilidades y contexto.
- `ejecucion`     — olas de herramientas, admision y registro.
- `finalizacion`  — progreso, verificacion, reparacion y rescate.
- `bucle`         — `run`, `resume` y una vuelta completa.

Quien usa Athena importa `AgentLoop` de este modulo, como siempre.
"""

from __future__ import annotations

from athena.agent_loop.bucle import AgentLoop
from athena.agent_loop.tipos import (
    AgentLoopConfig,
    AgentRunResult,
    AgentRunStatus,
    _RunData,
)

__all__ = [
    "AgentLoop",
    "AgentLoopConfig",
    "AgentRunResult",
    "AgentRunStatus",
    # Las pruebas construyen el estado vivo de una ejecucion directamente:
    # se reexporta para no obligarlas a conocer el reparto interno.
    "_RunData",
]
