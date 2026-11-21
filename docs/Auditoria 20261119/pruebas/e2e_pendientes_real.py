"""Pruebas que quedaron pendientes, contra el AI_Broker REAL (20-nov-2026).

Mismo aislamiento que `e2e_broker_real.py` (estado, LOCALAPPDATA y AI_OS_CONNECT_DIR
temporales; token pedido a Core con un GET). Casos:

  P1  aprobación remota por SSE, haciendo de cliente como ChatyGPT (ack + allow)
  P2  System-1 en modo sombra (goal completion + reviewer gate)
  P3  delegación (`delegate_task`) pedida en el objetivo
  P4  reanudar un plan interrumpido (kill a mitad de una tarea del grafo)
  P5  catálogo crudo del Broker (qué es `gpt-oss-120b`, H-10)
  P6  R06 repetido con H-09 corregido (corregir un test con verificación real)

Uso: python e2e_pendientes_real.py <carpeta_salida> [P1 P2 ...]
"""

from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from broker_real import BROKER_URL, broker_token  # noqa: E402
from e2e_broker_real import MODEL, unittest_project  # noqa: E402
from e2e_servicio import FINAL, RESULTS, Service, project, record  # noqa: E402

COMMON = {"ATHENA_ALLOWED_MODELS": MODEL, "ATHENA_PREFERRED_MODEL": MODEL}


def events(svc: Service, run_id: str) -> list[dict[str, Any]]:
    _, history = svc.call("GET", f"/v1/runs/{run_id}/history")
    return history.get("events", []) if isinstance(history, dict) else []


def names(svc: Service, run_id: str) -> list[str]:
    return [str(e.get("name")) for e in events(svc, run_id)]


class SseClient(threading.Thread):
    """Un cliente como ChatyGPT: se suscribe, y contesta cada aprobación viva."""

    def __init__(self, svc: Service, run_id: str, decision: str = "allow") -> None:
        super().__init__(daemon=True)
        self.svc, self.run_id, self.decision = svc, run_id, decision
        self.subscriber: str | None = None
        self.answered: list[dict[str, Any]] = []
        self.final: str | None = None
        self.error: str | None = None

    def run(self) -> None:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", self.svc.port, timeout=1800)
            conn.request(
                "GET",
                f"/v1/runs/{self.run_id}/events",
                headers={
                    "Authorization": f"Bearer {self.svc.token}",
                    "Accept": "text/event-stream",
                },
            )
            stream = conn.getresponse()
            kind, data = "", ""
            while True:
                raw = stream.readline()
                if not raw:
                    return
                line = raw.decode("utf-8").rstrip("\r\n")
                if line.startswith("event: "):
                    kind = line[7:]
                elif line.startswith("data: "):
                    data += line[6:]
                elif line == "" and data:
                    self._frame(kind, json.loads(data))
                    kind, data = "", ""
                    if self.final:
                        return
        except Exception as exc:  # el cliente registra; la prueba decide
            self.error = repr(exc)

    def _frame(self, kind: str, body: dict[str, Any]) -> None:
        if kind == "state":
            self.subscriber = body.get("subscriber_id")
            for pending in body.get("pending_approvals") or []:
                self._answer(str(pending.get("request_id")))
            return
        name = body.get("name")
        payload = body.get("payload") or {}
        if name == "permission.requested" and payload.get("awaiting_decision"):
            self._answer(str(payload.get("request_id") or body.get("correlation_id")))
        if name in ("agent.completed", "agent.failed", "agent.cancelled"):
            self.final = str(name)

    def _answer(self, request_id: str) -> None:
        header = {"X-Athena-Subscriber": self.subscriber or ""}
        ack = self.svc.call(
            "POST", f"/v1/runs/{self.run_id}/approvals/{request_id}/ack", {}, headers=header
        )
        decided = self.svc.call(
            "POST",
            f"/v1/runs/{self.run_id}/approvals/{request_id}",
            {"decision": self.decision},
            headers=header,
        )
        self.answered.append({"request_id": request_id, "ack": ack[0], "decision": decided})


def p1_sse(svc: Service, work: Path) -> None:
    ws = project(work, "p1")
    st, body = svc.call(
        "POST",
        "/v1/runs",
        {
            "objective": "Crea aviso.txt con una sola línea que diga «revisado por Athena».",
            "workspace": str(ws),
            "profile": "documents",
            "writes": "ask",
            "exec": "off",
            "deliverables": ["aviso.txt"],
            "execution_mode": "direct",
        },
    )
    run = body.get("run_id") if isinstance(body, dict) else None
    client = SseClient(svc, run) if run else None
    if client:
        client.start()
    final = svc.wait_status(run, FINAL, 1800) if run else None
    if client:
        client.join(10)
    text = (ws / "aviso.txt").read_text("utf-8") if (ws / "aviso.txt").exists() else None
    record(
        "P1",
        "aprobación remota por SSE (ack + allow con X-Athena-Subscriber)",
        "aprobación contestada con 200, completed y aviso.txt",
        {
            "http": st,
            "final": final and final.get("status"),
            "respuestas": client and client.answered,
            "fin_en_stream": client and client.final,
            "error_cliente": client and client.error,
            "fichero": text,
        },
        bool(final)
        and final.get("status") == "completed"
        and bool(client and client.answered)
        and all(a["decision"][0] == 200 for a in client.answered)
        and text is not None,
    )


def p2_system1(work: Path, broker: Any) -> None:
    env = {
        **COMMON,
        "ATHENA_PLANNING": "0",
        "ATHENA_SYSTEM1_GOAL_COMPLETION": "true",
        "ATHENA_SYSTEM1_REVIEWER_GATE": "true",
        "ATHENA_SYSTEM1_SHADOW_MODE": "true",
    }
    svc = Service(work, broker, env)
    try:
        ws = project(work, "p2")
        (ws / "README.md").write_text("# Inventario\n\nCuenta botellas.\n", encoding="utf-8")
        st, body = svc.call(
            "POST",
            "/v1/runs",
            {
                "objective": "Crea resumen.txt con una línea sobre el proyecto (lee README.md).",
                "workspace": str(ws),
                "profile": "documents",
                "writes": "allow",
                "exec": "off",
                "deliverables": ["resumen.txt"],
                "acceptance_criteria": ["resumen.txt existe y tiene una línea"],
            },
        )
        run = body.get("run_id") if isinstance(body, dict) else None
        final = svc.wait_status(run, FINAL, 1800) if run else None
        judged = [
            e.get("payload")
            for e in (events(svc, run) if run else [])
            if str(e.get("name", "")).startswith("system1.")
        ]
        record(
            "P2",
            "System-1 en sombra (goal completion + reviewer gate) con el Broker real",
            "el run termina igual que sin System-1 y deja eventos system1.* (juicio o fallback)",
            {"http": st, "final": final and final.get("status"), "system1": judged[:8]},
            bool(final) and final.get("status") == "completed" and bool(judged),
        )
    finally:
        svc.stop()


def p3_delegation(svc: Service, work: Path) -> None:
    ws = project(work, "p3")
    (ws / "a.py").write_text("def suma(a, b):\n    return a + b\n", encoding="utf-8")
    (ws / "b.py").write_text("def resta(a, b):\n    return a - b\n", encoding="utf-8")
    st, body = svc.call(
        "POST",
        "/v1/runs",
        {
            "objective": "Usa la herramienta delegate_task con el perfil explorer para que un "
            "delegado liste las funciones de este proyecto; después respóndeme con su lista.",
            "workspace": str(ws),
            "profile": "questions",
            "execution_mode": "direct",
        },
    )
    run = body.get("run_id") if isinstance(body, dict) else None
    final = svc.wait_status(run, FINAL, 1800) if run else None
    seen = names(svc, run) if run else []
    record(
        "P3",
        "delegación pedida en el objetivo (delegate_task, explorer)",
        "si el modelo delega: subagent.started/completed y run completed (depende del modelo)",
        {
            "http": st,
            "final": final and final.get("status"),
            "subagente": [n for n in seen if n.startswith("subagent")],
            "respuesta": (final or {}).get("working_memory", {}).get("facts"),
        },
        None
        if "subagent.started" not in seen
        else bool(final) and final.get("status") == "completed",
        "OBSERVADO si el modelo no delega: no es un fallo de Athena",
    )


def p4_plan_resume(work: Path, broker: Any) -> None:
    env = {**COMMON, "ATHENA_PLANNING": "1"}
    svc = Service(work, broker, env)
    state = svc.state
    ws = project(work, "p4")
    (ws / "README.md").write_text("# Inventario\n\nCuenta botellas.\n", encoding="utf-8")
    _, body = svc.call(
        "POST",
        "/v1/runs",
        {
            "objective": "Crea indice.txt con una línea por fichero del proyecto.",
            "workspace": str(ws),
            "profile": "documents",
            "writes": "allow",
            "exec": "off",
            "deliverables": ["indice.txt"],
            "execution_mode": "hierarchical",
        },
    )
    run = body.get("run_id") if isinstance(body, dict) else None
    started = False
    deadline = time.monotonic() + 900
    while run and time.monotonic() < deadline:
        if "task.started" in names(svc, run):
            started = True
            break
        time.sleep(2)
    time.sleep(3)
    svc.proc.kill()
    svc.proc.wait(10)
    svc = Service(work, broker, env, state=state)
    try:
        _, listing = svc.call("GET", "/v1/runs")
        found = next(
            (
                r
                for r in (listing.get("runs", []) if isinstance(listing, dict) else [])
                if r.get("run_id") == run
            ),
            None,
        )
        st_r, res = svc.call("POST", f"/v1/runs/{run}/resume", {"workspace": str(ws)})
        record(
            "P4",
            "reanudar un plan con una tarea interrumpida",
            "README/CURRENT_STATE: se niega y nombra las tareas de resultado desconocido",
            {
                "tarea_empezada": started,
                "listado": found and found.get("status"),
                "resume_http": st_r,
                "resume": res,
            },
            started and 400 <= st_r < 500 and "task" in json.dumps(res).lower(),
        )
    finally:
        svc.stop()


def p5_catalogue(out: Path) -> None:
    parts = urlsplit(BROKER_URL)
    from athena.adapters.ai_broker import broker_ssl_context

    conn = http.client.HTTPSConnection(
        parts.hostname, parts.port, context=broker_ssl_context(), timeout=60
    )
    conn.request("GET", "/api/v1/models")
    raw = conn.getresponse().read()
    (out / "broker_models.json").write_bytes(raw)
    data = json.loads(raw)
    models = data.get("models", []) if isinstance(data, dict) else []
    hit = [m for m in models if "gpt-oss" in json.dumps(m)]
    record(
        "P5",
        "catálogo crudo del Broker: ¿qué es gpt-oss-120b?",
        "se registra (H-10)",
        {"total": len(models), "gpt_oss": hit},
        None,
    )


def p6_r06(svc: Service, work: Path) -> None:
    calc = unittest_project(work / "p6")
    _, body = svc.call(
        "POST",
        "/v1/runs",
        {
            "objective": "El test de test_calc.py falla. Corrige calc.py para que pase, "
            "sin tocar el test.",
            "workspace": str(calc),
            "profile": "software_engineering",
            "writes": "allow",
            "exec": "allow",
            "execution_mode": "direct",
        },
    )
    run = body.get("run_id") if isinstance(body, dict) else None
    final = svc.wait_status(run, FINAL, 2400) if run else None
    wm = (final or {}).get("working_memory", {})
    record(
        "P6",
        "R06 repetido con H-09 corregido",
        "no acaba failed por el process_timeout de una herramienta y se verifica",
        {
            "final": final and final.get("status"),
            "errores": wm.get("errors"),
            "verificacion": (final or {}).get("verification"),
            "calc": (calc / "calc.py").read_text("utf-8"),
        },
        bool(final)
        and not (
            final.get("status") == "failed"
            and any(e.get("code") == "process_timeout" for e in wm.get("errors") or [])
        )
        and bool((final or {}).get("verification")),
    )


def main(out: Path, only: set[str]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="athena-pend-"))
    (work / "p6").mkdir()
    broker = SimpleNamespace(url=BROKER_URL, token=broker_token())
    started = time.monotonic()
    want = lambda case: not only or case in only  # noqa: E731
    if want("P5"):
        p5_catalogue(out)
    svc = Service(work, broker, {**COMMON, "ATHENA_PLANNING": "0"})  # type: ignore[arg-type]
    try:
        if want("P1"):
            p1_sse(svc, work)
        if want("P6"):
            p6_r06(svc, work)
        if want("P3"):
            p3_delegation(svc, work)
    finally:
        svc.stop()
    if want("P2"):
        p2_system1(work, broker)
    if want("P4"):
        p4_plan_resume(work, broker)
    (out / "e2e_pendientes_real.json").write_text(
        json.dumps(RESULTS, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    fails = sum(1 for r in RESULTS if r["estado"] == "FALLA")
    print(f"\n{len(RESULTS)} casos, {fails} en FALLA, {time.monotonic() - started:.0f} s. {work}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), set(sys.argv[2:]))
