"""MCP contra un servidor REAL de terceros (20-nov-2026): `tradingview-mcp` por stdio.

Athena no trae transporte MCP (ADR-014): quien integra aporta un `McpClient`. Aquí se
aporta uno mínimo por stdio (JSON-RPC 2.0, un mensaje por línea) y se ejecuta todo lo de
Athena de verdad: `mcp_tools`, registro, permisos R3, executor, bucle y lo que oye el
modelo. La herramienta elegida es inocua (análisis estático o comprobación de salud): no
se abre TradingView ni se toca ninguna cuenta.

Uso: python mcp_real.py <carpeta_salida>
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ATHENA = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ATHENA / "src"))
SERVER = Path.home() / ".local" / "bin" / "tradingview-mcp"

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


class StdioMcpClient:
    """Lo mínimo de MCP por stdio para cumplir `athena.mcp.McpClient`."""

    server_name = "tradingview"

    def __init__(self, command: list[str], cwd: Path) -> None:
        self.process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._next = 0
        self._lock = threading.Lock()
        self.log: list[str] = []

    def _rpc(self, method: str, params: JSONObject | None = None) -> Any:
        with self._lock:
            self._next += 1
            message = {"jsonrpc": "2.0", "id": self._next, "method": method, "params": params or {}}
            assert self.process.stdin and self.process.stdout
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()
            while True:
                line = self.process.stdout.readline()
                if not line:
                    raise RuntimeError("the MCP server closed its output")
                answer = json.loads(line)
                if answer.get("id") == self._next:
                    self.log.append(method)
                    if "error" in answer:
                        raise RuntimeError(f"MCP error: {answer['error']}")
                    return answer.get("result")

    def handshake(self) -> JSONObject:
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "athena-audit", "version": "0.3.8"},
            },
        )
        assert self.process.stdin
        self.process.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        )
        self.process.stdin.flush()
        return result

    async def list_tools(self, cancellation: CancellationToken) -> Sequence[McpToolDescriptor]:
        result = await asyncio.to_thread(self._rpc, "tools/list")
        return tuple(
            McpToolDescriptor(
                tool["name"],
                tool.get("description", ""),
                tool.get("inputSchema") or {"type": "object"},
                read_only=bool((tool.get("annotations") or {}).get("readOnlyHint")),
            )
            for tool in result.get("tools", [])
        )

    async def call_tool(
        self, name: str, arguments: JSONObject, cancellation: CancellationToken
    ) -> JSONObject:
        result = await asyncio.to_thread(
            self._rpc, "tools/call", {"name": name, "arguments": dict(arguments)}
        )
        return result if isinstance(result, dict) else {"result": result}

    def close(self) -> None:
        self.process.kill()
        self.process.wait(10)


class Prompt:
    def __init__(self) -> None:
        self.asked: list[str] = []

    async def confirm(self, request: PermissionRequest) -> PermissionDecision:
        self.asked.append(f"{request.tool_name} tier={request.tier.value}")
        return PermissionDecision.ALLOW


async def run(client: StdioMcpClient, root: Path) -> dict[str, Any]:
    source = CancellationSource()
    tools = await mcp_tools(
        client,
        source.token,
        policy=McpToolPolicy(timeout_seconds=30, load_policy=ToolLoadPolicy.CORE),
    )
    names = [tool.spec.name for tool in tools]
    chosen, arguments = None, {}
    for wanted, args in (
        ("pine_analyze", {"source": '//@version=5\nindicator("x")\nplot(close)\n'}),
        ("tv_health_check", {}),
    ):
        hit = f"mcp__tradingview__{wanted}"
        if hit in names:
            chosen, arguments = hit, args
            break
    if chosen is None:
        return {"herramientas": len(names), "error": "ninguna herramienta inocua conocida"}
    provider = FakeModelProvider(
        [
            ModelResponse(
                "", "fake", "tool_calls", tool_calls=(ModelToolCall("m-1", chosen, arguments),)
            ),
            ModelResponse("Hecho.", "fake", "stop"),
        ]
    )
    registry = ToolRegistry(tools)
    bus = InMemoryEventBus()
    prompt = Prompt()
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
    result = await loop.run("Analiza el script", workspace, source.token)
    told = provider.requests[1].messages[-1].content if len(provider.requests) > 1 else ""
    return {
        "herramientas_anunciadas": len(names),
        "herramienta": chosen,
        "preguntas": prompt.asked,
        "estado": result.status.value,
        "lo_que_oye_el_modelo": told[:600],
    }


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    client = StdioMcpClient(["node", "src/server.js"], SERVER)
    try:
        info = client.handshake()
        root = Path(tempfile.mkdtemp(prefix="athena-mcp-real-"))
        (root / "README.md").write_text("# demo\n", encoding="utf-8")
        result = asyncio.run(run(client, root))
        result["servidor"] = (info or {}).get("serverInfo")
        result["rpc"] = client.log
    finally:
        client.close()
    ok = (
        bool(result.get("preguntas"))
        and result.get("estado") == "completed"
        and '"ok": true' in result.get("lo_que_oye_el_modelo", "")
    )
    result["veredicto"] = "PASA" if ok else "REVISAR"
    (out / "mcp_real.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
