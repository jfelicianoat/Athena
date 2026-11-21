"""MCP de extremo a extremo dentro de un AgentLoop real (20-nov-2026).

Athena no trae transporte MCP (ADR-014: `McpClient` es un protocolo que pone quien
integra), así que el «servidor» es un cliente en proceso. Todo lo demás es real:
`mcp_tools`, registro, motor de permisos, executor, bucle y presentación al modelo.
  M1  el modelo pide la herramienta MCP; es R3 → se pregunta; se PERMITE → se ejecuta
  M2  igual, se DENIEGA → no se llama al servidor
  M3  el servidor se cuelga → plazo de la herramienta; con H-09 el modelo se entera y el
      run sigue (antes acababa el run como «se agotó el tiempo de la sesión»)

Uso: python mcp_e2e.py <carpeta_salida>
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ATHENA = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ATHENA / "src"))

from athena.agent_loop import AgentLoop, AgentLoopConfig  # noqa: E402
from athena.cancellation import CancellationSource, CancellationToken  # noqa: E402
from athena.context import ContextBuilder  # noqa: E402
from athena.events import InMemoryEventBus  # noqa: E402
from athena.mcp import McpToolDescriptor, McpToolPolicy, mcp_tools  # noqa: E402
from athena.models import ModelResponse, ModelToolCall  # noqa: E402
from athena.permissions import (  # noqa: E402
    PermissionDecision,
    PermissionPolicy,
    PermissionRequest,
    PolicyPermissionEngine,
)
from athena.registry import ToolRegistry  # noqa: E402
from athena.stores import InMemoryToolResultStore  # noqa: E402
from athena.testing import FakeModelProvider  # noqa: E402
from athena.tool_executor import ToolExecutor  # noqa: E402
from athena.tools import ToolLoadPolicy  # noqa: E402
from athena.types import JSONObject  # noqa: E402
from athena.workspace import Workspace  # noqa: E402


class Server:
    server_name = "tracker"

    def __init__(self, *, hang: bool = False) -> None:
        self.hang = hang
        self.calls: list[JSONObject] = []

    async def list_tools(self, cancellation: CancellationToken) -> Sequence[McpToolDescriptor]:
        return (
            McpToolDescriptor(
                "search_issues",
                "Search the issue tracker",
                {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
                read_only=True,
            ),
        )

    async def call_tool(
        self, name: str, arguments: JSONObject, cancellation: CancellationToken
    ) -> JSONObject:
        self.calls.append(dict(arguments))
        if self.hang:
            await asyncio.Event().wait()
        return {"issues": [{"id": 7, "title": "El total no cuadra"}]}


class Prompt:
    def __init__(self, answer: PermissionDecision) -> None:
        self.answer = answer
        self.asked: list[str] = []

    async def confirm(self, request: PermissionRequest) -> PermissionDecision:
        self.asked.append(f"{request.tool_name} tier={request.tier.value}")
        return self.answer


async def scenario(root: Path, answer: PermissionDecision, hang: bool) -> dict[str, Any]:
    server = Server(hang=hang)
    source = CancellationSource()
    policy = McpToolPolicy(timeout_seconds=1.0, load_policy=ToolLoadPolicy.CORE)
    tools = await mcp_tools(server, source.token, policy=policy)
    name = tools[0].spec.name
    provider = FakeModelProvider(
        [
            ModelResponse(
                "",
                "fake",
                "tool_calls",
                tool_calls=(ModelToolCall("m-1", name, {"query": "total"}),),
            ),
            ModelResponse("Hecho.", "fake", "stop"),
        ]
    )
    registry = ToolRegistry(tools)
    bus = InMemoryEventBus()
    prompt = Prompt(answer)
    executor = ToolExecutor(
        registry,
        PolicyPermissionEngine(PermissionPolicy()),
        InMemoryToolResultStore(),
        bus,
        prompt=prompt,
    )
    workspace = Workspace.from_path(root)
    loop = AgentLoop(
        provider,
        registry,
        executor,
        ContextBuilder(workspace),
        bus,
        config=AgentLoopConfig(retry_backoff_seconds=0),
    )
    result = await loop.run("Busca la incidencia del total", workspace, source.token)
    told = provider.requests[1].messages[-1].content if len(provider.requests) > 1 else ""
    return {
        "estado": result.status.value,
        "preguntas": prompt.asked,
        "llamadas_al_servidor": len(server.calls),
        "peticiones_al_modelo": len(provider.requests),
        "lo_que_oye_el_modelo": told[:300],
        "error": result.error.code if result.error else None,
    }


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="athena-mcp-"))
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    results = {
        "M1_permitida": asyncio.run(scenario(root, PermissionDecision.ALLOW, hang=False)),
        "M2_denegada": asyncio.run(scenario(root, PermissionDecision.DENY, hang=False)),
        "M3_servidor_colgado": asyncio.run(scenario(root, PermissionDecision.ALLOW, hang=True)),
    }
    m1, m2, m3 = results["M1_permitida"], results["M2_denegada"], results["M3_servidor_colgado"]
    results["veredicto"] = {
        "M1": "PASA"
        if m1["preguntas"]
        and m1["llamadas_al_servidor"] == 1
        and "El total no cuadra" in m1["lo_que_oye_el_modelo"]
        else "FALLA",
        "M2": "PASA" if m2["preguntas"] and m2["llamadas_al_servidor"] == 0 else "FALLA",
        "M3": "PASA" if m3["peticiones_al_modelo"] == 2 and m3["estado"] != "failed" else "FALLA",
    }
    (out / "mcp_e2e.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
