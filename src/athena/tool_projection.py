"""Un resultado, dos proyecciones: lo que se le cuenta al modelo y lo que ve una persona.

`ToolResult.output` es el resultado canónico: lo estructurado, lo que cumple el esquema
declarado y lo que se guarda. Hasta ahora era además lo único que había, así que iba
directo al modelo *y* cada interfaz se inventaba una presentación a partir de los eventos.
Las dos cosas son proyecciones distintas del mismo hecho, y ninguna de las dos es el hecho.

- **Al modelo** se le cuenta algo compacto y en texto. Su contexto es caro y no mejora por
  recibir el JSON entero de un listado de cien ficheros.
- **A una interfaz** se le da estructura: un tipo, un título, lo que se enumera y los datos
  sueltos. Sin esto, cada cliente vuelve a deducir la presentación leyendo el payload de un
  evento, que es acoplarse a un formato interno y repetir el trabajo en cada cliente.

**La regla que ordena todo esto: las proyecciones se derivan del resultado canónico, y
nada deriva el resultado canónico de una proyección.** Lo que se le recorta al modelo no se
le recorta al registro, y lo que se le da bonito a una interfaz no cambia lo que se
verificó. Una tool puede sustituir la proyección por defecto —sabe lo que devuelve mejor
que un caso general— pero no puede cambiar por esa vía lo que dice haber hecho.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from athena.tools import ToolResult, ToolSpec
from athena.types import JSONObject, JSONValue


class ResultKind(StrEnum):
    """Qué forma tiene un resultado para quien lo va a enseñar.

    Cinco y no más: son las formas que cambian cómo se dibuja algo. Un tipo por tool sería
    un catálogo que crece con cada tool y no le dice nada nuevo a quien pinta.
    """

    #: Un cuerpo de texto que se lee: un fichero, la salida de un comando.
    TEXT = "text"
    #: Una lista que se recorre: coincidencias, entradas de un directorio, rutas.
    ITEMS = "items"
    #: Algo cambió, y el cambio es lo que hay que enseñar.
    CHANGE = "change"
    #: Un hecho estructurado sin mejor forma que sus campos.
    RECORD = "record"
    #: Demasiado grande para caber: vive en el almacén y esto es su recibo.
    REFERENCE = "reference"


@dataclass(frozen=True, slots=True)
class ModelView:
    """Lo que se le cuenta al modelo sobre una llamada que terminó."""

    text: str
    truncated: bool = False
    reference_uri: str | None = None

    def to_json(self) -> JSONObject:
        return {
            "text": self.text,
            "truncated": self.truncated,
            "reference_uri": self.reference_uri,
        }


@dataclass(frozen=True, slots=True)
class DisplayView:
    """Lo que una interfaz necesita para dibujar un resultado sin interpretar prosa."""

    kind: ResultKind
    title: str
    summary: str = ""
    items: tuple[str, ...] = ()
    facts: JSONObject = field(default_factory=dict)
    reference_uri: str | None = None

    def to_json(self) -> JSONObject:
        return {
            "kind": self.kind.value,
            "title": self.title,
            "summary": self.summary,
            "items": list(self.items),
            "facts": dict(self.facts),
            "reference_uri": self.reference_uri,
        }


@dataclass(frozen=True, slots=True)
class ToolProjection:
    """Las dos vistas de un mismo resultado, hechas a la vez.

    Juntas y no por separado porque una tool que sabe explicarse al modelo sabe también
    qué enseñar, y dos métodos independientes acabarían contando dos historias.
    """

    model: ModelView
    display: DisplayView


@runtime_checkable
class ProjectsResults(Protocol):
    """Lo implementa la tool que quiere explicarse mejor que el caso general."""

    def project(self, result: ToolResult) -> ToolProjection: ...


#: Cuánto texto se le da al modelo por llamada antes de recortar.
#:
#: El límite es del canal, no del resultado: `max_result_size_chars` decide qué se
#: externaliza, y esto decide cuánto de lo que quedó inline vale la pena leer.
MODEL_TEXT_LIMIT = 4000

#: Cuántos elementos de una lista se enumeran en la vista de una interfaz.
DISPLAY_ITEM_LIMIT = 50


def project(tool: object, spec: ToolSpec, result: ToolResult) -> ToolProjection:
    """Las dos vistas de un resultado, dando primero a la tool ocasión de explicarse."""
    if isinstance(tool, ProjectsResults):
        return tool.project(result)
    return default_projection(spec, result)


def model_view_of(result: ToolResult) -> ModelView | None:
    """La vista que el ejecutor dejo puesta, si paso por el.

    Devuelve `None` en vez de fabricar una: un `ToolResult` construido a mano no ha
    cruzado el ejecutor, y darle una vista aqui disimularia que se salto el contrato, la
    politica de tamano y el resto de lo que ese camino hace.
    """
    raw = result.metadata.get("model_view")
    if not isinstance(raw, Mapping):
        return None
    text = raw.get("text")
    if not isinstance(text, str):
        return None
    uri = raw.get("reference_uri")
    return ModelView(
        text=text,
        truncated=bool(raw.get("truncated")),
        reference_uri=uri if isinstance(uri, str) else None,
    )


def default_projection(spec: ToolSpec, result: ToolResult) -> ToolProjection:
    """El caso general, para la tool que no dice nada.

    Deduce la forma de la estructura del resultado, no del nombre de la tool: el nombre lo
    elige quien la escribe y cambiaría la presentación por un renombrado.
    """
    if result.reference is not None:
        return _externalized(spec, result)
    output = result.output
    if isinstance(output, str):
        return _text(spec, output)
    if isinstance(output, Mapping):
        return _record(spec, output)
    if isinstance(output, (list, tuple)):
        return _items(spec, spec.name, [_line(item) for item in output], {})
    return _text(spec, _serialize(output))


def _externalized(spec: ToolSpec, result: ToolResult) -> ToolProjection:
    reference = result.reference
    assert reference is not None
    summary = ""
    if isinstance(result.output, Mapping):
        raw = result.output.get("summary")
        summary = raw if isinstance(raw, str) else ""
    return ToolProjection(
        model=ModelView(
            # Se le dice al modelo que hay más y dónde está, no se le esconde: una tool
            # cuyo resultado se externalizó y no lo dijera invitaría a razonar sobre un
            # extracto creyéndolo completo.
            text=f"{summary}\n[resultado completo en {reference.uri}]".strip(),
            truncated=True,
            reference_uri=reference.uri,
        ),
        display=DisplayView(
            kind=ResultKind.REFERENCE,
            title=spec.name,
            summary=summary,
            facts={"size_chars": reference.size_chars, "media_type": reference.media_type},
            reference_uri=reference.uri,
        ),
    )


def _text(spec: ToolSpec, body: str) -> ToolProjection:
    recortado, truncated = _clip(body, MODEL_TEXT_LIMIT)
    return ToolProjection(
        model=ModelView(recortado, truncated=truncated),
        display=DisplayView(
            kind=ResultKind.TEXT,
            title=spec.name,
            summary=_first_line(body),
            facts={"chars": len(body)},
        ),
    )


def _record(spec: ToolSpec, output: Mapping[str, JSONValue]) -> ToolProjection:
    """Un objeto: si lleva una lista, es lo que alguien va a recorrer."""
    listado = _first_list(output)
    escalares = {
        name: value
        for name, value in output.items()
        if not isinstance(value, (list, tuple, Mapping))
    }
    if listado is not None:
        nombre, elementos = listado
        return _items(spec, nombre, [_line(item) for item in elementos], escalares)
    cuerpo = _long_text(output)
    if cuerpo is not None:
        nombre, texto = cuerpo
        recortado, truncated = _clip(texto, MODEL_TEXT_LIMIT)
        return ToolProjection(
            model=ModelView(recortado, truncated=truncated),
            display=DisplayView(
                kind=ResultKind.TEXT,
                title=str(escalares.get("path") or spec.name),
                summary=_first_line(texto),
                facts={**escalares, "field": nombre},
            ),
        )
    serializado = _serialize(output)
    recortado, truncated = _clip(serializado, MODEL_TEXT_LIMIT)
    return ToolProjection(
        model=ModelView(recortado, truncated=truncated),
        display=DisplayView(
            kind=ResultKind.RECORD,
            title=spec.name,
            summary=_first_line(serializado),
            facts=dict(escalares),
        ),
    )


def _items(
    spec: ToolSpec, nombre: str, elementos: Sequence[str], escalares: JSONObject
) -> ToolProjection:
    cuerpo = "\n".join(elementos)
    recortado, truncated = _clip(cuerpo, MODEL_TEXT_LIMIT)
    return ToolProjection(
        model=ModelView(recortado or "(sin resultados)", truncated=truncated),
        display=DisplayView(
            kind=ResultKind.ITEMS,
            title=spec.name,
            # El total va aparte de los elementos enseñados: una interfaz que enseña
            # cincuenta de trescientos tiene que poder decir de cuántos.
            summary=f"{len(elementos)} {nombre}",
            items=tuple(elementos[:DISPLAY_ITEM_LIMIT]),
            facts={**escalares, "count": len(elementos)},
        ),
    )


def _first_list(output: Mapping[str, JSONValue]) -> tuple[str, Sequence[JSONValue]] | None:
    for name, value in output.items():
        if isinstance(value, (list, tuple)):
            return name, value
    return None


def _long_text(output: Mapping[str, JSONValue]) -> tuple[str, str] | None:
    """El campo de texto que es el cuerpo del resultado, si hay uno claro.

    «Claro» quiere decir uno solo con saltos de línea o largo: dos candidatos son un
    registro con varios textos, y elegir uno sería decidir por la tool cuál importa.
    """
    candidatos = [
        (name, value)
        for name, value in output.items()
        if isinstance(value, str) and ("\n" in value or len(value) > 200)
    ]
    return candidatos[0] if len(candidatos) == 1 else None


def _line(item: JSONValue) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, Mapping):
        return " ".join(
            f"{name}={value}"
            for name, value in item.items()
            if not isinstance(value, (list, tuple, Mapping))
        )
    return _serialize(item)


def _clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + "\n[…recortado]", True


def _first_line(text: str) -> str:
    primera = text.strip().splitlines()[0] if text.strip() else ""
    return primera[:200]


def _serialize(value: JSONValue) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


__all__ = [
    "DISPLAY_ITEM_LIMIT",
    "MODEL_TEXT_LIMIT",
    "DisplayView",
    "ModelView",
    "ProjectsResults",
    "ResultKind",
    "ToolProjection",
    "default_projection",
    "model_view_of",
    "project",
]
