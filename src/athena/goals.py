"""El objetivo de un run puede cambiar mientras el run pasa, y eso tiene consecuencias.

Hasta ahora el objetivo era un `str` que se pasaba una vez y no se volvía a mirar. Es lo
razonable mientras un run dure segundos; deja de serlo en cuanto dura minutos y alguien
que está mirando se da cuenta de que pidió mal las cosas. La alternativa que había era
cancelar y volver a empezar, tirando todo lo que ya se había averiguado.

Revisar el objetivo son tres problemas distintos y aquí se separan a propósito:

1. **Quién gana si dos personas revisan a la vez.** Cada revisión dice sobre cuál se
   escribe (`base_revision`). Si el objetivo ya no es ese, se rechaza. No se fusiona ni se
   pisa: fusionar dos encargos en prosa no lo sabe hacer nadie, y pisar convierte el
   trabajo de alguien en un cambio que nunca vio.
2. **Cuándo se aplica.** Sólo entre iteraciones. Un objetivo que cambiase con una tool a
   medias dejaría al modelo con un resultado pedido por un encargo y una pregunta hecha
   por otro.
3. **Qué pasa con lo ya hecho.** Y esta es la parte que no se puede callar: **la evidencia
   obtenida bajo una revisión no demuestra la siguiente.** Una verificación que pasó
   contra el objetivo de ayer no dice nada del de ahora, así que se marca caduca en vez de
   heredarse. Heredarla sería la forma más barata de dar por bueno un trabajo que nadie
   pidió.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from athena.errors import GoalConflict, ToolValidationError
from athena.types import JSONObject


@dataclass(frozen=True, slots=True)
class Goal:
    """Lo que se pidió, en su versión número `revision`."""

    text: str
    revision: int = 1
    #: Por qué se cambió, dicho por quien lo cambió. Vacío en la primera.
    reason: str = ""
    revised_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ToolValidationError("Un objetivo no puede estar vacío")
        if self.revision < 1:
            raise ValueError("Las revisiones empiezan en 1")

    def to_json(self) -> JSONObject:
        return {
            "text": self.text,
            "revision": self.revision,
            "reason": self.reason,
            "revised_at": self.revised_at.isoformat(),
        }


class GoalBoard:
    """El objetivo vigente de un run y todo lo que fue antes.

    Guarda la historia y no sólo el último: un run que acabó haciendo algo distinto de lo
    que se le pidió al principio es explicable si consta cuándo cambió y por qué, e
    inexplicable si sólo consta el final.
    """

    def __init__(self, objective: str) -> None:
        self._history: list[Goal] = [Goal(objective)]
        #: Si la revisión vigente ya la vio quien está trabajando. Lo pone el bucle al
        #: recogerla, no el cliente al escribirla: que un cambio esté escrito no quiere
        #: decir que haya llegado a tiempo de cambiar nada.
        self._delivered = 1

    @property
    def current(self) -> Goal:
        return self._history[-1]

    @property
    def revision(self) -> int:
        return self.current.revision

    def history(self) -> tuple[Goal, ...]:
        return tuple(self._history)

    def revise(self, text: str, *, base_revision: int, reason: str = "") -> Goal:
        """Cambiar el objetivo, diciendo sobre cuál se escribe.

        `base_revision` no es burocracia: es la única forma de que dos personas mirando el
        mismo run no se pisen sin enterarse. Quien llega con una base vieja recibe un
        conflicto y el objetivo actual, y decide con eso a la vista.
        """
        actual = self.current
        if base_revision != actual.revision:
            raise GoalConflict(
                f"El objetivo va por la revisión {actual.revision} y esta se escribió "
                f"sobre la {base_revision}",
                details={"current_revision": actual.revision, "current": actual.text},
            )
        propuesto = text.strip()
        if not propuesto:
            raise ToolValidationError("Un objetivo no puede estar vacío")
        if propuesto == actual.text:
            # Nada cambió, así que nada se revisa. Crear una revisión igual a la anterior
            # haría que el bucle interrumpiera su trabajo para que le contasen lo que ya
            # sabía, y dejaría en el registro un cambio que no lo fue.
            return actual
        siguiente = Goal(propuesto, revision=actual.revision + 1, reason=reason.strip())
        self._history.append(siguiente)
        return siguiente

    # -- entrega -----------------------------------------------------------

    @property
    def pending(self) -> Goal | None:
        """La revisión que aún no ha recogido quien trabaja, si la hay."""
        return self.current if self._delivered != self.revision else None

    def take(self) -> Goal | None:
        """Recoger la revisión pendiente. Devuelve `None` si no había ninguna."""
        nueva = self.pending
        if nueva is not None:
            self._delivered = nueva.revision
        return nueva

    def to_json(self) -> JSONObject:
        return {
            "current": self.current.to_json(),
            "revisions": len(self._history),
            "history": [goal.to_json() for goal in self._history],
        }


def announcement(previous: Goal, current: Goal) -> str:
    """Cómo se le cuenta al modelo que el encargo ha cambiado.

    Se le dice el objetivo nuevo **y** que lo anterior ya no manda, porque un modelo al
    que sólo se le añade una instrucción nueva tiende a hacer las dos: la vieja sigue en
    su transcripción y nada le dijo que dejara de valer.
    """
    razon = f" Motivo: {current.reason}" if current.reason else ""
    return (
        f"The goal has been revised while you were working (revision {current.revision})."
        f"{razon}\n\n"
        f"It is now:\n{current.text}\n\n"
        f"The previous goal no longer applies. What you did for it is not wasted — keep "
        f"anything still useful — but stop working towards it. Previous goal was:\n"
        f"{previous.text}"
    )


def summarise(history: Sequence[Goal]) -> JSONObject:
    """Lo que hay que saber de un objetivo que cambió, para quien lo lea después."""
    return {
        "revision": history[-1].revision if history else 0,
        "revised": len(history) > 1,
        "reasons": [goal.reason for goal in history[1:] if goal.reason],
    }


__all__ = ["Goal", "GoalBoard", "announcement", "summarise"]
