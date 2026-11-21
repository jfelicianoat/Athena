"""Regresiones de la auditoría del 19-nov-2026 (docs/Auditoria 20261119).

H-09 vive en `test_agent_loop.py` y `test_process_tools.py`, junto a sus pruebas gemelas.
"""

from __future__ import annotations

import asyncio
import json
import threading
import tkinter as tk
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from athena.adapters.ai_broker import AiBrokerModelProvider
from athena.adapters.ai_os_core import (
    AiOsCoreGovernor,
    CoreUnavailableError,
    governor_from_environment,
)
from athena.agent_loop import AgentRunResult, AgentRunStatus
from athena.cancellation import CancellationSource
from athena.errors import ModelPermanentError, ModelTransientError
from athena_desktop.config import DEFAULT_URLS, ProviderKind, SettingsStore
from athena_desktop.presentacion import present_result
from athena_desktop.runtime import (
    DesktopStores,
    RunConfiguration,
    RunSummary,
    build_registry,
    check_connection,
    run_options,
)

ROOT = Path(__file__).resolve().parents[1]

Route = Callable[[str, str, dict[str, Any]], tuple[int, bytes]]


class _Server:
    """Un servidor HTTP mínimo cuyas respuestas decide una función."""

    def __init__(self, route: Route) -> None:
        self.calls: list[tuple[str, str]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None:
                pass

            def _answer(self, verb: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                owner.calls.append((verb, self.path))
                headers = {key.lower(): value for key, value in self.headers.items()}
                status, raw = route(verb, self.path, {"body": body, "headers": headers})
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                self._answer("GET")

            def do_POST(self) -> None:
                self._answer("POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _json(payload: object, status: int = 200) -> tuple[int, bytes]:
    return status, json.dumps(payload).encode()


@pytest.fixture
def broker() -> Iterator[_Server]:
    def route(verb: str, path: str, request: dict[str, Any]) -> tuple[int, bytes]:
        if path == "/health":
            return _json({"status": "healthy"})
        if path == "/api/v1/models":
            return _json({"models": [{"name": "qwen3.8:27b", "capabilities": ["completion"]}]})
        if request["headers"].get("x-admin-token") != "tok-de-core":
            return _json({"error": {"code": "ADMIN_AUTH_REQUIRED"}}, 403)
        if path == "/api/v1/auth/check":
            return _json({"authenticated": True, "auth_required": True})
        return _json({}, 404)

    server = _Server(route)
    yield server
    server.close()


def _core(answer: Callable[[], tuple[int, bytes]]) -> _Server:
    def route(verb: str, path: str, request: dict[str, Any]) -> tuple[int, bytes]:
        if path == "/api/v1/credentials/broker":
            return _json({"token": "tok-de-core"})
        if path == "/api/v1/policies/evaluate":
            return answer()
        return _json({"ok": True})

    return _Server(route)


def _connect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, core_url: str) -> None:
    folder = tmp_path / "conexion"
    folder.mkdir(exist_ok=True)
    (folder / "athena.json").write_text(
        json.dumps({"core_url": core_url, "token": "tok-app"}), encoding="utf-8"
    )
    monkeypatch.setenv("AI_OS_CONNECT_DIR", str(folder))
    monkeypatch.delenv("ATHENA_CORE_URL", raising=False)


# ------------------------------------------------------------------ H-01


def test_h01_probar_conexion_usa_el_token_que_da_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broker: _Server
) -> None:
    core = _core(lambda: _json({"authorization": "ALLOW", "executable": True}))
    try:
        _connect(tmp_path, monkeypatch, core.url)
        configuration = RunConfiguration(
            workspace=tmp_path,
            objective="probar",
            provider=ProviderKind.AI_BROKER,
            base_url=broker.url,
            model="qwen3.8:27b",
        )
        report = asyncio.run(check_connection(configuration))
    finally:
        core.close()
    assert report.ok, report.message
    assert "AI OS Core" in report.message
    assert report.models == ("qwen3.8:27b",)


def test_h01_sin_token_ni_core_lo_dice(tmp_path: Path, broker: _Server) -> None:
    configuration = RunConfiguration(
        workspace=tmp_path,
        objective="probar",
        provider=ProviderKind.AI_BROKER,
        base_url=broker.url,
    )
    report = asyncio.run(check_connection(configuration))
    assert not report.ok
    assert "Falta el token" in report.message


# ------------------------------------------------------------------ H-02


def test_h02_un_token_no_ascii_es_401_y_no_500(tmp_path: Path) -> None:
    import http.client

    from athena.adapters.service import RunRegistry
    from athena.adapters.service.server import AthenaService, ServiceConfig
    from athena.events import InMemoryEventBus
    from athena.session_store import SqliteSessionStore
    from athena.stores import InMemoryToolResultStore
    from athena.testing import FakeModelProvider

    async def scenario() -> int:
        registry = RunRegistry(
            FakeModelProvider([]),
            InMemoryEventBus(),
            SqliteSessionStore(tmp_path / "sessions.db"),
            InMemoryToolResultStore(),
        )
        service = AthenaService(registry, ServiceConfig(token="secreto", port=0))
        _, port = await service.start()
        try:

            def ask() -> int:
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                connection.putrequest("GET", "/v1/auth/check")
                connection.putheader("Authorization", "Bearer tokén".encode("latin-1"))
                connection.endheaders()
                status = connection.getresponse().status
                connection.close()
                return status

            return await asyncio.to_thread(ask)
        finally:
            await service.stop()
            await registry.shutdown()

    assert asyncio.run(scenario()) == 401


# ------------------------------------------------------------------ H-03


@pytest.mark.parametrize(
    "answer",
    [
        lambda: (200, b"<html>proxy</html>"),
        lambda: _json(["ALLOW"]),
    ],
    ids=["html", "lista"],
)
def test_h03_core_sin_un_objeto_json_es_no_disponible(
    answer: Callable[[], tuple[int, bytes]],
) -> None:
    core = _core(answer)
    try:
        governor = AiOsCoreGovernor(core.url, "t")
        with pytest.raises(CoreUnavailableError):
            asyncio.run(governor.authorize("run-1", {}))
    finally:
        core.close()


def test_h03_un_recibo_que_no_entiende_core_no_lanza() -> None:
    server = _Server(lambda verb, path, request: (200, b"<html>no</html>"))
    try:
        asyncio.run(AiOsCoreGovernor(server.url, "t").receipt("run-1", "succeeded", {}))
    finally:
        server.close()


# ------------------------------------------------------------------ H-04


def test_h04_core_implicito_esta_documentado_y_el_error_dice_de_donde_sale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead = probe.getsockname()[1]
    _connect(tmp_path, monkeypatch, f"http://127.0.0.1:{dead}")
    governor = governor_from_environment()
    assert governor is not None  # conducta decidida: el fichero de conexión activa Core
    with pytest.raises(CoreUnavailableError) as raised:
        asyncio.run(governor.authorize("run-1", {}))
    assert "athena.json" in raised.value.message
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme[readme.index("## AI OS Core as director") :]
    section = section[: section.index("\n## ", 5)]
    assert "conexion" in section
    module = (ROOT / "src" / "athena" / "adapters" / "ai_os_core.py").read_text("utf-8")
    assert "sin `ATHENA_CORE_URL` todo sigue igual" not in module


# ------------------------------------------------------------------ H-08, H-10


def test_h08_la_url_por_defecto_es_la_del_broker_del_ecosistema() -> None:
    assert DEFAULT_URLS["ai_broker"] == "https://192.168.1.52:8765"


def test_h10_el_modelo_escrito_viaja_como_eleccion_del_run(tmp_path: Path) -> None:
    configuration = RunConfiguration(
        workspace=tmp_path,
        objective="x",
        provider=ProviderKind.AI_BROKER,
        base_url="https://broker",
        model="qwen3.8:27b",
        token="t",
    )
    options = run_options(configuration)
    assert options.model == "qwen3.8:27b"
    registry = build_registry(configuration, DesktopStores(tmp_path / "state"))
    # El registro lo ofrece y lo resuelve tal cual: el adaptador lo manda con
    # `fallback_allowed: false` porque lo nombra el run (ADR-034).
    assert registry.model_for(options) == "qwen3.8:27b"


def test_h10_sin_modelo_lo_elige_el_broker(tmp_path: Path) -> None:
    configuration = RunConfiguration(
        workspace=tmp_path,
        objective="x",
        provider=ProviderKind.AI_BROKER,
        base_url="https://broker",
        token="t",
    )
    options = run_options(configuration)
    assert options.model == ""
    assert build_registry(configuration, DesktopStores(tmp_path / "s")).model_for(options) == ""


def test_h10_reanudar_acepta_el_modelo_con_que_se_creo(tmp_path: Path) -> None:
    configuration = RunConfiguration(
        workspace=tmp_path,
        objective="x",
        provider=ProviderKind.AI_BROKER,
        base_url="https://broker",
        model="nuevo",
        token="t",
    )
    registry = build_registry(configuration, DesktopStores(tmp_path / "s"), also_models=("viejo",))
    from athena.adapters.service import RunOptions

    assert registry.model_for(RunOptions(model="viejo")) == "viejo"


# ------------------------------------------------------------------ H-11, R-04


def _failed(error: Exception) -> AgentRunResult:
    from athena.errors import AthenaRuntimeError
    from athena.state import SessionState

    assert isinstance(error, AthenaRuntimeError)
    return AgentRunResult(AgentRunStatus.FAILED, SessionState("s-1", "w-1"), error=error)


def test_h11_un_modelo_que_responde_sin_decision_no_es_un_proveedor_caido() -> None:
    view = present_result(
        _failed(ModelTransientError("AI_Broker returned an empty tool decision")),
        task_kind="documents",
    )
    assert "no respondió" not in view.explanation
    assert "decisión" in view.explanation


def test_r04_el_motivo_del_broker_llega_al_mensaje() -> None:
    def route(verb: str, path: str, request: dict[str, Any]) -> tuple[int, bytes]:
        if verb == "POST":
            return _json({"task_id": "t1"})
        return _json(
            {"status": "failed", "error": {"code": "MODEL_ERROR", "message": "sin memoria"}}
        )

    server = _Server(route)
    try:
        provider = AiBrokerModelProvider(server.url, "t", poll_interval_seconds=0.01)
        from athena.models import ModelMessage, ModelRequest, ModelRole

        request = ModelRequest(messages=(ModelMessage(ModelRole.USER, "hola"),))
        with pytest.raises(ModelPermanentError) as raised:
            asyncio.run(provider.complete(request, CancellationSource().token))
    finally:
        server.close()
    assert "MODEL_ERROR: sin memoria" in raised.value.message


# ------------------------------------------------------------------ H-05, H-06 (ventana)


@pytest.fixture(scope="module")
def tk_root() -> Iterator[tk.Tk]:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk no esta disponible: {exc}")
    root.withdraw()
    try:
        yield root
    finally:
        root.destroy()


@pytest.fixture
def window(tk_root: tk.Tk, tmp_path: Path) -> Iterator[Any]:
    from athena_desktop.app import AthenaDesktopApp

    top = tk.Toplevel(tk_root)
    top.withdraw()
    app = AthenaDesktopApp(
        top,  # type: ignore[arg-type]
        SettingsStore(tmp_path / "settings.json"),
        state_dir=tmp_path / "state",
    )
    try:
        yield app
    finally:
        app.closing = True
        top.destroy()


def test_h05_reanudar_usa_el_tipo_del_trabajo_guardado(
    window: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tkinter import messagebox

    app = window
    summary = RunSummary(
        run_id="run-1",
        status="recovery_pending",
        objective="Crea informe.txt",
        project=str(tmp_path),
        updated_at="",
        verification="",
        files_modified=(),
        errors=(),
        task_kind="documents",
        resumable=True,
        undoable=0,
    )
    app.history = [summary]
    app.tree.insert("", tk.END, iid="run-1", values=("", "", "", ""))
    app.tree.selection_set("run-1")
    app.task_kind.set("question")  # lo que haya en el formulario
    app.writes.set("Desactivado")
    started: list[str | None] = []
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)
    monkeypatch.setattr(app, "_start", lambda **k: started.append(k.get("resume_run_id")))

    app._resume_selected()

    assert started == ["run-1"]
    assert app.task_kind.get() == "documents"
    # Y con ese tipo, la configuracion valida aunque el formulario tuviera escrituras off.
    app.token.set("t")
    app._configuration_from_form().validate()


def test_h06_una_consulta_dice_que_no_escribe(window: Any, tmp_path: Path) -> None:
    app = window
    app.workspace.set(str(tmp_path))
    app.writes.set("Preguntar")
    configuration = RunConfiguration(
        workspace=tmp_path,
        objective="¿Qué hay?",
        provider=ProviderKind.AI_BROKER,
        base_url="https://broker",
        token="t",
        task_kind="question",
    )
    app.worker = None
    monkey_thread = app._run_worker
    app._run_worker = lambda *a, **k: None
    try:
        app._begin(configuration, None)
    finally:
        app._run_worker = monkey_thread
    assert "archivos: no (consulta)" in app.p_config.get()
    assert str(app.objective.cget("state")) == tk.DISABLED


# ------------------------------------------------------------------ CLI (20-nov)


def _cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *extra: str) -> tuple[int, str]:
    import contextlib
    import io

    from athena import cli
    from athena.models import ModelResponse
    from athena.testing import FakeModelProvider

    (tmp_path / "README.md").write_text("# repo\n", encoding="utf-8")
    monkeypatch.setattr(
        cli,
        "OpenAICompatibleModelProvider",
        lambda *a, **k: FakeModelProvider([ModelResponse("Respuesta del doble.", "f", "stop")]),
    )
    arguments = cli._parser().parse_args(
        [str(tmp_path), "--state-dir", str(tmp_path / ".estado"), *extra]
    )
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        code = asyncio.run(cli._run(arguments, CancellationSource()))
    return code, printed.getvalue()


def test_cli_una_consulta_de_solo_lectura_se_responde_sin_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El ejemplo del README («Explain the authentication flow») en un proyecto sin checks
    acababa `verification_inconclusive`, código 1 y sin imprimir la respuesta."""
    code, out = _cli(tmp_path, monkeypatch, "-o", "Explain the authentication flow")
    assert code == 0
    assert "Respuesta del doble." in out


def test_cli_reanudar_un_id_desconocido_lo_dice_sin_traza(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, out = _cli(tmp_path, monkeypatch, "--resume", "no-existe")
    assert code == 2
    assert "--list-sessions" in out


# ------------------------------------------------------------------ H-12


def test_h12_el_catalogo_cuenta_los_modelos_locales_online_y_no_los_de_la_nube() -> None:
    """El Broker real marca `online` los modelos de LM Studio y de la nube (708 de 736) y
    Athena solo contaba `available`: la comprobación I-06 no veía modelos que el Broker usa."""
    models = [
        {"name": "ollama-ok", "status": "available", "capabilities": ["completion"]},
        {
            "name": "lmstudio-ok",
            "status": "online",
            "deployment": "local",
            "capabilities": ["completion"],
        },
        {
            "name": "lmstudio-juez",
            "status": "online",
            "deployment": "local",
            "capabilities": ["decision"],
        },
        {
            "name": "gpt-oss-120b",
            "status": "online",
            "deployment": "cloud",
            "capabilities": ["completion"],
        },
        {"name": "apagado", "status": "offline", "capabilities": ["completion"]},
    ]
    server = _Server(lambda verb, path, request: _json({"models": models}))
    try:
        provider = AiBrokerModelProvider(server.url, "t")
        catalogue = asyncio.run(provider.model_capabilities(CancellationSource().token))
    finally:
        server.close()
    assert set(catalogue) == {"ollama-ok", "lmstudio-ok", "lmstudio-juez"}
    assert catalogue["lmstudio-juez"] == ("decision",)


# ------------------------------------------------------------------ TG-1 (Telegram)


def test_tg1_un_run_inconcluso_no_se_anuncia_como_parado() -> None:
    """Con la concesión por defecto de Telegram (ejecución off) casi todo run acaba
    `verification_inconclusive`, y el canal lo anunciaba como «Athena stopped»."""
    from athena.channels import ChannelIdentity, ResponseKind, render_event
    from athena.events import EventName, RuntimeEvent

    identity = ChannelIdentity("telegram", "111", "111")
    event = RuntimeEvent(
        EventName.AGENT_FAILED,
        "run-1",
        {"error_code": "verification_inconclusive", "message": "no checks"},
    )
    response = render_event(event, identity)
    assert response is not None
    assert response.text.startswith("Athena finished but could not verify")
    assert response.kind is ResponseKind.ERROR  # no se viste de éxito


# ------------------------------------------------------------------ accesibilidad (20-nov)


def test_a11y_los_textos_de_estado_tienen_contraste_wcag_aa(window: Any) -> None:
    """El verde de éxito daba 4,46:1 sobre el fondo, por debajo del 4,5:1 de WCAG AA."""
    from tkinter import ttk

    root = window.root

    def luminance(colour: str) -> float:
        def channel(value: int) -> float:
            c = value / 65535
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

        r, g, b = (channel(v) for v in root.winfo_rgb(colour))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    style = ttk.Style(root)
    background = style.lookup("TFrame", "background") or str(root.cget("background"))
    for name in ("TLabel", "Hint.TLabel", "Ok.TLabel", "Bad.TLabel"):
        foreground = style.lookup(name, "foreground") or "black"
        high, low = sorted((luminance(foreground), luminance(background)), reverse=True)
        assert (high + 0.05) / (low + 0.05) >= 4.5, name
