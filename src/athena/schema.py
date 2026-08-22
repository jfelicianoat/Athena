"""Comprobación estructural de un valor contra un esquema declarado.

Athena no tiene dependencias, así que esto no es una implementación de JSON Schema y no
pretende serlo. Comprueba lo que los contratos de Athena declaran de verdad —tipo, campos
obligatorios, campos desconocidos, tipo de los elementos de una lista y valores de un
`enum`— y **ignora en silencio lo que no entiende**.

Esa última parte es deliberada y es la razón de que este módulo diga aquí lo que hace: un
comprobador que fallase ante una palabra clave que no implementa rechazaría contratos
válidos, y uno que fingiera entenderlas daría por comprobado lo que nunca miró. Lo que no
está en la lista de arriba no se comprueba, y quien escriba un esquema tiene que saberlo.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from athena.types import JSONSchema, JSONValue

#: Los tipos de JSON Schema que sí se comprueban, y con qué se corresponden en Python.
#:
#: `bool` se excluye de `integer`/`number` a propósito: en Python es una subclase de `int`,
#: así que un `True` colado donde se esperaba un contador pasaría por bueno.
_TYPES: dict[str, tuple[type, ...]] = {
    "object": (Mapping,),
    "array": (list, tuple),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


def violations(schema: JSONSchema, value: JSONValue, *, where: str = "value") -> tuple[str, ...]:
    """En qué no se parece `value` a lo que `schema` declaraba.

    Devuelve las desviaciones en vez de lanzar porque quien llama decide qué hacer con
    ellas: una desviación puede ser un fallo o puede ser un dato que registrar, y esa es
    una decisión de política que no le toca a un comprobador.
    """
    return tuple(_check(schema, value, where))


def _check(schema: JSONSchema, value: JSONValue, where: str) -> list[str]:
    found: list[str] = []
    declared = schema.get("type")
    if isinstance(declared, str):
        expected = _TYPES.get(declared)
        if expected is not None and not _is(value, declared, expected):
            found.append(f"{where}: se esperaba {declared} y llegó {_name(value)}")
            # Sin el tipo correcto, lo demás no se puede comprobar sin inventar: un
            # «falta el campo x» sobre algo que ni siquiera es un objeto describiria mal
            # el unico problema que hay.
            return found
    allowed = schema.get("enum")
    if (
        isinstance(allowed, Sequence)
        and not isinstance(allowed, (str, bytes))
        and value not in list(allowed)
    ):
        found.append(f"{where}: {value!r} no es uno de los valores declarados")
    if isinstance(value, Mapping):
        found.extend(_check_object(schema, value, where))
    elif isinstance(value, (list, tuple)):
        found.extend(_check_array(schema, value, where))
    return found


def _check_object(schema: JSONSchema, value: Mapping[str, JSONValue], where: str) -> list[str]:
    found: list[str] = []
    required = schema.get("required")
    if isinstance(required, Sequence) and not isinstance(required, (str, bytes)):
        missing = [name for name in required if isinstance(name, str) and name not in value]
        if missing:
            found.append(f"{where}: faltan campos declarados: {', '.join(sorted(missing))}")
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return found
    if schema.get("additionalProperties") is False:
        unknown = sorted(set(value) - set(properties))
        if unknown:
            found.append(f"{where}: campos no declarados: {', '.join(unknown)}")
    for name, subschema in properties.items():
        if name in value and isinstance(subschema, Mapping):
            found.extend(_check(subschema, value[name], f"{where}.{name}"))
    return found


def _check_array(schema: JSONSchema, value: Sequence[JSONValue], where: str) -> list[str]:
    items = schema.get("items")
    if not isinstance(items, Mapping):
        return []
    found: list[str] = []
    for index, element in enumerate(value):
        found.extend(_check(items, element, f"{where}[{index}]"))
        if len(found) >= 5:
            # Un esquema incumplido en una lista larga lo incumple en cada elemento, y
            # mil lineas iguales no explican mejor el problema que cinco.
            found.append(f"{where}: y mas elementos con el mismo problema")
            break
    return found


def _is(value: JSONValue, declared: str, expected: tuple[type, ...]) -> bool:
    if declared in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, expected)


def _name(value: JSONValue) -> str:
    for name, types in _TYPES.items():
        if name in ("integer", "number") and isinstance(value, bool):
            continue
        if isinstance(value, types):
            return name
    return type(value).__name__


__all__ = ["violations"]
