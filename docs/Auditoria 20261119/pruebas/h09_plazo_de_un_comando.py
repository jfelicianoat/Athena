"""H-09: el plazo agotado de UN comando del modelo termina todo el run como fallido.

Visto con el Broker real (R06, qwen3.8:27b): el modelo corrigió calc.py, sus tests
pasaban, y después pidió `bash` con `python` a secas y 5-30 s de plazo. En Windows ese
proceso no termina (NUL cuenta como terminal y Python abre su consola interactiva); el
`process_timeout` se clasifica como fin de la SESIÓN y el run acaba `failed` sin
verificar el trabajo hecho.

Aquí, de forma determinista y sin modelo real: el doble pide `bash python` con 5 s. Se
esperaría que el error llegue al modelo como cualquier fallo de herramienta y el run siga.

Uso: python h09_plazo_de_un_comando.py <carpeta_salida>
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from dobles import FakeBroker  # noqa: E402
from e2e_servicio import FINAL, Service, project  # noqa: E402


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="athena-h09-"))
    broker = FakeBroker()
    svc = Service(base, broker)
    try:
        ws = project(base, "proyecto")
        # Como en R06: un proyecto con su comprobación declarada y en verde.
        (ws / "calc.py").write_text("def total(items):\n    return sum(items)\n", "utf-8")
        (ws / "test_calc.py").write_text(
            "import unittest\n\nfrom calc import total\n\n\nclass T(unittest.TestCase):\n"
            "    def test_total(self):\n        self.assertEqual(total([1, 2, 3]), 6)\n",
            "utf-8",
        )
        (ws / "AGENTS.md").write_text("# Calc\n\n## Verification\n\n```\npython -m unittest\n```\n")
        status, body = svc.call(
            "POST",
            "/v1/runs",
            {
                "objective": "Comprueba el entorno [[bash:python:5]]",
                "workspace": str(ws),
                "profile": "software_engineering",
                "writes": "allow",
                "exec": "allow",
                "execution_mode": "direct",
            },
        )
        run = body["run_id"]
        final = svc.wait_status(run, FINAL, 120)
        wm = (final or {}).get("working_memory", {})
        result = {
            "http": status,
            "estado": final and final.get("status"),
            "errores": wm.get("errors"),
            "tareas_al_broker": len(broker.submitted),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        (out / "h09.json").write_text(
            json.dumps({"resultado": result, "detalle": final}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # Lo esperado: el modelo vuelve a ser consultado tras el plazo agotado (≥ 2 tareas)
        # y el run no acaba `failed` por process_timeout.
        codes = [e.get("code") for e in wm.get("errors") or []]
        abierto = result["estado"] == "failed" and "process_timeout" in codes
        print("H-09 ABIERTO" if abierto else "H-09 corregido")
        sys.exit(1 if abierto else 0)
    finally:
        svc.stop()
        broker.close()


if __name__ == "__main__":
    main(Path(sys.argv[1]))
