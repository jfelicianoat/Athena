"""Que modelos ofrece este despliegue, y cual usa un run que no pide ninguno.

Hasta ahora el modelo era un dato del proceso: `ATHENA_PREFERRED_MODEL` se leia al
arrancar y valia para todos los runs, asi que ninguna interfaz podia ofrecer una eleccion
— y quien queria otro modelo tenia que reiniciar el servicio. El adaptador del broker, en
cambio, lleva desde siempre mirando `request.model or self._preferred_model`: la costura
existia y no llegaba nadie hasta ella.

Esto la abre sin regalar la decision. La regla 7 dice que las decisiones de seguridad las
toma el runtime y que el cliente puede pedir pero no autorizarse; lo mismo vale aqui. El
cliente **pide** un modelo y el despliegue decide cuales existen. Un nombre fuera de la
lista es un 400, no una caida silenciosa al de por defecto: quien pide un modelo capaz de
razonar sobre codigo y recibe otro no se entera hasta que el trabajo sale mal.

Sin configurar, la lista tiene un solo elemento: el modelo de siempre. Un catalogo que se
rellenase solo con lo que el broker anuncia ofreceria los ciento y pico modelos de su
inventario —incluidos los de *embeddings*— como si todos sirvieran para conducir un
agente, y elegir a ciegas entre ellos no es elegir.
"""

from __future__ import annotations

from collections.abc import Iterable

from athena.errors import ToolValidationError
from athena.types import JSONObject


class ModelCatalog:
    """Los modelos que este despliegue admite para un run, en el orden en que se ofrecen."""

    def __init__(self, models: Iterable[str] = (), *, default: str = "") -> None:
        names: list[str] = []
        for raw in models:
            name = raw.strip()
            if name and name not in names:
                names.append(name)
        chosen = default.strip()
        if chosen and chosen not in names:
            # El de por defecto siempre se ofrece. Tenerlo activo y fuera de la lista
            # describiria mal el despliegue: la interfaz enseñaria una eleccion que no
            # incluye lo que de verdad corre cuando no se elige.
            names.insert(0, chosen)
        if not names:
            raise ValueError("Un catalogo de modelos vacio no puede atender ningun run")
        self._names = tuple(names)
        self._default = chosen or self._names[0]

    @property
    def default(self) -> str:
        return self._default

    def names(self) -> tuple[str, ...]:
        """En orden de oferta, no alfabetico: el orden lo elige quien despliega."""
        return self._names

    def offers(self, name: str) -> bool:
        return name in self._names

    def resolve(self, requested: str | None) -> str:
        """El modelo con el que corre un run, o un error que dice cuales hay.

        Vacio significa «el que decida el despliegue», que es lo que manda cualquier
        cliente que no tenga selector — y por eso no puede ser un error.
        """
        if not requested:
            return self._default
        name = requested.strip()
        if name not in self._names:
            raise ToolValidationError(
                f"Modelo desconocido: {name}. Disponibles: {', '.join(self._names)}"
            )
        return name

    def to_json(self) -> JSONObject:
        return {
            "default": self._default,
            "models": [{"name": name, "default": name == self._default} for name in self._names],
        }

    @classmethod
    def from_environment(cls, allowed: str, preferred: str) -> ModelCatalog:
        """Lee `ATHENA_ALLOWED_MODELS` y `ATHENA_PREFERRED_MODEL`, ya en crudo.

        Toma cadenas y no `os.environ` para que quien la prueba no tenga que ensuciar el
        entorno del proceso de pruebas para comprobar como se lee una lista.
        """
        names = [item.strip() for item in allowed.split(",")] if allowed.strip() else []
        return cls(names, default=preferred.strip())


__all__ = ["ModelCatalog"]
