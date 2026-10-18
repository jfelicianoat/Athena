"""Regresiones de la auditoria del 9/10-oct-2026 (I-01 a I-09).

Cada prueba nombra la incidencia que defiende. Las del servicio van por HTTP real, como
las veria ChatyGPT o el runner de Agora.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from athena.adapters.service import AthenaService
from athena.adapters.service.approvals import PendingApproval
from athena.adapters.service.server.transporte import _ROUTES
from athena.agent_loop import AgentRunStatus
from athena.cancellation import CancellationSource
from athena.errors import AthenaRuntimeError
from athena.events import EventName, InMemoryEventBus, RuntimeEvent
from athena.metrics import MetricsCollector
from athena.models import ModelResponse, ModelToolCall
from athena.permissions import PermissionDecision
from athena.session_store import SessionRecord
from athena.state import AgentStatus
from athena.system1 import Judgment, System1, System1Config
from athena.testing import FakeModelProvider
from athena.types import JSONObject
from athena.working_state import WorkingState
from athena.workspace import Workspace
from athena_desktop.config import ProviderKind
from athena_desktop.presentacion import queue_line, turn_budget_warning
from athena_desktop.runtime import (
    DesktopStores,
    RunConfiguration,
    broker_queue,
    check_connection,
    list_runs,
    model_problem,
    run_athena,
    verification_warning,
)
from test_service_adapter import (
    _config,
    _permission,
    _registry,
    _request,
    _sandbox,
    _ScriptedProvider,
)


def _raw(host: str, port: int, method: str, path: str) -> asyncio.Future[bytes]:
    async def go() -> bytes:
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(
            f"{method} {path} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer test-token\r\n"
            "Content-Length: 0\r\n\r\n".encode("latin-1")
        )
        await writer.drain()
        raw = await reader.read()
        writer.close()
        return raw

    return asyncio.ensure_future(go())


def test_i02_i03_an_unknown_run_is_404_on_every_run_route(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = AthenaService(_registry(tmp_path, _ScriptedProvider([])), _config())
        host, port = await service.start()
        try:
            routes: tuple[tuple[str, str, JSONObject | None], ...] = (
                ("POST", "/v1/runs/no-existe/cancel", None),
                ("GET", "/v1/runs/no-existe/rollback", None),
                ("GET", "/v1/runs/no-existe/goal", None),
                ("POST", "/v1/runs/no-existe/goal", {"objective": "x", "base_revision": 1}),
                ("POST", "/v1/runs/no-existe/resume", {"workspace": str(tmp_path)}),
            )
            for method, path, body in routes:
                status, text = await _request(host, port, method, path, body=body)
                assert status == 404, (method, path, status, text)
                assert json.loads(text)["error"]["code"] == "not_found"
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_i02_cancelling_a_finished_run_is_a_conflict_not_a_bad_request(tmp_path: Path) -> None:
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        provider = _ScriptedProvider([ModelResponse("Nada que cambiar.", "scripted", "stop")])
        registry = _registry(tmp_path, provider)
        service = AthenaService(registry, _config())
        host, port = await service.start()
        try:
            status, text = await _request(
                host,
                port,
                "POST",
                "/v1/runs",
                body={
                    "objective": "Explica calc.py",
                    "workspace": str(root),
                    "writes": "off",
                    "exec": "off",
                },
            )
            assert status == 201
            run_id = json.loads(text)["run_id"]
            await registry.wait(run_id)
            for _ in range(100):
                if run_id not in registry.live_ids():
                    break
                await asyncio.sleep(0.05)
            status, text = await _request(host, port, "POST", f"/v1/runs/{run_id}/cancel")
            assert status == 409, text
            assert json.loads(text)["error"]["code"] == "run_finished"
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_i04_a_known_route_with_another_method_is_405_with_allow(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = AthenaService(_registry(tmp_path, _ScriptedProvider([])), _config())
        host, port = await service.start()
        try:
            raw = await _raw(host, port, "DELETE", "/v1/runs")
            head, _, body = raw.partition(b"\r\n\r\n")
            assert head.startswith(b"HTTP/1.1 405 ")
            assert b"\r\nAllow: GET, POST\r\n" in head + b"\r\n"
            assert json.loads(body)["error"]["code"] == "method_not_allowed"
            # Lo que no existe con ningun metodo sigue siendo 404.
            status, _ = await _request(host, port, "DELETE", "/v1/nada")
            assert status == 404
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_every_route_in_the_table_is_served(tmp_path: Path) -> None:
    """La tabla del 405 no puede desfasarse del enrutado: cada entrada tiene manejador."""

    async def scenario() -> None:
        service = AthenaService(_registry(tmp_path, _ScriptedProvider([])), _config())
        host, port = await service.start()
        try:
            for template, method in _ROUTES:
                path = template.replace("{}", "x")
                status, text = await _request(host, port, method, path)
                assert status != 405, (method, path)
                assert "No route for" not in text, (method, path, text)
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_i09_the_stream_never_offers_an_approval_that_is_no_longer_open(tmp_path: Path) -> None:
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        service = AthenaService(_registry(tmp_path, _ScriptedProvider([])), _config())
        loop = asyncio.get_running_loop()
        pending = PendingApproval(
            "req-1",
            "run-1",
            _permission(Workspace.from_path(root)),
            loop.create_future(),
            time.monotonic() + 30,
        )
        event = RuntimeEvent(
            EventName.PERMISSION_REQUESTED,
            "run-1",
            {**pending.to_json(), "awaiting_decision": True},
            "req-1",
        )

        def sent() -> dict[str, object]:
            payload = service._as_sent(event)["payload"]
            assert isinstance(payload, dict)
            return payload

        # Nunca registrada o ya descartada: caducada.
        assert sent()["awaiting_decision"] is False
        assert sent()["stale"] is True
        # Viva: sale tal cual.
        service.approvals.register(pending)
        assert sent()["awaiting_decision"] is True
        assert "stale" not in sent()
        # Contestada: ya no se ofrece.
        service.approvals.resolve("req-1", PermissionDecision.ALLOW)
        assert sent()["stale"] is True

    asyncio.run(scenario())


def test_i09_answering_a_dead_approval_says_it_is_gone_and_to_keep_listening(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        service = AthenaService(_registry(tmp_path, _ScriptedProvider([])), _config())
        host, port = await service.start()
        try:
            status, text = await _request(
                host, port, "POST", "/v1/runs/run-1/approvals/req-viejo/ack"
            )
            assert status == 404
            assert "Keep reading the event stream" in json.loads(text)["error"]["message"]
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_i01_health_is_public_by_design_and_says_so() -> None:
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    assert "authentication is required even though it is bound to loopback" not in readme
    assert "except `GET /v1/health`, which is public on purpose" in readme


# ------------------------------------------------------------------ escritorio: I-06 e I-07

_CATALOGUE = {
    "models": [
        {"name": "clef-flash:latest", "status": "available", "capabilities": ["decision"]},
        {"name": "qwen3.8:27b", "status": "available", "capabilities": ["completion", "tools"]},
        {"name": "apagado:1b", "status": "unavailable", "capabilities": ["completion"]},
    ]
}


class _FakeBroker(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        bodies = {
            "/health": {"status": "healthy"},
            "/api/v1/auth/check": {"authenticated": True, "auth_required": True},
            "/api/v1/models": _CATALOGUE,
            "/api/v1/queue": {"pending": [{}, {}], "active": [{}], "terminal": []},
        }
        body = json.dumps(bodies.get(self.path, {})).encode("utf-8")
        self.send_response(200 if self.path in bodies else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@pytest.fixture
def broker_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeBroker)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _broker_configuration(root: Path, url: str, model: str) -> RunConfiguration:
    return RunConfiguration(
        workspace=root,
        objective="¿Qué hace calc.py?",
        provider=ProviderKind.AI_BROKER,
        base_url=url,
        model=model,
        token="t",
        timeout_seconds=30,
    )


def test_i06_testing_the_connection_rejects_a_model_without_completion(
    tmp_path: Path, broker_url: str
) -> None:
    report = asyncio.run(
        check_connection(_broker_configuration(tmp_path, broker_url, "clef-flash:latest"))
    )
    assert report.ok is False
    assert "clef-flash:latest" in report.message and "completion" in report.message
    # La lista que rellena el desplegable: solo los que sirven y estan disponibles.
    assert report.models == ("qwen3.8:27b",)

    report = asyncio.run(
        check_connection(_broker_configuration(tmp_path, broker_url, "qwen3.8:27b"))
    )
    assert report.ok is True
    assert report.models == ("qwen3.8:27b",)


def test_i06_a_model_the_catalogue_does_not_list_is_not_refused() -> None:
    assert model_problem("enrutado-por-el-broker", {"x": ("completion",)}) is None
    assert model_problem("", {}) is None
    assert model_problem("m", {"m": ("decision",)}) is not None


def test_i06_starting_with_an_unusable_model_fails_before_any_run_exists(
    tmp_path: Path, broker_url: str
) -> None:
    state = tmp_path / "state"
    configuration = _broker_configuration(tmp_path, broker_url, "clef-flash:latest")
    with pytest.raises(AthenaRuntimeError, match="no sirve para trabajar con Athena"):
        asyncio.run(
            run_athena(
                configuration,
                CancellationSource(),
                on_event=lambda event: None,
                on_permission=lambda request: PermissionDecision.DENY,
                state_dir=state,
            )
        )
    assert asyncio.run(list_runs(state)) == []


def test_i07_a_first_turn_that_leaves_no_room_for_three_warns() -> None:
    assert turn_budget_warning(100, 600) is None
    warning = turn_budget_warning(250, 600)
    assert warning is not None and warning.tone == "warn"
    assert "Tiempo máximo" in warning.text


def test_mejora3_a_change_that_cannot_be_verified_is_announced_before_it_starts(
    tmp_path: Path,
) -> None:
    def configuration(kind: str, execution: str) -> RunConfiguration:
        return RunConfiguration(
            workspace=tmp_path,
            objective="Corrige calc.py",
            provider=ProviderKind.OPENAI_COMPATIBLE,
            base_url="http://127.0.0.1:1/v1",
            writes="ask",
            execution=execution,  # type: ignore[arg-type]
            task_kind=kind,  # type: ignore[arg-type]
        )

    warning = verification_warning(configuration("change", "ask"))
    assert warning is not None and "no define comprobaciones" in warning
    assert verification_warning(configuration("question", "off")) is None

    (tmp_path / "AGENTS.md").write_text(
        "# P\n\n## Verification\n\n```\npython -m pytest -q\n```\n", encoding="utf-8"
    )
    assert verification_warning(configuration("change", "ask")) is None
    warning = verification_warning(configuration("change", "off"))
    assert warning is not None and "Ejecutar código del proyecto" in warning


def test_i05_the_history_survives_the_process_that_wrote_it(tmp_path: Path) -> None:
    """Run en este proceso; el historial se lee desde OTRO, como al reabrir la ventana."""
    root = tmp_path / "repo"
    root.mkdir()
    state = tmp_path / "state"
    provider = FakeModelProvider([ModelResponse("Devuelve la suma.", "fake", "stop")])
    configuration = RunConfiguration(
        workspace=root,
        objective="¿Qué devuelve calc.py?",
        provider=ProviderKind.OPENAI_COMPATIBLE,
        base_url="http://127.0.0.1:1/v1",
        model="fake",
    )
    with patch("athena_desktop.runtime.build_provider", return_value=provider):
        result = asyncio.run(
            run_athena(
                configuration,
                CancellationSource(),
                on_event=lambda event: None,
                on_permission=lambda request: PermissionDecision.DENY,
                state_dir=state,
            )
        )
    script = (
        "import asyncio, json, sys\n"
        "from pathlib import Path\n"
        "from athena_desktop.runtime import list_runs\n"
        "runs = asyncio.run(list_runs(Path(sys.argv[1])))\n"
        "print(json.dumps([[r.run_id, r.status, r.objective] for r in runs]))\n"
    )
    src = Path(__file__).resolve().parents[1] / "src"
    completed = subprocess.run(
        [sys.executable, "-c", script, str(state)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONPATH": str(src)},
        timeout=60,
        check=True,
    )
    runs = json.loads(completed.stdout)
    assert len(runs) == 1
    assert runs[0][1] == result.status.value
    assert runs[0][2] == "¿Qué devuelve calc.py?"


def _documents(root: Path) -> RunConfiguration:
    return RunConfiguration(
        workspace=root,
        objective="Escribe nota.txt",
        provider=ProviderKind.OPENAI_COMPATIBLE,
        base_url="http://127.0.0.1:1/v1",
        model="fake",
        writes="allow",
        task_kind="documents",
        deliverables=("nota.txt",),
    )


def _run_documents(root: Path, provider: FakeModelProvider, state: Path) -> object:
    with patch("athena_desktop.runtime.build_provider", return_value=provider):
        return asyncio.run(
            run_athena(
                _documents(root),
                CancellationSource(),
                on_event=lambda event: None,
                on_permission=lambda request: PermissionDecision.DENY,
                state_dir=state,
            )
        )


def test_a03_a_model_that_breaks_after_writing_the_deliverable_is_verified_not_failed(
    tmp_path: Path,
) -> None:
    """Medido con Agora el 17-oct: entregable escrito y el broker corta con PROMPT_ECHOED."""
    root = tmp_path / "ws"
    root.mkdir()
    write = ModelToolCall("w", "write_file", {"path": "nota.txt", "content": "hola\n"})
    # Tras la escritura el guion se agota: el doble lanza ModelPermanentError.
    provider = FakeModelProvider([ModelResponse("", "fake", "tool_calls", tool_calls=(write,))])
    result = _run_documents(root, provider, tmp_path / "state")
    assert (root / "nota.txt").read_text(encoding="utf-8") == "hola\n"
    assert result.status is AgentRunStatus.COMPLETED  # type: ignore[attr-defined]


def test_a03_a_model_that_breaks_before_writing_anything_still_fails(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    result = _run_documents(root, FakeModelProvider([]), tmp_path / "state")
    assert result.status is AgentRunStatus.FAILED  # type: ignore[attr-defined]


def test_a_run_whose_process_died_is_resumable_as_soon_as_the_history_is_read(
    tmp_path: Path,
) -> None:
    """Medido con la ventana real: tras una caida el run seguia «En curso» y sin Reanudar."""
    state = tmp_path / "state"
    stores = DesktopStores(state)
    store = stores.sessions
    record = SessionRecord(
        session_id="caido",
        workspace_id="proj-x",
        status=AgentStatus.RUNNING,
        working_memory=WorkingState(objective="algo"),
    )
    asyncio.run(store.save(record))
    asyncio.run(
        store.save_manifest("caido", {"objective": "algo", "workspace_root": str(tmp_path)})
    )
    connection = sqlite3.connect(state / "sessions.db")
    connection.execute(
        "UPDATE session_owners SET pid = ? WHERE session_id = ?", (999_999_999, "caido")
    )
    connection.commit()
    connection.close()
    runs = asyncio.run(list_runs(state))
    assert [(run.run_id, run.resumable) for run in runs] == [("caido", True)]


# ------------------------------------------------------------------ mejora 10: System-1


def test_mejora10_an_oversized_judgment_is_not_sent_and_falls_back(tmp_path: Path) -> None:
    """El broker no juzga recortes: pasado el tope ni se envia, y se sigue sin juicio."""

    class CountingJudge:
        def __init__(self) -> None:
            self.calls = 0

        async def judge(self, request: object, cancellation: object) -> Judgment:
            self.calls += 1
            return Judgment(True, True, 0.99, "ollama_system1", "nimble", fallback_used=False)

    async def scenario() -> None:
        bus = InMemoryEventBus()
        seen: list[RuntimeEvent] = []
        bus.subscribe(seen.append)
        judge = CountingJudge()
        config = System1Config(goal_completion=True, shadow_mode=False, max_input_chars=500)
        system = System1(judge, config, bus)
        token = CancellationSource().token
        big = await system.completed({"output": "x" * 2_000}, token, session_id="run")
        assert big is None and judge.calls == 0
        judged = [e for e in seen if e.name is EventName.SYSTEM1_JUDGED]
        assert judged[-1].payload["reason_code"] == "INPUT_TOO_LARGE"
        assert judged[-1].payload["fallback"] is True
        small = await system.completed({"output": "ok"}, token, session_id="run")
        assert small is True and judge.calls == 1

    asyncio.run(scenario())


def test_mejora10_the_input_cap_comes_from_the_environment() -> None:
    config = System1Config.from_environment({"ATHENA_SYSTEM1_MAX_INPUT_CHARS": "1234"})
    assert config.max_input_chars == 1234
    assert System1Config.from_environment({}).max_input_chars == 32_000
    with pytest.raises(ValueError):
        System1Config(max_input_chars=0)


def test_mejora10_case_b_counts_failures_after_context_was_filtered() -> None:
    collector = MetricsCollector()
    collector.observe(RuntimeEvent(EventName.VERIFICATION_FAILED, "sin-filtro", {}))
    collector.observe(RuntimeEvent(EventName.SYSTEM1_CONTEXT, "filtrado", {"excluded": 2}))
    collector.observe(RuntimeEvent(EventName.VERIFICATION_FAILED, "filtrado", {}))
    key = "verification_failures_after_context_filtering"
    filtered, unfiltered = collector.get("filtrado"), collector.get("sin-filtro")
    assert filtered is not None and unfiltered is not None
    assert filtered.system1[key] == 1
    assert key not in unfiltered.system1


# ------------------------------------------------------------------ mejora 6: cola del broker


def test_mejora6_the_window_reads_how_busy_the_broker_is(tmp_path: Path, broker_url: str) -> None:
    configuration = _broker_configuration(tmp_path, broker_url, "qwen3.8:27b")
    assert asyncio.run(broker_queue(configuration)) == (2, 1)
    offline = _broker_configuration(tmp_path, "http://127.0.0.1:1", "qwen3.8:27b")
    assert asyncio.run(broker_queue(offline)) is None  # sin broker no se inventa una cola


def test_mejora6_busy_is_said_in_words() -> None:
    assert queue_line(0, 1).startswith("Libre")
    assert "ocupado" in queue_line(2, 1)
