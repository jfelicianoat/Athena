"""AI OS Core dirige los runs: sin su ALLOW no hay run ni llamada al modelo.

Core se simula con un servidor HTTP de verdad (las formas son las de Core 0.2.1), asi
que se prueba tambien el adaptador `AiOsCoreGovernor` y no solo el registro. El
recorrido contra el Core real esta en `scripts/verify_ai_os_core.py`.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from athena.adapters.ai_os_core import (
    AiOsCoreGovernor,
    CoreDeniedError,
    CoreUnavailableError,
    governor_from_environment,
)
from athena.adapters.service import AthenaService, CapabilityMode, RunOptions, RunRegistry
from athena.agent_loop import AgentRunStatus
from athena.events import InMemoryEventBus
from athena.models import ModelResponse
from athena.session_store import SqliteSessionStore
from athena.stores import InMemoryToolResultStore
from athena.testing import FakeModelProvider
from athena.workspace import Workspace
from test_service_adapter import _config, _request


class _Core:
    """Lo que Core contesta y lo que le llega."""

    def __init__(self) -> None:
        self.authorization = "ALLOW"
        self.calls: list[tuple[str, dict[str, Any]]] = []
        #: El monitor de Core caído o roto no debe notarse en el run.
        self.hops_fail = False
        #: El turno en la cola del Broker que el director fija en el monitor.
        self.broker_priority: int | None = None


@pytest.fixture
def core() -> Iterator[tuple[_Core, str]]:
    state = _Core()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.headers["X-Principal-Id"] == "athena"
            assert self.headers["Authorization"] == "Bearer tok-athena"
            state.calls.append((self.path, body))
            if self.path == "/api/v1/policies/evaluate":
                allow = state.authorization == "ALLOW"
                reply = {"authorization": state.authorization, "executable": allow,
                         "broker_priority": state.broker_priority}
            else:
                reply = {}
            data = json.dumps(reply).encode()
            if state.hops_fail and self.path == "/api/v1/flows/hops":
                self.send_response(500)
            else:
                self.send_response(200 if self.path.endswith("evaluate") else 201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield state, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


def _registry(tmp_path: Path, governor: AiOsCoreGovernor) -> tuple[RunRegistry, FakeModelProvider]:
    provider = FakeModelProvider([ModelResponse("Descripcion del proyecto.", "fake", "stop")] * 4)
    registry = RunRegistry(
        provider,
        InMemoryEventBus(),
        SqliteSessionStore(tmp_path / "sessions.db"),
        InMemoryToolResultStore(),
        governor=governor,
    )
    return registry, provider


def _repo(tmp_path: Path) -> Workspace:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("hola\n", encoding="utf-8")
    return Workspace.from_path(root)


_READ_ONLY = RunOptions(
    writes=CapabilityMode.OFF, execution=CapabilityMode.OFF, profile="questions"
)


@pytest.mark.parametrize("decision", ["DENY", "REQUIRE_APPROVAL", "INDETERMINATE"])
def test_without_allow_no_run_and_no_model_call(
    tmp_path: Path, core: tuple[_Core, str], decision: str
) -> None:
    state, url = core
    state.authorization = decision
    registry, provider = _registry(tmp_path, AiOsCoreGovernor(url, "tok-athena"))

    async def scenario() -> None:
        with pytest.raises(CoreDeniedError):
            await registry.start("Describe el proyecto", _repo(tmp_path), _READ_ONLY)
        assert not registry.live_ids()
        assert not await registry.list(None)
        await registry.shutdown()

    asyncio.run(scenario())
    assert provider.requests == []
    assert [path for path, _ in state.calls] == ["/api/v1/policies/evaluate"]
    context = state.calls[0][1]["context"]
    assert context["data_classification"] == "confidential"
    assert (context["writes"], context["exec"], context["operation_type"]) == (
        "off",
        "off",
        "agent_run",
    )


def test_core_down_fails_closed(tmp_path: Path) -> None:
    registry, provider = _registry(tmp_path, AiOsCoreGovernor("http://127.0.0.1:9", "tok-athena"))

    async def scenario() -> None:
        with pytest.raises(CoreUnavailableError):
            await registry.start("Describe el proyecto", _repo(tmp_path), _READ_ONLY)
        await registry.shutdown()

    asyncio.run(scenario())
    assert provider.requests == []


def test_allow_runs_and_leaves_receipts(tmp_path: Path, core: tuple[_Core, str]) -> None:
    state, url = core
    registry, provider = _registry(tmp_path, AiOsCoreGovernor(url, "tok-athena"))

    async def scenario() -> tuple[str, AgentRunStatus]:
        run_id = await registry.start("Describe el proyecto", _repo(tmp_path), _READ_ONLY)
        result = await registry.wait(run_id)
        await _receipts(state, 2)
        await registry.shutdown()
        return run_id, result.status

    run_id, status = asyncio.run(scenario())
    assert provider.requests, "con ALLOW el run llega al modelo"
    receipts = [body for path, body in state.calls if path == "/api/v1/operations/receipts"]
    assert {r["operation_id"] for r in receipts} == {run_id}
    expected = {"completed": "succeeded", "failed": "failed_terminal"}[status.value]
    assert sorted(r["status"] for r in receipts) == sorted(["accepted", expected])
    assert state.calls[0][1]["resource_id"] == run_id


def test_the_core_turn_reaches_the_run_and_the_broker_body(
    tmp_path: Path, core: tuple[_Core, str]
) -> None:
    from athena.adapters.ai_broker import QUEUE_PRIORITY, AiBrokerModelProvider, _prepare_request
    from athena.cancellation import CancellationSource
    from athena.models import ModelMessage, ModelRequest, ModelRole

    state, url = core
    state.broker_priority = 10
    registry, provider = _registry(tmp_path, AiOsCoreGovernor(url, "tok-athena"))
    seen: list[int | None] = []
    complete = provider.complete

    async def recording(request: Any, cancellation: Any) -> Any:
        seen.append(QUEUE_PRIORITY.get())
        return await complete(request, cancellation)

    provider.complete = recording  # type: ignore[method-assign]

    async def scenario() -> None:
        run_id = await registry.start("Describe el proyecto", _repo(tmp_path), _READ_ONLY)
        await registry.wait(run_id)
        await _receipts(state, 2)
        await registry.shutdown()

    asyncio.run(scenario())
    assert seen and set(seen) == {10}, "cada llamada del run lleva el turno de Core"

    broker = AiBrokerModelProvider("http://127.0.0.1:9", "tok")
    bodies: list[Any] = []

    async def fake_call(method: str, path: str, body: Any, cancellation: Any) -> tuple[int, Any]:
        bodies.append(body)
        return 202, {"task_id": "task_1"}

    broker._call = fake_call  # type: ignore[method-assign]
    request = ModelRequest((ModelMessage(ModelRole.USER, "hola"),))

    async def submit(priority: int | None) -> None:
        QUEUE_PRIORITY.set(priority)
        await broker._submit(request, _prepare_request(request), CancellationSource().token)

    asyncio.run(submit(10))
    asyncio.run(submit(None))
    assert bodies[0]["priority"] == 10
    assert "priority" not in bodies[1]  # sin turno fijado, el 100 del Broker


async def _receipts(state: _Core, count: int) -> None:
    """El recibo final y los saltos se mandan en segundo plano: esperar a que lleguen."""
    for _ in range(100):
        if sum(path == "/api/v1/operations/receipts" for path, _ in state.calls) >= count:
            return
        await asyncio.sleep(0.05)


def _hops(state: _Core) -> list[dict[str, Any]]:
    return [body for path, body in state.calls if path == "/api/v1/flows/hops"]


def test_flow_id_follows_the_run_to_core(tmp_path: Path, core: tuple[_Core, str]) -> None:
    """El monitor de Core ve la petición de otra app de punta a punta (Athena 0.3.3)."""
    state, url = core
    registry, provider = _registry(tmp_path, AiOsCoreGovernor(url, "tok-athena"))

    async def scenario() -> None:
        run_id = await registry.start(
            "Describe el proyecto", _repo(tmp_path), _READ_ONLY, flow_id="chaty_turno_42"
        )
        await registry.wait(run_id)
        await _receipts(state, 2)
        await registry.shutdown()

    asyncio.run(scenario())
    evaluate = [body for path, body in state.calls if path == "/api/v1/policies/evaluate"]
    assert evaluate[0]["context"]["flow_id"] == "chaty_turno_42"
    receipts = [body for path, body in state.calls if path == "/api/v1/operations/receipts"]
    assert {r["evidence"]["flow_id"] for r in receipts} == {"chaty_turno_42"}
    hops = _hops(state)
    assert len(provider.requests) == sum(h["kind"] == "request" for h in hops)
    assert {(h["flow_id"], h["source_system"], h["target_system"]) for h in hops} == {
        ("chaty_turno_42", "athena", "ai_broker")
    }
    response = next(h for h in hops if h["kind"] == "response")
    assert response["details"]["model"] == "fake"
    assert response["details"]["latency_ms"] >= 0


def test_bad_flow_id_falls_back_to_the_run(tmp_path: Path, core: tuple[_Core, str]) -> None:
    state, url = core
    registry, _ = _registry(tmp_path, AiOsCoreGovernor(url, "tok-athena"))

    async def scenario() -> str:
        run_id = await registry.start(
            "Describe el proyecto", _repo(tmp_path), _READ_ONLY, flow_id="con espacios"
        )
        await registry.wait(run_id)
        await _receipts(state, 2)
        await registry.shutdown()
        return run_id

    run_id = asyncio.run(scenario())
    assert {h["flow_id"] for h in _hops(state)} == {run_id}


def test_monitor_failures_never_reach_the_run(tmp_path: Path, core: tuple[_Core, str]) -> None:
    state, url = core
    state.hops_fail = True
    registry, provider = _registry(tmp_path, AiOsCoreGovernor(url, "tok-athena"))

    async def scenario() -> AgentRunStatus:
        run_id = await registry.start("Describe el proyecto", _repo(tmp_path), _READ_ONLY)
        result = await registry.wait(run_id)
        await _receipts(state, 2)
        await registry.shutdown()
        return result.status

    status = asyncio.run(scenario())
    assert provider.requests and _hops(state)
    assert status is AgentRunStatus.COMPLETED


def test_service_passes_the_flow_header(tmp_path: Path, core: tuple[_Core, str]) -> None:
    state, url = core

    async def scenario() -> int:
        registry, _ = _registry(tmp_path, AiOsCoreGovernor(url, "tok-athena"))
        service = AthenaService(registry, _config())
        host, port = await service.start()
        try:
            body = {"objective": "Describe", "workspace": str(tmp_path / "repo")}
            status, _ = await _request(
                host, port, "POST", "/v1/runs", body=body, headers={"X-Flow-Id": "kanban_7"}
            )
            await _receipts(state, 2)
            return status
        finally:
            await service.stop()

    _repo(tmp_path)
    assert asyncio.run(scenario()) == 201
    evaluate = [body for path, body in state.calls if path == "/api/v1/policies/evaluate"]
    assert evaluate[0]["context"]["flow_id"] == "kanban_7"


def test_service_answers_403_and_503(tmp_path: Path, core: tuple[_Core, str]) -> None:
    state, url = core
    state.authorization = "DENY"

    async def scenario(governor: AiOsCoreGovernor) -> tuple[int, str]:
        registry, _ = _registry(tmp_path, governor)
        service = AthenaService(registry, _config())
        host, port = await service.start()
        try:
            body = {"objective": "Describe", "workspace": str(tmp_path / "repo")}
            return await _request(host, port, "POST", "/v1/runs", body=body)
        finally:
            await service.stop()

    _repo(tmp_path)
    status, body = asyncio.run(scenario(AiOsCoreGovernor(url, "tok-athena")))
    assert status == 403 and json.loads(body)["error"]["code"] == "core_denied"
    status, body = asyncio.run(scenario(AiOsCoreGovernor("http://127.0.0.1:9", "tok-athena")))
    assert status == 503 and json.loads(body)["error"]["code"] == "core_unavailable"


def test_governor_only_with_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATHENA_CORE_URL", raising=False)
    assert governor_from_environment() is None
    monkeypatch.setenv("ATHENA_CORE_URL", "http://127.0.0.1:8000")
    assert governor_from_environment() is not None
    monkeypatch.setenv("ATHENA_CORE_URL", "ftp://x")
    with pytest.raises(ValueError):
        governor_from_environment()
