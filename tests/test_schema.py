"""Qué comprueba el comprobador de esquemas, y qué dice honestamente que no comprueba."""

from __future__ import annotations

from athena.schema import violations
from athena.types import JSONSchema

OBJETO: JSONSchema = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "line_count": {"type": "integer"},
        "clean": {"type": "boolean"},
    },
    "required": ["path", "line_count"],
    "additionalProperties": False,
}


def test_lo_que_cumple_no_produce_ni_una_desviacion() -> None:
    assert violations(OBJETO, {"path": "a.py", "line_count": 3, "clean": True}) == ()


def test_falta_un_campo_declarado_obligatorio() -> None:
    (unica,) = violations(OBJETO, {"path": "a.py"})
    assert "line_count" in unica


def test_un_campo_que_nadie_declaro_es_una_desviacion() -> None:
    """Un campo de más no es inofensivo: o sobra o el esquema se quedó viejo."""
    (unica,) = violations(OBJETO, {"path": "a.py", "line_count": 1, "sorpresa": 2})
    assert "sorpresa" in unica


def test_un_booleano_no_cuela_por_entero() -> None:
    """En Python `bool` hereda de `int`, así que un `True` pasaría por un contador.

    Es la clase de dato que se lee como cifra: un `líneas: True` contado como 1 no falla
    en ninguna parte, simplemente miente en todas.
    """
    (unica,) = violations(OBJETO, {"path": "a.py", "line_count": True})
    assert "integer" in unica


def test_un_entero_si_cuela_por_numero() -> None:
    assert violations({"type": "number"}, 3) == ()
    assert violations({"type": "number"}, True) != ()


def test_los_valores_de_un_enum_se_comprueban() -> None:
    esquema: JSONSchema = {"type": "string", "enum": ["file", "directory"]}
    assert violations(esquema, "file") == ()
    assert violations(esquema, "socket") != ()


def test_los_elementos_de_una_lista_se_comprueban_por_dentro() -> None:
    esquema: JSONSchema = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"line": {"type": "integer"}},
            "required": ["line"],
        },
    }
    assert violations(esquema, [{"line": 1}, {"line": 2}]) == ()
    assert violations(esquema, [{"line": 1}, {"line": "dos"}]) != ()


def test_una_lista_larga_incumplida_no_produce_mil_lineas_iguales() -> None:
    """Repetir el mismo problema trescientas veces no lo explica mejor que cinco."""
    esquema: JSONSchema = {"type": "array", "items": {"type": "integer"}}
    encontradas = violations(esquema, ["no"] * 300)

    assert 0 < len(encontradas) <= 6
    assert "mas elementos" in encontradas[-1]


def test_un_tipo_equivocado_no_arrastra_quejas_derivadas() -> None:
    """Si ni siquiera es un objeto, «falta el campo path» describiría mal el problema."""
    encontradas = violations(OBJETO, "no soy un objeto")

    assert len(encontradas) == 1
    assert "object" in encontradas[0]


def test_lo_que_no_entiende_no_lo_rechaza() -> None:
    """Esto no es JSON Schema y no finge serlo.

    Fallar ante una palabra clave que no implementa rechazaría contratos válidos; fingir
    entenderla daría por comprobado lo que nunca miró. Se ignora, y está escrito.
    """
    esquema: JSONSchema = {
        "type": "integer",
        "minimum": 10,
        "multipleOf": 3,
        "oneOf": [{"type": "string"}],
    }

    assert violations(esquema, 4) == ()


def test_la_ruta_del_problema_dice_donde_esta() -> None:
    esquema: JSONSchema = {
        "type": "object",
        "properties": {
            "matches": {
                "type": "array",
                "items": {"type": "object", "properties": {"line": {"type": "integer"}}},
            }
        },
    }
    (unica,) = violations(esquema, {"matches": [{"line": "x"}]}, where="grep")

    assert unica.startswith("grep.matches[0].line")
