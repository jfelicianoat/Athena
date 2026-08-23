"""Deteccion de estancamiento: el mismo turno repetido no es trabajo.

Un bucle de agente no tiene forma nativa de notar que esta dando vueltas. Mientras el
modelo devuelva tool calls bien formadas, el runtime las admite, las ejecuta y vuelve a
preguntar; si el modelo pide exactamente lo mismo y recibe exactamente lo mismo, el ciclo
se repite hasta que se acaba el presupuesto. Visto desde fuera parece trabajo: hay
llamadas, hay resultados, hay eventos. Lo que no hay es progreso.

Esto se vio en un run real: tres iteraciones seguidas listando el mismo directorio y
leyendo el mismo README, con resultados identicos byte a byte, y el run murio despues por
`budget_exceeded` — un diagnostico que manda a subir el limite cuando el limite no era el
problema.

La firma incluye el resultado a proposito. Repetir una llamada cuyo resultado cambia es
legitimo (esperar a que aparezca un fichero, releer algo que se acaba de escribir); lo que
no lleva a ninguna parte es repetir la pregunta *y* recibir la misma respuesta.

La politica es en dos tiempos y no en uno: primero se le dice al modelo lo que esta
haciendo, porque un modelo pequeño muchas veces sale del bucle en cuanto se lo nombran, y
solo si insiste se corta. Cortar a la primera tirara runs que iban a recuperarse solos.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from athena.types import JSONObject, JSONValue

#: Turnos identicos consecutivos tras los cuales se avisa al modelo.
DEFAULT_WARN_AFTER = 2
#: Turnos identicos consecutivos tras los cuales se abandona el run.
DEFAULT_FAIL_AFTER = 3


class ProgressVerdict(StrEnum):
    """Que hacer con el turno que se acaba de observar."""

    #: El turno aporta algo nuevo. Seguir.
    PROGRESSING = "progressing"
    #: Se repite. Decirselo al modelo y darle otra vuelta.
    REPEATING = "repeating"
    #: Sigue repitiendose despues de avisarle. No va a salir solo.
    STUCK = "stuck"


def _stable_digest(value: JSONValue) -> str:
    """Huella de un valor JSON que no depende del orden de las claves."""
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


#: Claves del resultado que cambian entre turnos sin que cambie nada de lo ocurrido.
#: `call_id` lo pone el modelo y es nuevo cada vez; `reference_uri` es el recibo del
#: almacen, y guardar dos veces la misma salida da dos recibos distintos.
_VOLATILE_KEYS = frozenset({"call_id", "reference_uri"})


def _comparable(payload: JSONObject | None) -> JSONValue:
    """El resultado sin lo que cambia por construccion.

    Esto costo un run real. La firma incluia el payload entero, y el payload lleva el
    `call_id`: como el modelo genera uno nuevo en cada turno, dos turnos identicos nunca
    daban la misma huella y el detector no salto jamas. Se vio con
    `nemotron-3.5-lightning:30b`, que lanzo `glob **/test_cola.py` seis veces seguidas
    —misma llamada, mismo resultado— y agoto el presupuesto sin que nadie lo parase.

    Los tests no lo cazaron porque el proveedor de mentira reutilizaba el mismo id en
    todos los turnos, asi que las huellas coincidian por accidente.
    """
    if payload is None:
        return None
    return {key: value for key, value in payload.items() if key not in _VOLATILE_KEYS}


def turn_signature(
    calls: Sequence[tuple[str, JSONObject]],
    payloads: Iterable[JSONObject | None],
) -> str:
    """Huella de un turno: que se pidio y que se recibio.

    Los pares se ordenan antes de resumirlos porque el orden en que el modelo enumera sus
    tool calls no es una decision suya que signifique nada: pedir A y B no es un turno
    distinto de pedir B y A, y dejar que lo parezca es justo la forma mas facil de que un
    bucle pase desapercibido.

    Lo que identifica cada llamada es su nombre y sus argumentos, nunca su `call_id`: ese
    es nuevo en cada turno por construccion, y colarlo —en la llamada o en el resultado—
    hace que dos turnos identicos no lo parezcan nunca.
    """
    pairs = [
        _stable_digest([name, arguments, _comparable(payload)])
        for (name, arguments), payload in zip(calls, payloads, strict=True)
    ]
    return _stable_digest(sorted(pairs))


@dataclass(slots=True)
class NoProgressDetector:
    """Cuenta cuantas veces seguidas se ha repetido el mismo turno.

    Es deliberadamente amnesico salvo para la racha en curso: un turno distinto reinicia
    la cuenta. Lo que se persigue es el bucle cerrado, no que una tool se repita a lo largo
    de un run, que es normal y a menudo correcto.
    """

    warn_after: int = DEFAULT_WARN_AFTER
    fail_after: int = DEFAULT_FAIL_AFTER
    _signature: str | None = field(default=None, init=False)
    _repeats: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.warn_after < 1:
            raise ValueError("warn_after must be at least 1")
        if self.fail_after <= self.warn_after:
            raise ValueError("fail_after must be greater than warn_after")

    @property
    def repeats(self) -> int:
        """Repeticiones consecutivas del turno actual, sin contar la primera vez."""
        return self._repeats

    def observe(self, signature: str) -> ProgressVerdict:
        if signature != self._signature:
            self._signature = signature
            self._repeats = 0
            return ProgressVerdict.PROGRESSING
        self._repeats += 1
        if self._repeats >= self.fail_after:
            return ProgressVerdict.STUCK
        if self._repeats >= self.warn_after:
            return ProgressVerdict.REPEATING
        return ProgressVerdict.PROGRESSING

    def reset(self) -> None:
        """Olvidar la racha. Lo usa el bucle cuando el turno deja de ser comparable."""
        self._signature = None
        self._repeats = 0


__all__ = [
    "DEFAULT_FAIL_AFTER",
    "DEFAULT_WARN_AFTER",
    "NoProgressDetector",
    "ProgressVerdict",
    "turn_signature",
]
