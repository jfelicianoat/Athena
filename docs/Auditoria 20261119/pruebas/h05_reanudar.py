"""H-05: reanudar desde Historial presenta el resultado con el tipo de tarea del formulario.

Orquesta las dos fases de `arnes_ventana.py` con un mismo Broker falso:
  1. `caida`: trabajo «Crear documentos» que muere a mitad (os._exit).
  2. `reanudar`: ventana nueva con «Responder una pregunta» marcado; Historial → Reanudar.

Uso: python h05_reanudar.py <carpeta_salida>
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from dobles import FakeBroker  # noqa: E402

PYTHON = str(HERE.parents[2] / ".venv" / "Scripts" / "python.exe")


def main(out: Path) -> None:
    base = Path(tempfile.mkdtemp(prefix="athena-h05-"))
    project = base / "proyecto"
    project.mkdir()
    (project / "README.md").write_text(
        "# Inventario\n\nCuenta botellas de agua en un almacén.\n", encoding="utf-8"
    )
    # Con --real usa el Broker de verdad (token pedido a Core, pasado por entorno).
    if "--real" in sys.argv:
        from broker_real import BROKER_URL, broker_token

        os.environ["AUDIT_BROKER_TOKEN"] = broker_token()
        os.environ.setdefault("AUDIT_MODEL", "qwen3.8:27b")
        os.environ.setdefault(
            "AUDIT_OBJETIVO",
            "Crea informe.txt con una sola línea que diga qué hace el proyecto (lee README.md).",
        )
        os.environ.setdefault("AUDIT_TIMEOUT", "1500")
        broker = SimpleNamespace(url=BROKER_URL, close=lambda: None)
    else:
        broker = FakeBroker()
    try:
        script = str(HERE / "arnes_ventana.py")
        first = subprocess.run(
            [PYTHON, "-X", "utf8", script, "caida", str(base), str(project), broker.url],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        print("fase 1:", first.returncode, first.stdout[-500:], first.stderr[-500:])
        print("informe.txt tras la caída:", (project / "informe.txt").exists())
        second = subprocess.run(
            [
                PYTHON,
                "-X",
                "utf8",
                script,
                "reanudar",
                str(out),
                str(base),
                str(project),
                broker.url,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=float(os.environ.get("AUDIT_TIMEOUT", "240")) + 120,
        )
        print("fase 2:", second.returncode, second.stdout[-3000:], second.stderr[-1500:])
    finally:
        broker.close()
    # Veredicto automático: el titular no puede negar los cambios que la propia vista lista.
    import json

    result = json.loads((out / "arnes_reanudar.json").read_text(encoding="utf-8"))
    after = result.get("despues") or {}
    written = bool(after.get("fichero"))
    denies = "no se ha cambiado ningún archivo" in str(after.get("estado", ""))
    print(f"informe.txt escrito: {written}; titular niega cambios: {denies}")
    if not written:
        # Sin fichero no se puede juzgar la presentación (p. ej. el modelo falló).
        print("H-05 NO CONCLUYENTE: el trabajo reanudado no escribió informe.txt")
        sys.exit(2)
    if denies:
        print("H-05 ABIERTO: el resultado reanudado contradice lo que hizo el trabajo")
        sys.exit(1)
    print("H-05 corregido")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
