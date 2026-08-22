"""Declarar lo que se puede hacer, y exigirlo antes de contar con ello.

Athena ya tenía `ModelCapabilities`: todo proveedor la declara y `ProviderRouter` la
agrega. Lo que no había era nadie que la consultara. Una capacidad declarada y nunca
exigida no es una garantía, es un comentario — y el fallo que produce llega tarde, lejos y
sin atribución: una petición con herramientas a un proveedor que no las admite no falla al
enviarse, falla más adelante, como una respuesta rara.

La regla es una sola: **si una operación requiere algo que el proveedor no ofrece, se
rechaza antes de intentarla**. No se ejecuta «a ver si cuela», y no se degrada en silencio
a una versión sin esa garantía. Ese silencio es lo único que este módulo existe para
impedir.

No es un marco general de capacidades. Los tipos concretos —`ModelCapabilities`,
`SubagentCapabilities`— siguen viviendo donde tienen sentido; aquí sólo está el vocabulario
de exigir: qué se pide, con qué fuerza, y qué se responde cuando no se puede.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from athena.errors import AthenaRuntimeError
from athena.models import ModelCapabilities
from athena.types import JSONObject


class CapabilityStrength(StrEnum):
    """Con qué fuerza se pide algo.

    La distinción es de seguridad, no de estilo. Marcar como opcional un requisito real
    —aislamiento, cancelación— es exactamente cómo se cuela una degradación silenciosa con
    apariencia de configuración.
    """

    #: Su ausencia impide ejecutar. No hay versión aceptable sin esto.
    REQUIRED = "required"
    #: Se preferiría tenerlo, y su ausencia no invalida el resultado.
    PREFERRED = "preferred"
    #: Se usará si está; nada cambia si no.
    OPTIONAL = "optional"


@dataclass(frozen=True, slots=True)
class CapabilityRequirement:
    """Una exigencia concreta sobre un proveedor.

    `minimum` existe porque no todo es un sí o un no: una ventana de contexto es un número,
    y «admite contexto» no dice si admite el que hace falta. Un booleano ahí obligaría a
    descubrir la insuficiencia gastando una llamada.
    """

    name: str
    strength: CapabilityStrength = CapabilityStrength.REQUIRED
    minimum: int | None = None

    def met_by(self, capabilities: ModelCapabilities) -> bool:
        value = getattr(capabilities, self.name, None)
        if value is None:
            # Lo no declarado se lee como ausente. Un proveedor que calla no obtiene el
            # beneficio de la duda: la duda la pagaría quien contó con la garantía.
            return False
        if self.minimum is not None:
            return isinstance(value, int) and not isinstance(value, bool) and value >= self.minimum
        return bool(value)


@dataclass(frozen=True, slots=True)
class CapabilityMatch:
    """Si un proveedor sirve, y qué le falta si no."""

    provider: str
    missing_required: tuple[str, ...] = ()
    missing_preferred: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return not self.missing_required

    def to_json(self) -> JSONObject:
        return {
            "provider": self.provider,
            "usable": self.usable,
            "missing_required": list(self.missing_required),
            "missing_preferred": list(self.missing_preferred),
        }


class UnsupportedCapabilityError(AthenaRuntimeError):
    """Se pidió una garantía que el proveedor elegido no da.

    Se lanza en vez de continuar. Continuar sería ejecutar sin la garantía y confiar en
    que nadie la necesitaba de verdad, que es la definición del problema.
    """

    code = "unsupported_capability"


class CapabilityResolutionError(AthenaRuntimeError):
    """Ningún candidato cumple lo requerido."""

    code = "capability_resolution_error"


def match(
    name: str,
    capabilities: ModelCapabilities,
    requirements: tuple[CapabilityRequirement, ...],
) -> CapabilityMatch:
    """Comparar un proveedor con lo que se le pide, sin decidir nada todavía."""
    faltan_requeridas = tuple(
        requirement.name
        for requirement in requirements
        if requirement.strength is CapabilityStrength.REQUIRED
        and not requirement.met_by(capabilities)
    )
    faltan_preferidas = tuple(
        requirement.name
        for requirement in requirements
        if requirement.strength is CapabilityStrength.PREFERRED
        and not requirement.met_by(capabilities)
    )
    return CapabilityMatch(name, faltan_requeridas, faltan_preferidas)


def require(
    name: str,
    capabilities: ModelCapabilities,
    requirements: tuple[CapabilityRequirement, ...],
) -> CapabilityMatch:
    """Exigir, y fallar ruidosamente si no se cumple.

    Devuelve la comparación cuando sirve —con las preferidas que falten, que son
    información y no un problema— y lanza cuando no.
    """
    resultado = match(name, capabilities, requirements)
    if not resultado.usable:
        raise UnsupportedCapabilityError(
            f"{name} does not offer what this operation requires",
            details=resultado.to_json(),
        )
    return resultado


def requirements_for(
    *, offers_tools: bool = False, needs_schema: bool = False
) -> tuple[CapabilityRequirement, ...]:
    """Qué exige una petición por lo que lleva dentro, y con qué fuerza.

    La distinción importa y costó una prueba entenderla: **ofrecer herramientas no es
    necesitarlas**. El bucle manda siempre su catálogo, y un modelo que no sabe llamarlas
    todavía puede contestar algo que no requiere ninguna. Rechazarlo de entrada prohibiría
    un uso legítimo, y lo que ocurre si hacía falta actuar ya tiene mecanismo: la
    verificación no encuentra evidencia y el run no se da por bueno.

    Un esquema de respuesta sí es una exigencia. Quien lo manda va a parsear contra él, y
    un proveedor que no lo garantiza devuelve algo que no se puede leer — un fallo sin
    nada que lo explique salvo la ausencia que nadie comprobó.
    """
    requirements: list[CapabilityRequirement] = []
    if offers_tools:
        requirements.append(CapabilityRequirement("tool_calls", CapabilityStrength.PREFERRED))
    if needs_schema:
        requirements.append(CapabilityRequirement("structured_output"))
    return tuple(requirements)


__all__ = [
    "CapabilityMatch",
    "CapabilityRequirement",
    "CapabilityResolutionError",
    "CapabilityStrength",
    "UnsupportedCapabilityError",
    "match",
    "require",
    "requirements_for",
]
