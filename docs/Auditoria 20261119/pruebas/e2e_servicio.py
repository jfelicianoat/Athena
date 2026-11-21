"""Pruebas de extremo a extremo del servicio HTTP de Athena (auditoría 19-nov-2026).

Arranca `python -m athena_service` de verdad, como proceso hijo, contra los dobles de
`dobles.py`. Todo aislado: LOCALAPPDATA, ATHENA_STATE_DIR y AI_OS_CONNECT_DIR apuntan a
carpetas temporales, así que ni el Core ni el Broker reales se tocan.

Uso:  python e2e_servicio.py <carpeta_salida>
Escribe <carpeta_salida>/e2e_servicio.json con cada caso (esperado, observado, estado).
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ATHENA = HERE.parents[2]
sys.path.insert(0, str(HERE))
from dobles import FakeBroker, FakeCore  # noqa: E402

PYTHON = str(ATHENA / ".venv" / "Scripts" / "python.exe")
RESULTS: list[dict[str, Any]] = []


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Service:
    def __init__(
        self,
        base: Path,
        broker: FakeBroker,
        extra: dict[str, str] | None = None,
        state: Path | None = None,
        port: int | None = None,
    ):
        self.base = base
        self.state = state or Path(tempfile.mkdtemp(prefix="state-", dir=base))
        self.port = port or free_port()
        connect = base / "conexion_vacia"
        connect.mkdir(exist_ok=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ATHENA_", "AI_OS_"))}
        env.update(
            {
                "PYTHONPATH": str(ATHENA / "src"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "LOCALAPPDATA": str(base / "localappdata"),
                "AI_OS_CONNECT_DIR": str(connect),
                "ATHENA_STATE_DIR": str(self.state),
                "ATHENA_SERVICE_PORT": str(self.port),
                "ATHENA_BROKER_BASE_URL": broker.url,
                "ATHENA_BROKER_TOKEN": broker.token,
                "ATHENA_PLANNING": "0",
            }
        )
        env.update(extra or {})
        self.env = env
        self.proc = subprocess.Popen(
            [PYTHON, "-m", "athena_service"],
            env=env,
            cwd=str(base),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self.token = ""
        deadline = time.monotonic() + 30
        assert self.proc.stdout is not None
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                break
            if line.startswith("ATHENA_SERVICE_READY "):
                self.token = json.loads(line.split(" ", 1)[1])["token"]
                return
        err = self.proc.stderr.read() if self.proc.stderr else ""
        raise RuntimeError(f"el servicio no arrancó: {err[-2000:]}")

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        token: str | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30,
    ) -> tuple[int, Any]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        h = {"Content-Type": "application/json"}
        tok = self.token if token is None else token
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        h.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        conn.putrequest(method, path, skip_accept_encoding=True)
        for k, v in h.items():
            conn.putheader(k, v.encode("latin-1") if isinstance(v, str) else v)
        conn.putheader("Content-Length", str(len(data or b"")))
        conn.endheaders(data)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", "replace")
        conn.close()
        try:
            return resp.status, json.loads(raw)
        except ValueError:
            return resp.status, raw

    def wait_status(self, run_id: str, final: tuple[str, ...], seconds: float = 90) -> Any:
        deadline = time.monotonic() + seconds
        last: Any = None
        while time.monotonic() < deadline:
            _, last = self.call("GET", f"/v1/runs/{run_id}")
            if isinstance(last, dict) and str(last.get("status")) in final:
                return last
            time.sleep(0.5)
        return last

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def record(
    case: str, goal: str, expected: str, observed: Any, ok: bool | None, note: str = ""
) -> None:
    state = "PASA" if ok else ("FALLA" if ok is False else "OBSERVADO")
    RESULTS.append(
        {
            "id": case,
            "objetivo": goal,
            "esperado": expected,
            "observado": observed,
            "estado": state,
            "nota": note,
        }
    )
    print(f"[{state}] {case}: {goal}\n    observado: {str(observed)[:300]}")


def project(base: Path, name: str) -> Path:
    root = base / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("# Proyecto de prueba\n", encoding="utf-8")
    return root


FINAL = ("completed", "failed", "cancelled", "verification_inconclusive", "recovery_pending")


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="athena-e2e-"))
    broker = FakeBroker()
    svc = Service(base, broker)
    try:
        ws = project(base, "proyecto")
        # --- autenticación
        st, body = svc.call("GET", "/v1/health", token="")
        record("E01", "health público sin token", "200", (st, body), st == 200)
        st, body = svc.call("GET", "/v1/auth/check", token="")
        record("E02", "ruta protegida sin token", "401", st, st == 401)
        st, body = svc.call("GET", "/v1/auth/check", token="malo")
        record("E03", "token incorrecto", "401", st, st == 401)
        st, body = svc.call("GET", "/v1/auth/check", token="tokén-con-tilde")
        record(
            "E04",
            "token con carácter no ASCII",
            "401 (credencial incorrecta)",
            (st, body),
            st == 401,
            "compare_digest con str no ASCII lanza TypeError",
        )

        # --- consulta
        st, body = svc.call(
            "POST",
            "/v1/runs",
            {
                "objective": "¿Qué hay en este proyecto?",
                "workspace": str(ws),
                "profile": "questions",
                "writes": "off",
                "exec": "off",
            },
        )
        run_q = body.get("run_id") if isinstance(body, dict) else None
        final = svc.wait_status(run_q, FINAL) if run_q else None
        record(
            "E05",
            "consulta con perfil questions",
            "201 y run completed",
            (st, final and final.get("status")),
            st == 201 and final and final.get("status") == "completed",
        )
        RESULTS[-1]["detalle"] = final

        # --- documento con escritura permitida y deshacer
        st, body = svc.call(
            "POST",
            "/v1/runs",
            {
                "objective": "Crea la nota [[escribe:nota.txt:hola auditoria]]",
                "workspace": str(ws),
                "profile": "documents",
                "writes": "allow",
                "exec": "off",
                "deliverables": ["nota.txt"],
            },
        )
        run_d = body.get("run_id") if isinstance(body, dict) else None
        final = svc.wait_status(run_d, FINAL) if run_d else None
        exists = (ws / "nota.txt").exists()
        record(
            "E06",
            "documento con writes=allow escribe el entregable",
            "completed y nota.txt en disco",
            (st, final and final.get("status"), exists),
            bool(final and final.get("status") == "completed" and exists),
        )
        RESULTS[-1]["detalle"] = final
        st, points = svc.call("GET", f"/v1/runs/{run_d}/rollback")
        record(
            "E07",
            "puntos de deshacer del run",
            "200 con al menos un punto",
            (st, points),
            st == 200 and bool(points.get("points")) if isinstance(points, dict) else False,
        )
        st, rb = svc.call("POST", f"/v1/runs/{run_d}/rollback", {})
        record(
            "E08",
            "deshacer elimina el fichero creado",
            "200 y nota.txt ya no existe",
            (st, rb, (ws / "nota.txt").exists()),
            st == 200 and not (ws / "nota.txt").exists(),
        )

        # --- writes=ask sin cliente que conteste
        st, body = svc.call(
            "POST",
            "/v1/runs",
            {
                "objective": "Crea [[escribe:pedido.txt:x]]",
                "workspace": str(ws),
                "profile": "documents",
                "writes": "ask",
                "exec": "off",
                "deliverables": ["pedido.txt"],
            },
        )
        run_a = body.get("run_id") if isinstance(body, dict) else None
        final = svc.wait_status(run_a, FINAL, 120) if run_a else None
        record(
            "E09",
            "writes=ask sin cliente conectado: se deniega y no escribe",
            "estado final != completed y pedido.txt no existe",
            (final and final.get("status"), (ws / "pedido.txt").exists()),
            bool(final) and final.get("status") != "completed" and not (ws / "pedido.txt").exists(),
        )
        RESULTS[-1]["detalle"] = final

        # --- idempotencia con otra carga
        key = {"Idempotency-Key": "clave-aud-1"}
        st1, b1 = svc.call(
            "POST",
            "/v1/runs",
            {"objective": "Pregunta A", "workspace": str(ws), "profile": "questions"},
            headers=key,
        )
        st2, b2 = svc.call(
            "POST",
            "/v1/runs",
            {"objective": "Pregunta B distinta", "workspace": str(ws), "profile": "questions"},
            headers=key,
        )
        record(
            "E10",
            "misma Idempotency-Key con otro objetivo",
            "sin regla documentada (ADR-021 solo promete un run por clave): se registra",
            (st1, b1, st2, b2),
            None,
            "OBSERVADO: devuelve el run del primer objetivo; ver R-01",
        )
        if isinstance(b1, dict) and b1.get("run_id"):
            svc.wait_status(b1["run_id"], FINAL)

        # --- modelo que contesta en prosa
        st, body = svc.call(
            "POST",
            "/v1/runs",
            {"objective": "Responde [[prosa]]", "workspace": str(ws), "profile": "questions"},
        )
        run_p = body.get("run_id") if isinstance(body, dict) else None
        final = svc.wait_status(run_p, FINAL, 180) if run_p else None
        record(
            "E11",
            "modelo que nunca da la decisión estructurada",
            "failed, acotado",
            final and (final.get("status"), final.get("error") or final.get("summary")),
            bool(final) and final.get("status") == "failed",
        )
        RESULTS[-1]["detalle"] = final
        RESULTS[-1]["tareas_broker"] = len(broker.submitted)

        # --- fallo no reintentable del broker
        st, body = svc.call(
            "POST",
            "/v1/runs",
            {"objective": "Responde [[falla]]", "workspace": str(ws), "profile": "questions"},
        )
        run_f = body.get("run_id") if isinstance(body, dict) else None
        final = svc.wait_status(run_f, FINAL) if run_f else None
        record(
            "E12",
            "broker con fallo permanente",
            "failed",
            final and final.get("status"),
            bool(final) and final.get("status") == "failed",
        )
        RESULTS[-1]["detalle"] = final

        # --- cancelar y cancelar otra vez
        st, body = svc.call(
            "POST",
            "/v1/runs",
            {
                "objective": "Piensa despacio [[lento:30]]",
                "workspace": str(ws),
                "profile": "questions",
            },
        )
        run_c = body.get("run_id") if isinstance(body, dict) else None
        time.sleep(2)
        st1, _ = svc.call("POST", f"/v1/runs/{run_c}/cancel", {})
        final = svc.wait_status(run_c, FINAL, 30)
        st2, _ = svc.call("POST", f"/v1/runs/{run_c}/cancel", {})
        record(
            "E13",
            "cancelar un run en marcha y repetir",
            "202 → cancelled → 409",
            (st1, final and final.get("status"), st2),
            st1 in (200, 202) and bool(final) and final.get("status") == "cancelled" and st2 == 409,
        )

        # --- workspace inexistente y relativo
        st, body = svc.call("POST", "/v1/runs", {"objective": "x", "workspace": "relativo/x"})
        record("E14", "workspace relativo", "4xx", (st, body), 400 <= st < 500)
        st, body = svc.call(
            "POST", "/v1/runs", {"objective": "x", "workspace": str(base / "no-existe")}
        )
        record("E15", "workspace inexistente", "4xx", (st, body), 400 <= st < 500)
        st, body = svc.call(
            "POST", "/v1/runs", {"objective": "x", "workspace": str(ws), "writes": "quizas"}
        )
        record("E16", "modo de escritura inválido", "400", (st, body), st == 400)

        # --- caída a mitad de run y reanudación
        st, body = svc.call(
            "POST",
            "/v1/runs",
            {"objective": "Tarea larga [[lento:60]]", "workspace": str(ws), "profile": "questions"},
        )
        run_k = body.get("run_id") if isinstance(body, dict) else None
        time.sleep(3)
        svc.proc.kill()
        svc.proc.wait(10)
        svc = Service(base, broker, state=svc.state)
        st, listing = svc.call("GET", "/v1/runs")
        found = None
        if isinstance(listing, dict):
            for item in listing.get("runs", []):
                if item.get("run_id") == run_k:
                    found = item
        record(
            "E17",
            "tras matar el proceso el run queda reanudable",
            "recovery_pending",
            found,
            bool(found) and found.get("status") == "recovery_pending",
        )
        st, res = svc.call("POST", f"/v1/runs/{run_k}/resume", {"workspace": str(ws)})
        record("E18", "reanudar un run interrumpido", "2xx", (st, res), 200 <= st < 300)
        if 200 <= st < 300:
            final = svc.wait_status(run_k, FINAL, 120)
            RESULTS[-1]["final"] = final and final.get("status")

        # --- una consulta no escribe aunque el modelo lo pida (writes forzado a off)
        st, body = svc.call(
            "POST",
            "/v1/runs",
            {
                "objective": "Responde y de paso [[escribe:consulta.txt:no debería]]",
                "workspace": str(ws),
                "profile": "questions",
                "writes": "allow",
            },
        )
        run_w = body.get("run_id") if isinstance(body, dict) else None
        final = svc.wait_status(run_w, FINAL, 120) if run_w else None
        record(
            "E19",
            "perfil questions con el modelo pidiendo write_file (writes=allow pedido)",
            "no se crea consulta.txt (perfil questions sin herramientas de escritura)",
            (st, final and final.get("status"), (ws / "consulta.txt").exists()),
            st in (201, 400) and not (ws / "consulta.txt").exists(),
        )
        RESULTS[-1]["detalle"] = final
    finally:
        svc.stop()

    # ---------------------------------------------------------------- AI OS Core
    for mode, expect_status in (
        ("allow", 201),
        ("deny", 403),
        ("require", 403),
        ("html", 503),
        ("lista", 503),
        ("500", 503),
    ):
        core = FakeCore(mode)
        before = len(broker.submitted)
        svc = Service(base, broker, {"ATHENA_CORE_URL": core.url, "ATHENA_CORE_TOKEN": "c"})
        try:
            ws = project(base, f"core-{mode}")
            st, body = svc.call(
                "POST",
                "/v1/runs",
                {"objective": "Pregunta gobernada", "workspace": str(ws), "profile": "questions"},
                headers={"X-Flow-Id": "flujo-aud"},
            )
            final = None
            if st == 201:
                final = svc.wait_status(body["run_id"], FINAL)
                time.sleep(1.5)
            sent = len(broker.submitted) - before
            receipts = [r.get("status") for r in core.posted("/api/v1/operations/receipts")]
            hops = [h.get("kind") for h in core.posted("/api/v1/flows/hops")]
            ok = st == expect_status and (sent > 0) == (expect_status == 201)
            record(
                f"C-{mode}",
                f"Core responde «{mode}» a policies/evaluate",
                f"{expect_status}; tareas al broker solo si 201",
                {
                    "http": st,
                    "cuerpo": body,
                    "tareas_broker": sent,
                    "recibos": receipts,
                    "saltos": hops,
                    "final": final and final.get("status"),
                },
                ok,
            )
        finally:
            svc.stop()
            core.close()

    # Core caído
    svc = Service(base, broker, {"ATHENA_CORE_URL": f"http://127.0.0.1:{free_port()}"})
    try:
        ws = project(base, "core-caido")
        st, body = svc.call(
            "POST", "/v1/runs", {"objective": "x", "workspace": str(ws), "profile": "questions"}
        )
        record("C-caido", "Core no escucha", "503 core_unavailable", (st, body), st == 503)
    finally:
        svc.stop()

    # Core implícito y caído: solo el fichero de conexión, apuntando a un puerto sin nadie
    dead = base / "conexion_core_caido"
    dead.mkdir(exist_ok=True)
    (dead / "athena.json").write_text(
        json.dumps({"core_url": f"http://127.0.0.1:{free_port()}", "token": "t"}),
        encoding="utf-8",
    )
    svc = Service(base, broker, {"AI_OS_CONNECT_DIR": str(dead)})
    try:
        ws = project(base, "core-implicito-caido")
        st, body = svc.call(
            "POST", "/v1/runs", {"objective": "x", "workspace": str(ws), "profile": "questions"}
        )
        record(
            "C-implicito-caido",
            "sin ATHENA_CORE_URL, con athena.json y Core parado",
            "se registra (impacto de H-04)",
            (st, body),
            None,
        )
    finally:
        svc.stop()

    # Core implícito: sin ATHENA_CORE_URL pero con fichero de conexión
    core = FakeCore("deny")
    connect = base / "conexion_con_athena"
    connect.mkdir(exist_ok=True)
    (connect / "athena.json").write_text(
        json.dumps({"core_url": core.url, "token": "t"}), encoding="utf-8"
    )
    svc = Service(base, broker, {"AI_OS_CONNECT_DIR": str(connect)})
    try:
        ws = project(base, "core-implicito")
        st, body = svc.call(
            "POST", "/v1/runs", {"objective": "x", "workspace": str(ws), "profile": "questions"}
        )
        record(
            "C-implicito",
            "sin ATHENA_CORE_URL pero con conexion/athena.json",
            "README (0.3.8): el fichero de conexión activa Core → decide Core (DENY → 403)",
            (st, body),
            st == 403,
            "en 0.3.7 el README decía que sin ATHENA_CORE_URL nada cambiaba (H-04)",
        )
    finally:
        svc.stop()
        core.close()

    # Token del broker dado por Core (sin ATHENA_BROKER_TOKEN)
    core = FakeCore("allow", broker_token=broker.token)
    svc = Service(
        base,
        broker,
        {"ATHENA_CORE_URL": core.url, "ATHENA_CORE_TOKEN": "c", "ATHENA_BROKER_TOKEN": ""},
    )
    try:
        ws = project(base, "core-token")
        st, body = svc.call(
            "POST", "/v1/runs", {"objective": "x", "workspace": str(ws), "profile": "questions"}
        )
        final = svc.wait_status(body["run_id"], FINAL) if st == 201 else None
        record(
            "C-token",
            "token del Broker servido por Core",
            "201 y completed",
            (st, final and final.get("status")),
            st == 201 and bool(final) and final.get("status") == "completed",
        )
    finally:
        svc.stop()
        core.close()

    broker.close()
    (out / "e2e_servicio.json").write_text(
        json.dumps(RESULTS, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    fails = sum(1 for r in RESULTS if r["estado"] == "FALLA")
    print(f"\n{len(RESULTS)} casos, {fails} en FALLA. Base temporal: {base}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
