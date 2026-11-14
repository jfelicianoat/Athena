"""Sin pegar tokens: Core da su conexión a Athena y el token del Broker.

El token del Broker se renueva tras un 403.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from athena.adapters.ai_broker import AiBrokerModelProvider
from athena.adapters.ai_os_core import broker_token_source, governor_from_environment


def _server(handler: type[BaseHTTPRequestHandler]) -> HTTPServer:
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_core_da_conexion_y_token_del_broker_que_se_renueva(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vigente = ["broker-1"]
    vistos: list[str | None] = []

    class Core(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            ok = self.headers.get("Authorization") == "Bearer tok-ath"
            self.send_response(200 if ok else 401)
            self.end_headers()
            self.wfile.write(json.dumps({"token": vigente[0]} if ok else {}).encode())

        def log_message(self, *_: object) -> None:
            pass

    class Broker(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            vistos.append(self.headers.get("x-admin-token"))
            ok = self.headers.get("x-admin-token") == vigente[0]
            self.send_response(200 if ok else 403)
            self.end_headers()
            cuerpo: dict[str, object] = {"ok": True} if ok else {"detail": "ADMIN_AUTH_REQUIRED"}
            self.wfile.write(json.dumps(cuerpo).encode())

        def log_message(self, *_: object) -> None:
            pass

    core, broker = _server(Core), _server(Broker)
    try:
        core_url = f"http://127.0.0.1:{core.server_port}"
        monkeypatch.setenv("AI_OS_CONNECT_DIR", str(tmp_path))
        monkeypatch.delenv("ATHENA_CORE_URL", raising=False)
        monkeypatch.delenv("ATHENA_CORE_TOKEN", raising=False)
        assert broker_token_source() is None and governor_from_environment() is None
        conexion = {"core_url": core_url, "token": "tok-ath"}
        (tmp_path / "athena.json").write_text(json.dumps(conexion))
        assert governor_from_environment() is not None

        source = broker_token_source("pegado-caducado")
        assert source is not None
        provider = AiBrokerModelProvider(f"http://127.0.0.1:{broker.server_port}", source)

        def llamar() -> int:
            return asyncio.run(asyncio.to_thread(provider._blocking_call, "GET", "/x", None, None))[
                0
            ]

        assert llamar() == 200
        vigente[0] = "broker-2"  # el Broker se reinicia; el runner publica el nuevo en Core
        assert llamar() == 200
        assert vistos == ["broker-1", "broker-1", "broker-2"]
    finally:
        core.shutdown()
        broker.shutdown()


def test_sin_localappdata_busca_la_conexion_donde_la_deja_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AT-02: fuera de Windows Core escribe en ~/.ai_os_core; Athena tiene que mirar ahí."""
    from athena.adapters.ai_os_core import core_connection

    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("AI_OS_CONNECT_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    carpeta = tmp_path / ".ai_os_core" / "AIOSCore" / "conexion"
    carpeta.mkdir(parents=True)
    conexion = {"core_url": "http://127.0.0.1:8720", "token": "tok-linux"}
    (carpeta / "athena.json").write_text(json.dumps(conexion), encoding="utf-8")
    assert core_connection() == ("http://127.0.0.1:8720", "tok-linux")
