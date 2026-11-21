"""Dobles HTTP para la auditoría del 19-nov-2026: un AI_Broker y un AI OS Core falsos.

Solo stdlib. Sirven para manejar el servicio y la ventana de Athena de punta a punta sin
tocar el Broker ni el Core reales (ni gastar una llamada a un modelo).

Broker falso: el «modelo» lee marcas en el objetivo del usuario:
  [[escribe:RUTA:TEXTO]]  primer turno write_file; cuando ve su resultado, mensaje final.
  [[prosa]]               contesta siempre en prosa (decisión no estructurada).
  [[lento:N]]             la tarea queda «running» N segundos antes de contestar.
  [[falla]]               la tarea termina `failed` (no reintentable).
  sin marca               mensaje final «Respuesta de prueba».
Core falso: modo de `policies/evaluate` configurable (allow, deny, require, html,
lista, 500) y registro de recibos, saltos y peticiones.
"""

from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

_MARK_WRITE = re.compile(r"\[\[escribe:([^:\]]+):([^\]]*)\]\]")
_MARK_SLOW = re.compile(r"\[\[lento:(\d+)\]\]")
#: [[bash:COMANDO:SEGUNDOS]] primer turno bash con ese plazo; después, mensaje final.
_MARK_BASH = re.compile(r"\[\[bash:([^:\]]+):(\d+)\]\]")


class _Base(BaseHTTPRequestHandler):
    server_version = "doble/1"

    def log_message(self, *_: Any) -> None:  # silencio
        pass

    def _json(self, status: int, payload: Any, raw: bytes | None = None) -> None:
        body = raw if raw is not None else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}") if length else {}


class FakeBroker:
    def __init__(self, token: str = "tok-broker", models: dict[str, list[str]] | None = None):
        self.token = token
        self.models = models or {
            "modelo-bueno": ["completion", "decision"],
            "modelo-solo-decision": ["decision"],
        }
        self.tasks: dict[str, dict[str, Any]] = {}
        self.submitted: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        owner = self

        class Handler(_Base):
            def do_GET(self) -> None:
                if self.path in ("/health", "/api/v1/health"):
                    return self._json(200, {"status": "ok"})
                if self.path == "/api/v1/models":
                    return self._json(
                        200,
                        {
                            "models": [
                                {"name": n, "capabilities": c, "status": "available"}
                                for n, c in owner.models.items()
                            ]
                        },
                    )
                if self.path == "/api/v1/queue":
                    return self._json(200, {"queued": 0, "running": 0})
                if self.headers.get("x-admin-token") != owner.token:
                    return self._json(403, {"error": {"code": "ADMIN_AUTH_REQUIRED"}})
                if self.path.startswith("/api/v1/auth/check"):
                    return self._json(200, {"authenticated": True, "auth_required": True})
                if self.path.startswith("/api/v1/dashboard/tasks"):
                    return self._json(200, {"tasks": []})
                m = re.match(r"^/api/v1/tasks/([\w-]+)$", self.path)
                if m:
                    return self._json(200, owner._poll(m.group(1)))
                return self._json(404, {"error": "no"})

            def do_POST(self) -> None:
                if self.headers.get("x-admin-token") != owner.token:
                    return self._json(403, {"error": {"code": "ADMIN_AUTH_REQUIRED"}})
                if self.path == "/api/v1/tasks":
                    return self._json(202, owner._submit(self._body()))
                return self._json(404, {"error": "no"})

            def do_DELETE(self) -> None:
                return self._json(200, {"ok": True})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def _submit(self, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            task_id = f"t{len(self.submitted) + 1}"
            self.submitted.append(body)
            prompt = str(body.get("content", {}).get("prompt", ""))
            slow = _MARK_SLOW.search(_objective(prompt))
            self.tasks[task_id] = {
                "prompt": prompt,
                "ready_at": time.monotonic() + (int(slow.group(1)) if slow else 0),
            }
            return {"task_id": task_id, "status": "queued"}

    def _poll(self, task_id: str) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        if task is None:
            return {"status": "not_found"}
        if time.monotonic() < task["ready_at"]:
            return {"task_id": task_id, "status": "running"}
        prompt = task["prompt"]
        objective = _objective(prompt)
        if "[[falla]]" in objective:
            return {
                "task_id": task_id,
                "status": "failed",
                "error": {"code": "MODEL_ERROR", "message": "falla simulada", "retryable": False},
            }
        if "[[prosa]]" in objective:
            return _done(task_id, "Claro, aquí tienes una respuesta en prosa sin JSON.")
        bash = _MARK_BASH.search(objective)
        if bash and "Assistant requested tool calls" not in prompt:
            # Una sola vez: el comando con su plazo, como hizo qwen3.8:27b en R06.
            decision = {
                "kind": "tool_calls",
                "message": "",
                "tool_calls": [
                    {
                        "call_id": "verify-1",
                        "name": "bash",
                        "arguments": {
                            "command": bash.group(1),
                            "timeout_seconds": int(bash.group(2)),
                        },
                    }
                ],
            }
            return _done(task_id, json.dumps(decision), structured=decision)
        write = _MARK_WRITE.search(objective)
        if write and "Tool write_file result" not in prompt:
            decision = {
                "kind": "tool_calls",
                "message": "",
                "tool_calls": [
                    {
                        "call_id": "c1",
                        "name": "write_file",
                        "arguments": {"path": write.group(1), "content": write.group(2) + "\n"},
                    }
                ],
            }
        else:
            decision = {"kind": "message", "message": "Respuesta de prueba", "tool_calls": []}
        return _done(task_id, json.dumps(decision), structured=decision)


def _objective(prompt: str) -> str:
    # El objetivo va en el primer bloque «User:»; las marcas solo se buscan ahí, para que
    # el «modelo» no reaccione a lo que escribe la herramienta.
    m = re.search(r"User: (.*?)(?=\n\n(?:Assistant|Tool|System|User)\b|\Z)", prompt, re.S)
    return m.group(1) if m else prompt


def _done(task_id: str, content: str, structured: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "assistant_content": content,
        "model_used": {"model": "modelo-bueno"},
        "usage": {"tokens_input": 10, "tokens_output": 5},
    }
    if structured is not None:
        result["structured_output"] = structured
    return {"task_id": task_id, "status": "completed", "result": result}


class FakeCore:
    """`mode`: allow | deny | require | html | lista | 500 | lento."""

    def __init__(self, mode: str = "allow", broker_token: str | None = None):
        self.mode = mode
        self.broker_token = broker_token
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        owner = self

        class Handler(_Base):
            def do_GET(self) -> None:
                owner.requests.append(("GET", self.path, {}))
                if self.path == "/api/v1/credentials/broker":
                    if owner.broker_token:
                        return self._json(200, {"token": owner.broker_token})
                    return self._json(404, {"detail": "sin token"})
                return self._json(404, {})

            def do_POST(self) -> None:
                body = self._body()
                owner.requests.append(("POST", self.path, body))
                if self.path == "/api/v1/policies/evaluate":
                    mode = owner.mode
                    if mode == "html":
                        return self._json(200, None, raw=b"<html>proxy</html>")
                    if mode == "lista":
                        return self._json(200, ["ALLOW"])
                    if mode == "500":
                        return self._json(500, {"detail": "boom"})
                    if mode == "lento":
                        time.sleep(15)
                    auth = {"allow": "ALLOW", "deny": "DENY", "require": "REQUIRE_APPROVAL"}
                    return self._json(
                        200,
                        {
                            "authorization": auth.get(mode, "ALLOW"),
                            "executable": mode == "allow",
                            "trace_id": "tr-1",
                        },
                    )
                return self._json(200, {"ok": True})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def posted(self, path: str) -> list[dict[str, Any]]:
        return [body for verb, p, body in self.requests if verb == "POST" and p == path]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
