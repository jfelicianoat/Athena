"""Recorrido real: AI OS Core dirige runs del servicio de Athena sobre el AI Broker de verdad.

Uso: python scripts/verify_ai_os_core.py <core_url> <token_athena> <token_director>
     <broker_url> <carpeta>
     con ATHENA_BROKER_TOKEN en el entorno. Opcional ATHENA_ALLOWED_MODELS (por defecto un
     modelo de Ollama: sin fijarlo el Broker puede enrutar a LM Studio y morir por response_format).
Core ha de tener las políticas de `sembrar_ecosistema.py` y el token por app de athena; el
token director (común) pone y quita la política DENY de la prueba. El estado de Athena y el
proyecto de prueba viven en <carpeta>: el estado real de %LOCALAPPDATA%\\Athena no se toca.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CORE, TOK_ATH, TOK_DIR, BROKER, WORK = sys.argv[1:6]
ROOT = Path(__file__).resolve().parents[1]
work = Path(WORK)
(work / "repo").mkdir(parents=True, exist_ok=True)
(work / "repo" / "notas.txt").write_text("uno\ndos\ntres\n", encoding="utf-8")
ok = 0


def step(name: str, cond: bool, detail: str = "") -> None:
    global ok
    print(f"[{'OK ' if cond else 'FALLO'}] {name} {detail}", flush=True)
    if not cond:
        sys.exit(1)
    ok += 1


def call(base: str, method: str, path: str, token: str, body: Any = None) -> tuple[int, Any]:
    request = urllib.request.Request(
        base + path,
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


def start_athena(core_url: str) -> tuple[subprocess.Popen[str], str, str]:
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "ATHENA_STATE_DIR": str(
            work / f"state-{time.time_ns()}"
        ),  # cada arranque, sin runs previos
        "ATHENA_SERVICE_PORT": "0",
        "ATHENA_BROKER_BASE_URL": BROKER,
        "ATHENA_ALLOWED_MODELS": os.environ.get("ATHENA_ALLOWED_MODELS", "muse-glimmer:latest"),
        "ATHENA_PLANNING": "0",
        "ATHENA_CORE_URL": core_url,
        "ATHENA_CORE_TOKEN": TOK_ATH,
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "athena_service"],
        env=env,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    assert process.stdout is not None
    for line in process.stdout:
        if line.startswith("ATHENA_SERVICE_READY "):
            ready = json.loads(line.split(" ", 1)[1])
            return process, ready["base_url"], ready["token"]
    raise SystemExit("Athena no arrancó")


def new_run(base: str, token: str) -> tuple[int, Any]:
    return call(
        base,
        "POST",
        "/v1/runs",
        token,
        {
            "objective": "¿Cuántas líneas tiene notas.txt? Léelo y responde solo el número.",
            "workspace": str(work / "repo"),
            "writes": "off",
            "exec": "off",
            "profile": "questions",
        },
    )


deny = {
    "policy_id": "verify_ath_deny",
    "policy_key": "verify_ath_deny",
    "scope": "athena:broker",
    "decision_if_matched": "DENY",
    "conditions": [{"field": "operation_type", "operator": "equals", "value": "agent_run"}],
}

athena, base, token = start_athena(CORE)
try:
    step("athena arranca con Core configurado", call(base, "GET", "/v1/health", token)[0] == 200)
    step(
        "director pone DENY", call(CORE, "POST", "/api/v1/policies", TOK_DIR, deny)[0] in (200, 201)
    )
    status, body = new_run(base, token)
    step(
        "con DENY Athena contesta 403 core_denied",
        status == 403 and body["error"]["code"] == "core_denied",
        str(body)[:160],
    )
    step("con DENY no hay run", call(base, "GET", "/v1/runs", token)[1]["runs"] == [])
    step(
        "director quita DENY",
        call(CORE, "POST", "/api/v1/policies", TOK_DIR, {**deny, "enabled": False})[0]
        in (200, 201),
    )

    status, body = new_run(base, token)
    step("con ALLOW el run se crea", status == 201, str(body)[:160])
    run_id = body["run_id"]
    final = None
    for _ in range(300):
        final = call(base, "GET", f"/v1/runs/{run_id}", token)[1]["status"]
        if final in ("completed", "failed", "cancelled"):
            break
        time.sleep(2)
    history = call(base, "GET", f"/v1/runs/{run_id}/history", token)[1]
    models = {
        e["payload"].get("model") for e in history["events"] if e["name"] == "model.completed"
    }
    step("el run llegó al Broker real", bool(models - {None}), f"estado={final} modelos={models}")
    receipt: dict[str, Any] = {}
    for _ in range(20):
        receipt = call(
            CORE,
            "POST",
            "/api/v1/operations/status",
            TOK_DIR,
            {"operation_id": run_id, "target_service_id": "athena"},
        )[1]
        if receipt.get("status") not in (None, "accepted", "not_seen"):
            break
        time.sleep(0.5)
    expected = {"completed": "succeeded", "failed": "failed_terminal", "cancelled": "cancelled"}[
        final
    ]
    step(
        "Core tiene el recibo terminal del run",
        receipt.get("status") == expected,
        str(receipt)[:200],
    )
finally:
    athena.terminate()
    athena.wait(timeout=30)

athena, base, token = start_athena("http://127.0.0.1:9")
try:
    status, body = new_run(base, token)
    step(
        "con Core caído Athena contesta 503 y no crea run",
        status == 503
        and body["error"]["code"] == "core_unavailable"
        and call(base, "GET", "/v1/runs", token)[1]["runs"] == [],
        str(body)[:160],
    )
finally:
    athena.terminate()
    athena.wait(timeout=30)
print(f"\n{ok} comprobaciones OK")
