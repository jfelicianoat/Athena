"""La ventana de Athena contra el AI_Broker REAL (auditoría 19-nov-2026).

Reutiliza `arnes_ventana.py`. Solo el Broker es real: estado y LOCALAPPDATA temporales y
AI_OS_CONNECT_DIR vacío (sin director: nada se escribe en Core). Excepción leída, no
escrita: en el paso H-01 se apunta a la carpeta de conexión real de Core solo para
«Probar conexión», que no llama al director.

Uso: python ventana_broker_real.py <carpeta_salida>
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import arnes_ventana as a  # noqa: E402
from broker_real import BROKER_URL, broker_token  # noqa: E402

MODEL = "qwen3.8:27b"


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    real_connect = Path(os.environ["LOCALAPPDATA"]) / "AIOSCore" / "conexion"
    token = broker_token()  # antes de aislar: lee la conexión real de Core
    base = Path(tempfile.mkdtemp(prefix="athena-ui-real-"))
    a.isolate(base)
    a.patch_messagebox()
    project = base / "proyecto"
    project.mkdir()
    (project / "README.md").write_text(
        "# Inventario\n\nPrograma que cuenta botellas de agua en un almacén.\n",
        encoding="utf-8",
    )
    root, app = a.make_app(base)
    r = a.Runner(root, app, out)
    results: dict[str, Any] = {}
    done = lambda: app.connection_result.get() not in ("Sin probar", "Probando…")  # noqa: E731

    def connect(with_token: bool, connect_dir: Path | None = None) -> None:
        os.environ["AI_OS_CONNECT_DIR"] = str(connect_dir or base / "conexion_vacia")
        app.base_url.set(BROKER_URL)
        app._switch_credential()
        app.token.set(token if with_token else "")
        app.model.set(MODEL)
        app.connection_result.set("Sin probar")
        app._test_connection()

    r.step(
        "H-01 real: token vacío, Core real lo daría", lambda: connect(False, real_connect), done, 60
    )
    r.step(
        "captura H-01",
        lambda: results.__setitem__(
            "V01",
            {"captura": r.shot("real_h01_sin_token"), "conexion": app.connection_result.get()},
        ),
    )
    r.step("probar conexión real con token", lambda: connect(True), done, 90)
    r.step(
        "captura conexión",
        lambda: results.__setitem__(
            "V02",
            {
                "captura": r.shot("real_conexion_ok"),
                "conexion": app.connection_result.get(),
                "modelos": list(app.model_box.cget("values")),
            },
        ),
    )

    def bad_model() -> None:
        app.workspace.set(str(project))
        app.task_kind.set("question")
        app.model.set("tev1:4b")
        a.set_objective(app, "¿Qué hace este proyecto?")
        app._start()

    r.step(
        "modelo real sin completion (tev1:4b)",
        bad_model,
        lambda: not app._running() and any(d["titulo"] == "Athena" for d in a.DIALOGS),
        90,
    )
    r.step(
        "registro I-06",
        lambda: results.__setitem__(
            "V03", {"dialogo": [d for d in a.DIALOGS if d["titulo"] == "Athena"][-1:]}
        ),
    )

    def ask() -> None:
        app.model.set(MODEL)
        a.set_objective(app, "¿Qué cuenta el programa de este proyecto? Responde en una frase.")
        app._start()

    r.step(
        "consulta real", ask, lambda: not app._running() and app.p_phase.get() != "Arrancando", 1500
    )
    r.step(
        "captura consulta",
        lambda: results.__setitem__(
            "V04",
            {
                "captura": r.shot("real_consulta"),
                "estado": app.status.get(),
                "texto": a.answer_text(app)[:3000],
            },
        ),
    )

    def documents() -> None:
        app.task_kind.set("documents")
        app.writes.set("Preguntar")
        app.deliverables.set("resumen.txt")
        a.set_objective(
            app,
            "Crea resumen.txt con una sola línea que diga qué hace este proyecto (lee README.md).",
        )
        app._start()

    def asking_or_done() -> bool:
        return a.find_button(root, "Permitir una vez") is not None or not app._running()

    r.step("documento real con permiso", documents, asking_or_done, 1500)

    def allow() -> None:
        button = a.find_button(root, "Permitir una vez")
        results["V05"] = {"captura": r.shot("real_permiso"), "permiso": app.p_permission.get()}
        if button is not None:
            button.invoke()

    granted: list[str] = []

    def keep_allowing() -> bool:
        # Un modelo real puede pedir más de un permiso: se concede cada uno hasta terminar.
        button = a.find_button(root, "Permitir una vez")
        if button is not None:
            granted.append(app.p_permission.get())
            button.invoke()
        return not app._running()

    r.step("permitir hasta terminar", allow, keep_allowing, 1500)
    r.step("permisos concedidos", lambda: results.__setitem__("V05b", {"concedidos": granted}))
    r.step(
        "captura documento",
        lambda: results.__setitem__(
            "V06",
            {
                "captura": r.shot("real_documento"),
                "estado": app.status.get(),
                "fichero": (project / "resumen.txt").read_text("utf-8")
                if (project / "resumen.txt").exists()
                else None,
                "texto": a.answer_text(app)[:3000],
            },
        ),
    )
    r.step(
        "deshacer",
        lambda: app._undo_last(),
        lambda: any(d["titulo"] == "Deshacer cambios" and d["tipo"] == "info" for d in a.DIALOGS),
        60,
    )
    r.step(
        "tras deshacer",
        lambda: results.__setitem__("V07", {"existe": (project / "resumen.txt").exists()}),
    )
    r.run()
    results["dialogos"] = a.DIALOGS
    results["pasos"] = r.log
    (out / "ventana_broker_real.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print("base:", base)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
