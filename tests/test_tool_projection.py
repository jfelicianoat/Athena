"""Un resultado y dos vistas: lo que se le cuenta al modelo y lo que ve una persona.

La propiedad que se defiende aquí es una sola: **las vistas se derivan del resultado
canónico y nada deriva el resultado canónico de una vista.** Casi todas las pruebas de
este fichero son formas distintas de intentar romperla.
"""

from __future__ import annotations

from athena.permissions import RiskLevel
from athena.tool_projection import (
    DISPLAY_ITEM_LIMIT,
    MODEL_TEXT_LIMIT,
    DisplayView,
    ModelView,
    ResultKind,
    ToolProjection,
    default_projection,
    model_view_of,
    project,
)
from athena.tools import ToolResult, ToolResultReference, ToolSpec


def _spec(name: str = "grep") -> ToolSpec:
    return ToolSpec(
        name=name,
        description="Una tool cualquiera.",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        risk=RiskLevel.LOW,
        max_result_size_chars=1_000,
    )


def test_una_lista_se_proyecta_como_algo_que_se_recorre() -> None:
    resultado = ToolResult({"query": "add", "matches": ["a.py:1", "b.py:2"], "truncated": False})

    vista = default_projection(_spec(), resultado)

    assert vista.display.kind is ResultKind.ITEMS
    assert vista.display.items == ("a.py:1", "b.py:2")
    assert vista.display.facts["count"] == 2
    # Los escalares del resultado siguen ahí: son lo que una interfaz enseña al lado.
    assert vista.display.facts["query"] == "add"
    assert "a.py:1" in vista.model.text


def test_el_total_no_es_lo_que_se_ensena() -> None:
    """Una interfaz que enseña cincuenta de trescientos tiene que poder decir de cuántos.

    Si `count` fuese lo enumerado, el número diría cuánto cupo en pantalla y se leería
    como cuánto hay. Es un dato que se contradice a sí mismo sin avisar.
    """
    muchos = [f"linea-{indice}" for indice in range(300)]
    vista = default_projection(_spec(), ToolResult({"matches": muchos}))

    assert len(vista.display.items) == DISPLAY_ITEM_LIMIT
    assert vista.display.facts["count"] == 300


def test_lo_que_se_le_recorta_al_modelo_no_se_le_recorta_al_registro() -> None:
    """El resultado canónico sale intacto de proyectarlo."""
    largo = "x" * (MODEL_TEXT_LIMIT * 2)
    resultado = ToolResult({"path": "a.py", "content": largo, "line_count": 1})

    vista = default_projection(_spec("read_file"), resultado)

    assert vista.model.truncated
    assert len(vista.model.text) < len(largo)
    assert resultado.output == {"path": "a.py", "content": largo, "line_count": 1}


def test_un_resultado_externalizado_le_dice_al_modelo_que_hay_mas() -> None:
    """Y dónde está.

    Un extracto presentado como resultado completo invita a razonar sobre él como si lo
    fuera, que es la forma barata de equivocarse con confianza.
    """
    referencia = ToolResultReference("k1", "text/plain", 900_000)
    resultado = ToolResult({"summary": "primeras lineas"}, reference=referencia)

    vista = default_projection(_spec("bash"), resultado)

    assert vista.model.truncated
    assert referencia.uri in vista.model.text
    assert vista.display.kind is ResultKind.REFERENCE
    assert vista.display.reference_uri == referencia.uri


def test_un_registro_sin_lista_ni_cuerpo_se_ensena_por_sus_campos() -> None:
    resultado = ToolResult({"committed": True, "revision": "abc123"})

    vista = default_projection(_spec("git_commit"), resultado)

    assert vista.display.kind is ResultKind.RECORD
    assert vista.display.facts == {"committed": True, "revision": "abc123"}


def test_con_dos_textos_largos_no_se_elige_uno_por_el_agente() -> None:
    """`bash` devuelve stdout y stderr: cuál es «el cuerpo» no lo decide un caso general.

    Elegir el primero enseñaría un comando fallido por su salida vacía y escondería el
    error, que es exactamente el dato que alguien había ido a buscar.
    """
    resultado = ToolResult(
        {
            "stdout": "linea\n" * 100,
            "stderr": "traceback\n" * 100,
            "exit_code": 1,
        }
    )

    vista = default_projection(_spec("bash"), resultado)

    assert vista.display.kind is ResultKind.RECORD
    assert "stderr" in vista.model.text, "se escondio la salida de error"
    assert vista.display.facts["exit_code"] == 1


def test_una_tool_puede_explicarse_mejor_que_el_caso_general() -> None:
    class ConVozPropia:
        spec = _spec("propia")

        def project(self, result: ToolResult) -> ToolProjection:
            del result
            return ToolProjection(
                model=ModelView("lo cuento yo"),
                display=DisplayView(ResultKind.CHANGE, "propia", "a mi manera"),
            )

    vista = project(ConVozPropia(), _spec("propia"), ToolResult({"a": 1}))

    assert vista.model.text == "lo cuento yo"
    assert vista.display.kind is ResultKind.CHANGE


def test_una_tool_que_no_dice_nada_recibe_el_caso_general() -> None:
    class Callada:
        spec = _spec("callada")

    vista = project(Callada(), _spec("callada"), ToolResult({"matches": ["x"]}))

    assert vista.display.kind is ResultKind.ITEMS


def test_un_resultado_que_no_paso_por_el_ejecutor_no_tiene_vista() -> None:
    """No se le fabrica una: se salto el contrato y la politica de tamaño.

    Rellenarla aquí disimularía justamente eso, y quien la leyese creería que ese
    resultado recorrió un camino que nunca recorrió.
    """
    assert model_view_of(ToolResult({"a": 1})) is None


def test_la_vista_del_modelo_se_lee_de_vuelta_tal_y_como_se_dejo() -> None:
    original = ModelView("hola", truncated=True, reference_uri="athena-result://k")
    resultado = ToolResult({"a": 1}, metadata={"model_view": original.to_json()})

    assert model_view_of(resultado) == original


def test_una_vista_corrupta_se_trata_como_ausente_y_no_como_vacia() -> None:
    """Un texto vacío se le contaría al modelo como «la tool no devolvió nada»."""
    assert model_view_of(ToolResult({"a": 1}, metadata={"model_view": {"text": 7}})) is None
    assert model_view_of(ToolResult({"a": 1}, metadata={"model_view": "no soy un objeto"})) is None


def test_una_lista_vacia_lo_dice_en_vez_de_quedarse_muda() -> None:
    """Cero resultados es una respuesta; un texto vacío parece un fallo."""
    vista = default_projection(_spec(), ToolResult({"matches": []}))

    assert vista.model.text.strip() != ""
    assert vista.display.facts["count"] == 0


def test_grep_se_explica_como_se_lee_una_busqueda() -> None:
    """`fichero:linea: texto`, no `path=... line=... text=...`.

    El caso general no sabe que eso son coincidencias en un fichero. La forma de toda la
    vida sirve para dos cosas que la otra no: cuesta menos contexto, y el modelo puede
    encadenar la linea con `read_range` sin volver a interpretar nada.
    """
    from athena.repository_tools import GrepTool

    resultado = ToolResult(
        {
            "query": "add",
            "matches": [{"path": "calc.py", "line": 2, "text": "def add(a, b):"}],
            "truncated": False,
        }
    )

    vista = GrepTool().project(resultado)

    assert vista.model.text == "calc.py:2: def add(a, b):"
    assert vista.display.summary == "1 coincidencia"
    assert vista.display.facts["truncated"] is False


def test_read_range_se_explica_con_el_codigo_numerado() -> None:
    from athena.repository_tools import ReadRangeTool

    resultado = ToolResult(
        {
            "path": "calc.py",
            "start_line": 1,
            "end_line": 2,
            "lines": [
                {"line": 1, "text": "def add(a, b):"},
                {"line": 2, "text": "    return a + b"},
            ],
        }
    )

    vista = ReadRangeTool().project(resultado)

    assert vista.model.text == "1: def add(a, b):\n2:     return a + b"
    assert vista.display.title == "calc.py"
    assert vista.display.facts["start_line"] == 1


def test_una_busqueda_sin_resultados_lo_dice() -> None:
    """Cero coincidencias es una respuesta util; un texto vacio parece un fallo."""
    from athena.repository_tools import GrepTool

    vista = GrepTool().project(ToolResult({"query": "nada", "matches": [], "truncated": False}))

    assert vista.model.text.strip() != ""
    assert vista.display.summary == "0 coincidencias"
