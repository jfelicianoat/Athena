"""Recorrido del servicio de Athena contra el AI_Broker REAL (auditoría 19-nov-2026).

Mismo arranque aislado que `e2e_servicio.py` (estado, LOCALAPPDATA y AI_OS_CONNECT_DIR
temporales: el Core real no recibe recibos ni saltos); solo el Broker es el de verdad. El
token se lee de Core con un GET y pasa al servicio por variable de entorno.

Uso: python e2e_broker_real.py <carpeta_salida>
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import e2e_servicio as base_e2e  # noqa: E402
from broker_real import BROKER_URL, broker_token  # noqa: E402
from e2e_servicio import FINAL, RESULTS, Service, record  # noqa: E402

MODEL = "qwen3.8:27b"


def history_models(svc: Service, run_id: str) -> list[str]:
    _, history = svc.call("GET", f"/v1/runs/{run_id}/history")
    events = history.get("events", []) if isinstance(history, dict) else []
    return [
        str(e.get("payload", {}).get("model"))
        for e in events
        if isinstance(e, dict) and e.get("name") == "model.completed"
    ]


def unittest_project(base: Path) -> Path:
    root = base / "calc"
    root.mkdir()
    (root / "calc.py").write_text(
        "def total(items):\n    return sum(items) + 1\n", encoding="utf-8"
    )
    (root / "test_calc.py").write_text(
        "import unittest\n\nfrom calc import total\n\n\n"
        "class TotalTest(unittest.TestCase):\n"
        "    def test_total(self):\n        self.assertEqual(total([1, 2, 3]), 6)\n\n\n"
        'if __name__ == "__main__":\n    unittest.main()\n',
        encoding="utf-8",
    )
    (root / "AGENTS.md").write_text(
        "# Calc\n\n## Verification\n\n```\npython -m unittest\n```\n", encoding="utf-8"
    )
    return root


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="athena-real-"))
    broker = SimpleNamespace(url=BROKER_URL, token=broker_token())
    common = {
        "ATHENA_ALLOWED_MODELS": f"{MODEL},nemotron-3.5-lightning:30b",
        "ATHENA_PREFERRED_MODEL": MODEL,
    }
    svc = Service(work, broker, common)  # type: ignore[arg-type]
    started = time.monotonic()
    try:
        ws = base_e2e.project(work, "proyecto")
        (ws / "README.md").write_text(
            "# Inventario\n\nPrograma que cuenta botellas de agua en un almacén.\n",
            encoding="utf-8",
        )
        st, models = svc.call("GET", "/v1/models")
        record("R00", "catálogo del despliegue", "200 con los permitidos", (st, models), st == 200)

        # R01 consulta real
        st, body = svc.call(
            "POST",
            "/v1/runs",
            {
                "objective": "¿Qué cuenta el programa de este proyecto? Responde en una frase.",
                "workspace": str(ws),
                "profile": "questions",
            },
        )
        run = body.get("run_id") if isinstance(body, dict) else None
        final = svc.wait_status(run, FINAL, 1200) if run else None
        record(
            "R01",
            "consulta con el modelo real",
            "completed; el modelo que contestó es el pedido",
            (st, final and final.get("status"), run and history_models(svc, run)),
            bool(final) and final.get("status") == "completed",
        )
        RESULTS[-1]["detalle"] = final

        # R02 documento real + deshacer
        st, body = svc.call(
            "POST",
            "/v1/runs",
            {
                "objective": "Crea el archivo resumen.txt con una sola línea que diga qué "
                "hace este proyecto (lee README.md).",
                "workspace": str(ws),
                "profile": "documents",
                "writes": "allow",
                "exec": "off",
                "deliverables": ["resumen.txt"],
                "execution_mode": "direct",
            },
        )
        run = body.get("run_id") if isinstance(body, dict) else None
        final = svc.wait_status(run, FINAL, 1500) if run else None
        text = (ws / "resumen.txt").read_text("utf-8") if (ws / "resumen.txt").exists() else ""
        record(
            "R02",
            "documento con el modelo real (bucle directo)",
            "completed y resumen.txt no vacío",
            (st, final and final.get("status"), text[:200]),
            bool(final) and final.get("status") == "completed" and bool(text.strip()),
        )
        RESULTS[-1]["detalle"] = final
        if run:
            st, rb = svc.call("POST", f"/v1/runs/{run}/rollback", {})
            record(
                "R03",
                "deshacer el documento real",
                "200 y resumen.txt eliminado",
                (st, rb, (ws / "resumen.txt").exists()),
                st == 200 and not (ws / "resumen.txt").exists(),
            )

        # R04 modelo fuera del catálogo del despliegue
        st, body = svc.call(
            "POST",
            "/v1/runs",
            {"objective": "x", "workspace": str(ws), "profile": "questions", "model": "gemma4:12b"},
        )
        record("R04", "modelo no permitido", "4xx sin crear run", (st, body), 400 <= st < 500)

        # R05 cancelar mientras espera al modelo
        st, body = svc.call(
            "POST",
            "/v1/runs",
            {
                "objective": "Describe con detalle cada fichero del proyecto.",
                "workspace": str(ws),
                "profile": "questions",
            },
        )
        run = body.get("run_id") if isinstance(body, dict) else None
        time.sleep(8)
        st_c, _ = svc.call("POST", f"/v1/runs/{run}/cancel", {})
        t0 = time.monotonic()
        final = svc.wait_status(run, FINAL, 120)
        record(
            "R05",
            "cancelar con una llamada real en curso",
            "cancelled en segundos",
            (st_c, final and final.get("status"), round(time.monotonic() - t0, 1)),
            bool(final) and final.get("status") == "cancelled" and time.monotonic() - t0 < 60,
        )

        # R06 cambio de código con verificación real (unittest)
        calc = unittest_project(work)
        st, body = svc.call(
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
        source = (calc / "calc.py").read_text("utf-8")
        record(
            "R06",
            "corregir un test con verificación real (python -m unittest)",
            "completed, calc.py corregido y test intacto",
            (st, final and final.get("status"), source, (final or {}).get("verification")),
            bool(final) and final.get("status") == "completed" and "+ 1" not in source,
        )
        RESULTS[-1]["detalle"] = final
    finally:
        svc.stop()

    # R07 planificación encendida (modo de serie del servicio)
    svc = Service(work, broker, {**common, "ATHENA_PLANNING": "1"})  # type: ignore[arg-type]
    try:
        ws = work / "proyecto"
        st, body = svc.call(
            "POST",
            "/v1/runs",
            {
                "objective": "Crea notas.txt con dos líneas: qué hace el proyecto y en qué "
                "fichero se describe.",
                "workspace": str(ws),
                "profile": "documents",
                "writes": "allow",
                "exec": "off",
                "deliverables": ["notas.txt"],
            },
        )
        run = body.get("run_id") if isinstance(body, dict) else None
        final = svc.wait_status(run, FINAL, 2400) if run else None
        _, history = svc.call("GET", f"/v1/runs/{run}/history") if run else (0, {})
        names = (
            sorted({e.get("name") for e in history.get("events", []) if isinstance(e, dict)})
            if isinstance(history, dict)
            else []
        )
        record(
            "R07",
            "documento con ATHENA_PLANNING encendido (modo por defecto)",
            "completed y notas.txt; se registra la forma elegida (plan.decided)",
            (
                st,
                final and final.get("status"),
                (ws / "notas.txt").exists(),
                [n for n in names if str(n).startswith(("plan", "graph", "task"))],
            ),
            bool(final) and final.get("status") == "completed" and (ws / "notas.txt").exists(),
        )
        RESULTS[-1]["detalle"] = final

        # R08 plan forzado: el camino del grafo de tareas con el modelo real
        st, body = svc.call(
            "POST",
            "/v1/runs",
            {
                "objective": "Crea indice.txt con una línea por cada fichero del proyecto y "
                "una frase sobre él.",
                "workspace": str(ws),
                "profile": "documents",
                "writes": "allow",
                "exec": "off",
                "deliverables": ["indice.txt"],
                "execution_mode": "hierarchical",
            },
        )
        run = body.get("run_id") if isinstance(body, dict) else None
        final = svc.wait_status(run, FINAL, 3000) if run else None
        _, history = svc.call("GET", f"/v1/runs/{run}/history") if run else (0, {})
        names = (
            sorted({e.get("name") for e in history.get("events", []) if isinstance(e, dict)})
            if isinstance(history, dict)
            else []
        )
        record(
            "R08",
            "documento con plan forzado (execution_mode=hierarchical)",
            "termina con agent.* y indice.txt; eventos graph.*/task.*",
            (
                st,
                body if st != 201 else None,
                final and final.get("status"),
                (ws / "indice.txt").exists(),
                [n for n in names if str(n).startswith(("plan", "graph", "task", "agent"))],
            ),
            bool(final) and final.get("status") == "completed" and (ws / "indice.txt").exists(),
        )
        RESULTS[-1]["detalle"] = final
    finally:
        svc.stop()

    (out / "e2e_broker_real.json").write_text(
        json.dumps(RESULTS, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    fails = sum(1 for r in RESULTS if r["estado"] == "FALLA")
    print(f"\n{len(RESULTS)} casos, {fails} en FALLA, {time.monotonic() - started:.0f} s. {work}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
