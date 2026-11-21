"""El CLI `athena` de extremo a extremo contra un doble OpenAI-compatible (20-nov-2026).

No hay Ollama ni LM Studio en marcha en este equipo, así que el «modelo» es un servidor
mínimo con tool-calling nativo:
  turno 1: llama a `glob` (o `write_file` si el objetivo contiene «ESCRIBE»)
  turno 2: mensaje final
Casos: consulta de solo lectura; escritura con --writes ask contestando «n» y «y» por la
consola; --list-sessions; --resume de un id inexistente.

Uso: python cli_e2e.py <carpeta_salida>
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ATHENA = Path(__file__).resolve().parents[3]
PYTHON = str(ATHENA / ".venv" / "Scripts" / "python.exe")


class FakeOpenAI:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None:
                pass

            def _send(self, payload: Any) -> None:
                raw = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                self._send({"data": [{"id": "doble"}]})

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(body)
                self._send(owner.answer(body))

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @staticmethod
    def answer(body: dict[str, Any]) -> dict[str, Any]:
        messages = body.get("messages", [])
        user = next((m["content"] for m in messages if m.get("role") == "user"), "")
        used_tool = any(m.get("role") == "tool" for m in messages)
        if not used_tool:
            if "ESCRIBE" in user:
                call = {
                    "name": "write_file",
                    "arguments": json.dumps({"path": "cli.txt", "content": "hola\n"}),
                }
            else:
                call = {"name": "glob", "arguments": json.dumps({"pattern": "*"})}
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "c1", "type": "function", "function": call}],
            }
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": "Listo: respuesta del doble."}
            finish = "stop"
        return {
            "id": "x",
            "model": "doble",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def cli(args: list[str], stdin: str = "", env: dict[str, str] | None = None) -> dict[str, Any]:
    proc = subprocess.run(
        [PYTHON, "-X", "utf8", "-m", "athena.cli", *args],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        env=env,
    )
    return {"rc": proc.returncode, "out": proc.stdout[-1500:], "err": proc.stderr[-1500:]}


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="athena-cli-"))
    ws = base / "repo"
    ws.mkdir()
    (ws / "README.md").write_text("# repo\n", encoding="utf-8")
    model = FakeOpenAI()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ATHENA_", "AI_OS_"))}
    env.update(
        {
            "PYTHONPATH": str(ATHENA / "src"),
            "LOCALAPPDATA": str(base / "local"),
            "AI_OS_CONNECT_DIR": str(base / "vacia"),
        }
    )
    (base / "vacia").mkdir()
    common = [
        str(ws),
        "--base-url",
        model.url,
        "--model",
        "doble",
        "--state-dir",
        str(base / "state"),
    ]
    results: dict[str, Any] = {}
    try:
        results["C1_consulta"] = cli([*common, "-o", "¿Qué hay aquí?"], env=env)
        results["C2_escritura_denegada"] = cli(
            [*common, "-o", "ESCRIBE el fichero", "--writes", "ask"], stdin="n\n", env=env
        )
        results["C2_fichero_tras_denegar"] = (ws / "cli.txt").exists()
        results["C3_escritura_permitida"] = cli(
            [*common, "-o", "ESCRIBE el fichero", "--writes", "ask"], stdin="y\n", env=env
        )
        results["C3_fichero_tras_permitir"] = (ws / "cli.txt").exists()
        results["C4_list_sessions"] = cli([*common, "--list-sessions"], env=env)
        results["C5_resume_inexistente"] = cli([*common, "--resume", "no-existe"], env=env)
        results["peticiones_al_modelo"] = len(model.requests)
    finally:
        model.close()
    (out / "cli_e2e.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
