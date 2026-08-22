"""El esquema de salida obliga, y donde no obliga se dice que no obligó.

Un `output_schema` declarado y nunca comprobado es documentación que se desincroniza del
código sin que nada lo denuncie — y cuanto más se confía en él, peor: quien proyecta el
resultado, quien lo guarda y quien lo enseña dan por ciertos unos campos que quizá ya no
están.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from athena.cancellation import CancellationSource, CancellationToken
from athena.errors import ToolContractError
from athena.events import EventName, InMemoryEventBus, RuntimeEvent
from athena.models import ModelToolCall
from athena.permissions import (
    PermissionRequest,
    ReadOnlyPermissionEngine,
    RiskLevel,
    RiskTier,
)
from athena.registry import ToolRegistry
from athena.stores import InMemoryToolResultStore
from athena.tool_executor import ToolExecutor
from athena.tools import OutputContract, ToolContext, ToolResult, ToolSpec
from athena.types import JSONObject, JSONValue
from athena.workspace import Workspace

ESQUEMA: JSONObject = {
    "type": "object",
    "properties": {"path": {"type": "string"}, "line_count": {"type": "integer"}},
    "required": ["path", "line_count"],
    "additionalProperties": False,
}


class _Tool:
    """Una tool que devuelve exactamente lo que se le diga, cumpla o no."""

    def __init__(self, devuelve: JSONValue, contrato: OutputContract) -> None:
        self.devuelve = devuelve
        self.spec = ToolSpec(
            name="declarada",
            description="Devuelve lo que se le dijo.",
            input_schema={"type": "object", "additionalProperties": False},
            output_schema=ESQUEMA,
            risk=RiskLevel.LOW,
            max_result_size_chars=10_000,
            output_contract=contrato,
        )

    def validate(self, arguments: JSONObject) -> JSONObject:
        return dict(arguments)

    def permission(self, context: ToolContext, arguments: JSONObject) -> PermissionRequest:
        return PermissionRequest(
            tool_name=self.spec.name,
            operation="leer",
            workspace=context.workspace,
            risk=self.spec.risk,
            tier=RiskTier.R0_READ_ONLY,
            is_read_only=True,
            is_destructive=False,
            arguments=arguments,
        )

    async def execute(
        self, context: ToolContext, arguments: JSONObject, cancellation: CancellationToken
    ) -> ToolResult:
        del context, arguments
        cancellation.raise_if_cancelled()
        return ToolResult(self.devuelve)

    def is_read_only(self, arguments: JSONObject) -> bool:
        del arguments
        return True

    def is_destructive(self, arguments: JSONObject) -> bool:
        del arguments
        return False

    def is_concurrency_safe(self, arguments: JSONObject) -> bool:
        del arguments
        return True


def _run(tool: _Tool, root: Path) -> tuple[ToolResult | Exception, list[RuntimeEvent]]:
    eventos: list[RuntimeEvent] = []

    async def scenario() -> ToolResult | Exception:
        bus = InMemoryEventBus()
        bus.subscribe(eventos.append)
        executor = ToolExecutor(
            ToolRegistry((tool,)),
            ReadOnlyPermissionEngine(),
            InMemoryToolResultStore(),
            bus,
        )
        try:
            return await executor.execute(
                ModelToolCall("c-1", "declarada", {}),
                session_id="s-1",
                workspace=Workspace.from_path(root),
                cancellation=CancellationSource().token,
            )
        except Exception as error:
            return error

    return asyncio.run(scenario()), eventos


def test_lo_que_cumple_su_contrato_pasa(tmp_path: Path) -> None:
    resultado, _ = _run(_Tool({"path": "a.py", "line_count": 3}, OutputContract.ENFORCED), tmp_path)

    assert isinstance(resultado, ToolResult)
    assert resultado.output == {"path": "a.py", "line_count": 3}


def test_una_tool_que_incumple_lo_que_declaro_falla(tmp_path: Path) -> None:
    """Y falla como error de ejecución, no de validación.

    Los argumentos estaban bien; quien incumplió fue la tool. Importa porque recuperarse
    de un argumento malo es reformular la llamada, y aquí reformularla no arregla nada.
    """
    resultado, _ = _run(_Tool({"path": "a.py"}, OutputContract.ENFORCED), tmp_path)

    assert isinstance(resultado, ToolContractError)
    assert "line_count" in str(resultado)
    assert resultado.details["violations"]


def test_un_contrato_que_solo_describe_publica_la_desviacion_y_sigue(tmp_path: Path) -> None:
    """Para lo que Athena no escribe —una tool remota— imponer sería caerse por otro.

    Pero tolerar en silencio convertiría la desviación en la forma normal de esa tool sin
    que nadie lo hubiera decidido, así que consta.
    """
    resultado, eventos = _run(_Tool({"path": "a.py"}, OutputContract.DECLARED), tmp_path)

    assert isinstance(resultado, ToolResult), "una desviación tolerada no puede tumbar la llamada"
    avisos = [e for e in eventos if e.name is EventName.TOOL_CONTRACT_VIOLATED]
    assert len(avisos) == 1
    assert avisos[0].payload["tool_name"] == "declarada"
    assert avisos[0].correlation_id == "c-1", "el aviso no dice de qué llamada era"


def test_el_contrato_se_comprueba_antes_de_externalizar(tmp_path: Path) -> None:
    """Después, lo que hay es el recibo del almacén y no lo que la tool prometió.

    Comprobar el recibo contra el esquema del resultado daría por incumplido cualquier
    resultado grande, que es la peor forma de tener razón.
    """
    grande = _Tool({"path": "a.py", "line_count": 1}, OutputContract.ENFORCED)
    grande.devuelve = {"path": "a.py" * 5_000, "line_count": 1}
    grande.spec = ToolSpec(
        name="declarada",
        description="Devuelve algo grande.",
        input_schema={"type": "object", "additionalProperties": False},
        output_schema=ESQUEMA,
        risk=RiskLevel.LOW,
        max_result_size_chars=100,
    )

    resultado, _ = _run(grande, tmp_path)

    assert isinstance(resultado, ToolResult)
    assert resultado.reference is not None, "el resultado grande tenía que externalizarse"


def test_el_evento_de_fin_lleva_lo_que_una_interfaz_necesita_para_dibujarlo(
    tmp_path: Path,
) -> None:
    """Sin esto, cada cliente vuelve a deducir la presentación de un payload interno."""
    _, eventos = _run(_Tool({"path": "a.py", "line_count": 3}, OutputContract.ENFORCED), tmp_path)

    (fin,) = [e for e in eventos if e.name is EventName.TOOL_COMPLETED]
    display = fin.payload["display"]
    assert isinstance(display, dict)
    assert display["kind"] == "record"
    assert display["facts"] == {"path": "a.py", "line_count": 3}


def test_la_vista_va_aparte_del_resultado_canonico(tmp_path: Path) -> None:
    """Meterla en `output` dejaría al resultado incumpliendo su propio esquema."""
    resultado, _ = _run(_Tool({"path": "a.py", "line_count": 3}, OutputContract.ENFORCED), tmp_path)

    assert isinstance(resultado, ToolResult)
    assert resultado.output == {"path": "a.py", "line_count": 3}
    assert "model_view" in resultado.metadata
    assert "display" in resultado.metadata


def test_las_tools_de_athena_declaran_de_verdad_lo_que_devuelven() -> None:
    """Un `{"type": "object"}` es un contrato que no dice nada y no se puede incumplir.

    Esta prueba existe porque así estaban todas: el esquema se comprobaba y pasaba
    siempre, no porque las tools cumplieran sino porque no se les pedía nada.
    """
    from athena.git_tools import git_read_tools
    from athena.mutation_tools import workspace_mutation_tools
    from athena.repository_tools import repository_read_tools

    vacias = [
        tool.spec.name
        for tool in (*repository_read_tools(), *git_read_tools(), *workspace_mutation_tools())
        if set(tool.spec.output_schema) <= {"type"}
    ]

    assert vacias == [], f"estas tools no declaran su salida: {vacias}"


def test_solo_lo_ajeno_puede_rebajar_el_contrato() -> None:
    """Athena responde por lo suyo. De un servidor remoto no puede responder."""
    from athena.git_tools import git_read_tools
    from athena.repository_tools import repository_read_tools

    for tool in (*repository_read_tools(), *git_read_tools()):
        assert tool.spec.output_contract is OutputContract.ENFORCED, tool.spec.name


def test_quien_no_dice_nada_queda_obligado() -> None:
    """El descuido cae del lado seguro.

    Si rebajar fuese el defecto, una tool nueva escrita sin pensar en esto naceria con el
    contrato apagado, y apagarlo sin decidirlo es como estaban todas antes.
    """
    spec = ToolSpec(
        name="x",
        description="x",
        input_schema={},
        output_schema={},
        risk=RiskLevel.LOW,
        max_result_size_chars=1,
    )

    assert spec.output_contract is OutputContract.ENFORCED
