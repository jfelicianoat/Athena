"""Cada tool de Athena, ejecutada de verdad, contra el esquema que declara.

Esta prueba existe porque un run real encontró lo que 835 pruebas no: `read_range`
declaraba devolver una lista de cadenas y devuelve una lista de objetos. Ninguna prueba lo
vio porque ninguna comparaba lo devuelto con lo declarado — cada una comprobaba lo que la
tool hace, que es otra pregunta.

La regla que deja escrita: **una tool nueva no vale con estar probada, tiene que estar
probada contra su propio contrato**, y eso se hace aquí, ejecutándola.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from athena.cancellation import CancellationSource
from athena.git_tools import git_read_tools
from athena.mutation_tools import workspace_mutation_tools
from athena.process_tools import BashTool
from athena.registry import ToolRegistry
from athena.repository_tools import repository_read_tools
from athena.schema import violations
from athena.tool_search import ToolSearchTool
from athena.tools import Tool, ToolContext
from athena.types import JSONObject
from athena.workspace import Workspace

#: Unos argumentos validos por tool. Escritos a mano a proposito: generarlos del
#: input_schema probaria el generador, no las tools.
LLAMADAS: dict[str, JSONObject] = {
    "glob": {"pattern": "*.py"},
    "grep": {"query": "add"},
    "read_file": {"path": "calc.py"},
    "read_range": {"path": "calc.py", "start_line": 1, "end_line": 2},
    "list_directory": {"path": "."},
    "git_status": {},
    "git_diff": {},
    "git_log": {},
    "git_show": {"revision": "HEAD"},
    "write_file": {"path": "nuevo.py", "content": "x = 1\n"},
    "edit_file": {"path": "calc.py", "old_string": "a - b", "new_string": "a + b"},
    "bash": {"command": "git --version"},
    "tool_search": {"query": "leer"},
}


def _repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    for argumentos in (
        ("init",),
        ("add", "."),
        ("-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-m", "base"),
    ):
        hecho = subprocess.run(
            ["git", "-C", str(root), *argumentos], capture_output=True, check=False, text=True
        )
        if hecho.returncode != 0:
            pytest.skip(f"git no disponible: {hecho.stderr}")
    return root


def _tools() -> tuple[Tool, ...]:
    """Todas las tools que Athena escribe, incluidas las que cuesta montar.

    `bash` y `tool_search` entran aunque necesiten mas andamiaje: dejarlas fuera por
    incomodas convertiria esta prueba en una lista de las tools faciles, que es como se
    quedan los huecos.
    """
    return (
        *repository_read_tools(),
        *git_read_tools(),
        *workspace_mutation_tools(),
        BashTool(),
        ToolSearchTool(ToolRegistry(repository_read_tools())),
    )


@pytest.mark.parametrize("tool", _tools(), ids=lambda tool: tool.spec.name)
def test_lo_que_una_tool_devuelve_es_lo_que_declaro_devolver(tool: Tool, tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    argumentos = LLAMADAS.get(tool.spec.name)
    assert argumentos is not None, (
        f"{tool.spec.name} no tiene una llamada en esta prueba: una tool sin ella entra "
        "en el runtime sin que nadie compare su salida con su contrato"
    )

    async def scenario() -> JSONObject:
        contexto = ToolContext("s-1", Workspace.from_path(root), "c-1")
        validados = tool.validate(dict(argumentos))
        resultado = await tool.execute(contexto, validados, CancellationSource().token)
        return {"output": resultado.output}

    devuelto = asyncio.run(scenario())["output"]
    desviaciones = violations(tool.spec.output_schema, devuelto, where=tool.spec.name)

    assert desviaciones == (), f"{tool.spec.name} no cumple lo que declara: {desviaciones}"


def test_ninguna_tool_de_athena_se_queda_sin_llamada_en_esta_prueba() -> None:
    """Si alguien añade una tool y no la prueba aquí, que se note al añadirla."""
    sin_cubrir = sorted({tool.spec.name for tool in _tools()} - set(LLAMADAS))

    assert sin_cubrir == [], f"tools sin comprobar contra su contrato: {sin_cubrir}"
