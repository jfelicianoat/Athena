"""H-13: reanudar un plan cuya tarea estaba trabajando cuando murió el proceso.

Visto con el Broker real (P4, 20-nov): el plan guardado decía `pending` y la reanudación
repetía la tarea a ciegas (202). Lo documentado (README, CURRENT_STATE) es negarse y
nombrar las tareas de resultado desconocido. Aquí, con el Broker falso: el planificador no
da un plan usable, el objetivo entero va como tarea «whole», y el proceso muere mientras
la tarea espera al modelo ([[lento:40]]).

Uso: python h13_plan_interrumpido.py <carpeta_salida>   (sale con 1 si H-13 sigue abierto)
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from dobles import FakeBroker  # noqa: E402
from e2e_servicio import Service, project  # noqa: E402


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="athena-h13-"))
    broker = FakeBroker()
    env = {"ATHENA_PLANNING": "1"}
    svc = Service(work, broker, env)
    state = svc.state
    ws = project(work, "plan")
    _, body = svc.call(
        "POST",
        "/v1/runs",
        {
            "objective": "Crea indice.txt [[lento:40]]",
            "workspace": str(ws),
            "profile": "documents",
            "writes": "allow",
            "exec": "off",
            "deliverables": ["indice.txt"],
            "execution_mode": "hierarchical",
        },
    )
    run = body["run_id"]
    started = False
    for _ in range(120):
        _, history = svc.call("GET", f"/v1/runs/{run}/history")
        events = history.get("events", []) if isinstance(history, dict) else []
        if any(e.get("name") == "task.started" for e in events):
            started = True
            break
        time.sleep(0.5)
    time.sleep(2)
    svc.proc.kill()
    svc.proc.wait(10)
    svc = Service(work, broker, env, state=state)
    try:
        status, answer = svc.call("POST", f"/v1/runs/{run}/resume", {"workspace": str(ws)})
    finally:
        svc.stop()
        broker.close()
    result = {"tarea_empezada": started, "resume_http": status, "resume": answer}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    (out / "h13.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    refused = 400 <= status < 500 and "whole" in json.dumps(answer)
    print("H-13 corregido" if started and refused else "H-13 ABIERTO")
    sys.exit(0 if started and refused else 1)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
