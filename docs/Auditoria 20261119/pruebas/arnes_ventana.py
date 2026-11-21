"""Arnés de la ventana de Athena Desktop (auditoría 19-nov-2026).

Abre la `AthenaDesktopApp` real en este proceso, contra los dobles de `dobles.py`, con
LOCALAPPDATA y AI_OS_CONNECT_DIR aislados. Los botones se pulsan con `invoke()` (la misma
orden que ejecuta un clic) y los diálogos nativos de `messagebox` se sustituyen por un
registro con respuestas guionizadas: un clic real del sistema no se usa (en sesiones
anteriores las ventanas sin primer plano se tragaban los clics).

Uso:
  python arnes_ventana.py recorrido <carpeta_salida>
  python arnes_ventana.py caida <carpeta_estado> <carpeta_proyecto> <url_broker>   (fase 1 de H-05)
  python arnes_ventana.py reanudar <carpeta_salida> <carpeta_estado> <carpeta_proyecto> <url_broker>
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import tempfile
import time
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

HERE = Path(__file__).resolve().parent
ATHENA = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ATHENA / "src"))


def isolate(base: Path, connect: Path | None = None) -> None:
    (base / "local").mkdir(parents=True, exist_ok=True)
    os.environ["LOCALAPPDATA"] = str(base / "local")
    empty = base / "conexion_vacia"
    empty.mkdir(exist_ok=True)
    os.environ["AI_OS_CONNECT_DIR"] = str(connect or empty)
    for name in list(os.environ):
        if name.startswith("ATHENA_") or (
            name.startswith("AI_OS_") and name != "AI_OS_CONNECT_DIR"
        ):
            del os.environ[name]


DIALOGS: list[dict[str, Any]] = []
ANSWERS: dict[str, Any] = {}


def patch_messagebox() -> None:
    def make(kind: str, default: Any) -> Callable[..., Any]:
        def fake(title: str = "", message: str = "", **_: Any) -> Any:
            answer = ANSWERS.get(title, default)
            DIALOGS.append({"tipo": kind, "titulo": title, "mensaje": message, "respuesta": answer})
            print(f"  [diálogo {kind}] {title}: {message[:160]!r} -> {answer}")
            return answer

        return fake

    messagebox.showerror = make("error", "ok")  # type: ignore[assignment]
    messagebox.showinfo = make("info", "ok")  # type: ignore[assignment]
    messagebox.showwarning = make("aviso", "ok")  # type: ignore[assignment]
    messagebox.askyesno = make("sí/no", True)  # type: ignore[assignment]
    messagebox.askokcancel = make("aceptar/cancelar", True)  # type: ignore[assignment]
    messagebox.askyesnocancel = make("sí/no/cancelar", True)  # type: ignore[assignment]


class Runner:
    def __init__(self, root: tk.Tk, app: Any, out: Path) -> None:
        self.root, self.app, self.out = root, app, out
        self.steps: list[tuple[str, Callable[[], Any], Callable[[], bool], float]] = []
        self.log: list[dict[str, Any]] = []
        self.shots = 0

    def step(
        self,
        name: str,
        action: Callable[[], Any],
        until: Callable[[], bool] = lambda: True,
        timeout: float = 60,
    ) -> None:
        self.steps.append((name, action, until, timeout))

    def shot(self, name: str) -> str:
        from PIL import ImageGrab

        self.root.update_idletasks()
        self.root.update()
        x, y = self.root.winfo_rootx(), self.root.winfo_rooty()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        self.shots += 1
        path = self.out / f"{self.shots:02d}_{name}.png"
        ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True).save(path)
        return path.name

    def run(self) -> None:
        self.root.after(800, self._next, 0)
        self.root.mainloop()

    def _next(self, index: int) -> None:
        if index >= len(self.steps):
            self.root.after(200, self.root.destroy)
            return
        name, action, until, timeout = self.steps[index]
        print(f"- {name}")
        started = time.monotonic()
        try:
            action()
        except Exception as exc:  # el arnés registra, no se cae
            self.log.append({"paso": name, "error": repr(exc)})
            print(f"  ERROR en acción: {exc!r}")
        self._wait(index, name, until, started, timeout)

    def _wait(
        self, index: int, name: str, until: Callable[[], bool], started: float, timeout: float
    ) -> None:
        try:
            done = until()
        except Exception:
            done = False
        if done or time.monotonic() - started > timeout:
            self.log.append(
                {
                    "paso": name,
                    "cumplido": bool(done),
                    "segundos": round(time.monotonic() - started, 1),
                    "estado": self.app.status.get(),
                    "fase": self.app.p_phase.get(),
                }
            )
            if not done:
                print(f"  TIEMPO AGOTADO esperando: {name}")
            self.root.after(150, self._next, index + 1)
            return
        self.root.after(200, self._wait, index, name, until, started, timeout)


def text_of(widget: tk.Text) -> str:
    return widget.get("1.0", tk.END).strip()


def find_text_widgets(root: tk.Misc) -> list[tk.Text]:
    found: list[tk.Text] = []
    for child in root.winfo_children():
        if isinstance(child, tk.Text):
            found.append(child)
        found.extend(find_text_widgets(child))
    return found


def find_button(root: tk.Misc, label: str) -> ttk.Button | None:
    for child in root.winfo_children():
        if isinstance(child, ttk.Button) and str(child.cget("text")) == label:
            return child
        hit = find_button(child, label)
        if hit is not None:
            return hit
    return None


def make_app(base: Path) -> tuple[tk.Tk, Any]:
    if sys.platform == "win32":
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    from athena_desktop.app import AthenaDesktopApp

    root = tk.Tk()
    root.geometry("1280x900+20+20")
    root.attributes("-topmost", True)
    app = AthenaDesktopApp(root)
    return root, app


def set_objective(app: Any, text: str) -> None:
    app.objective.configure(state=tk.NORMAL)
    app.objective.delete("1.0", tk.END)
    app.objective.insert("1.0", text)
    app._refresh()


def answer_text(app: Any) -> str:
    texts = find_text_widgets(app.root)
    return "\n---\n".join(text_of(t) for t in texts)


def recorrido(out: Path) -> None:
    from dobles import FakeBroker, FakeCore

    out.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="athena-ui-"))
    broker = FakeBroker()
    core = FakeCore("allow", broker_token=broker.token)
    isolate(base)
    patch_messagebox()
    project = base / "proyecto"
    project.mkdir()
    (project / "README.md").write_text("# demo\n", encoding="utf-8")
    root, app = make_app(base)
    r = Runner(root, app, out)
    results: dict[str, Any] = {}

    def capture(key: str, name: str) -> Callable[[], None]:
        def do() -> None:
            results[key] = {
                "captura": r.shot(name),
                "estado": app.status.get(),
                "conexion": app.connection_result.get(),
                "texto": answer_text(app)[:3000],
            }

        return do

    r.step("primera ejecución", capture("U01", "primera_ejecucion"))
    r.step(
        "historial vacío",
        lambda: (app.tabs.select(1), None),
        lambda: "trabajo" in app.history_status.get(),
    )
    r.step("captura historial vacío", capture("U02", "historial_vacio"))
    r.step("volver a Trabajo", lambda: app.tabs.select(0))

    # Probar conexión: URL por defecto (sin broker)
    r.step(
        "probar conexión con la URL por defecto",
        lambda: app._test_connection(),
        lambda: app.connection_result.get() not in ("Sin probar", "Probando…"),
        30,
    )
    r.step("captura conexión por defecto", capture("U03", "conexion_url_defecto"))

    # Probar conexión con Core que da el token y campo vacío
    def with_core() -> None:
        folder = base / "conexion_core"
        folder.mkdir(exist_ok=True)
        (folder / "athena.json").write_text(
            json.dumps({"core_url": core.url, "token": "c"}), encoding="utf-8"
        )
        os.environ["AI_OS_CONNECT_DIR"] = str(folder)
        app.base_url.set(broker.url)
        app.token.set("")
        app.model.set("modelo-bueno")
        app.connection_result.set("Sin probar")
        app._test_connection()

    r.step(
        "probar conexión: token vacío y Core lo da (H-01)",
        with_core,
        lambda: app.connection_result.get() not in ("Sin probar", "Probando…"),
        30,
    )
    r.step("captura H-01", capture("U04", "conexion_token_de_core"))

    # Probar conexión con token correcto: rellena el desplegable
    def with_token() -> None:
        os.environ["AI_OS_CONNECT_DIR"] = str(base / "conexion_vacia")
        app.token.set(broker.token)
        app.connection_result.set("Sin probar")
        app._test_connection()

    r.step(
        "probar conexión con token",
        with_token,
        lambda: app.connection_result.get() not in ("Sin probar", "Probando…"),
        30,
    )
    r.step(
        "captura conexión ok",
        lambda: results.__setitem__(
            "U05",
            {
                "captura": r.shot("conexion_ok"),
                "conexion": app.connection_result.get(),
                "modelos": list(app.model_box.cget("values")),
            },
        ),
    )

    # Modelo sin `completion` (I-06): Iniciar debe negarse antes de crear el run
    def bad_model() -> None:
        app.workspace.set(str(project))
        app.task_kind.set("question")
        app.model.set("modelo-solo-decision")
        set_objective(app, "¿Cuántos ficheros hay?")
        app._start()

    r.step(
        "modelo sin completion",
        bad_model,
        lambda: any(d["titulo"] == "Athena" for d in DIALOGS) or not app._running(),
        30,
    )
    r.step("esperar fin", lambda: None, lambda: not app._running(), 30)
    r.step("restaurar modelo", lambda: app.model.set("modelo-bueno"))

    # Pregunta
    def ask() -> None:
        app.workspace.set(str(project))
        app.task_kind.set("question")
        set_objective(app, "¿Qué contiene este proyecto?")
        app._start()

    r.step("consulta", ask, lambda: not app._running() and app.p_phase.get() != "Arrancando", 60)
    r.step("captura consulta", capture("U06", "consulta_resultado"))

    # Consulta que parece pedir un cambio: diálogo «¿Consulta o cambio?» -> Cancelar
    def ambiguous() -> None:
        ANSWERS["¿Consulta o cambio?"] = None
        set_objective(app, "Corrige el fichero main.py")
        app._start()

    r.step("consulta con verbo de cambio (Cancelar)", ambiguous)

    # Documento con escritura en «Preguntar»: diálogo de permiso real (Toplevel)
    def documents() -> None:
        app.task_kind.set("documents")
        app.writes.set("Preguntar")
        app.deliverables.set("nota.txt")
        set_objective(app, "Escribe la nota [[escribe:nota.txt:hola desde la auditoria]]")
        app._start()

    def approval_open() -> bool:
        return find_button(root, "Permitir una vez") is not None

    r.step("documento con permiso", documents, approval_open, 60)
    r.step(
        "captura diálogo de permiso",
        lambda: results.__setitem__(
            "U07",
            {
                "captura": r.shot("dialogo_permiso_ventana_principal"),
                "fase": app.p_phase.get(),
                "permiso": app.p_permission.get(),
            },
        ),
    )

    def allow() -> None:
        button = find_button(root, "Permitir una vez")
        top = button.winfo_toplevel()
        from PIL import ImageGrab

        top.update()
        bbox = (
            top.winfo_rootx(),
            top.winfo_rooty(),
            top.winfo_rootx() + top.winfo_width(),
            top.winfo_rooty() + top.winfo_height(),
        )
        r.shots += 1
        name = f"{r.shots:02d}_dialogo_permiso.png"
        ImageGrab.grab(bbox=bbox, all_screens=True).save(out / name)
        results["U07"]["captura_dialogo"] = name
        button.invoke()

    r.step("permitir una vez", allow, lambda: not app._running(), 60)
    r.step(
        "captura documento",
        lambda: results.__setitem__(
            "U08",
            {
                "captura": r.shot("documento_resultado"),
                "estado": app.status.get(),
                "fichero": (project / "nota.txt").exists(),
                "deshacer_activo": str(app.undo_button.cget("state")),
                "texto": answer_text(app)[:3000],
            },
        ),
    )

    r.step(
        "deshacer",
        lambda: app._undo_last(),
        lambda: any(d["titulo"] == "Deshacer cambios" and d["tipo"] == "info" for d in DIALOGS),
        30,
    )
    r.step(
        "comprobar deshacer",
        lambda: results.__setitem__(
            "U09", {"fichero_tras_deshacer": (project / "nota.txt").exists()}
        ),
    )

    # Modificar el proyecto sin comprobaciones: aviso previo -> Cancelar
    def change() -> None:
        ANSWERS["El trabajo no se podrá comprobar"] = False
        app.task_kind.set("change")
        app.writes.set("Preguntar")
        set_objective(app, "Añade una línea al README")
        app._start()

    r.step("cambio sin comprobaciones (aviso, Cancelar)", change)

    # Historial
    r.step(
        "historial",
        lambda: (app.tabs.select(1), app._refresh_history()),
        lambda: "trabajo" in app.history_status.get(),
        20,
    )
    r.step(
        "captura historial",
        lambda: results.__setitem__(
            "U10",
            {
                "captura": r.shot("historial"),
                "filas": len(app.tree.get_children()),
                "estado": app.history_status.get(),
            },
        ),
    )

    # Cerrar con un trabajo en marcha
    def long_run() -> None:
        app.tabs.select(0)
        app.task_kind.set("question")
        set_objective(app, "Piensa mucho [[lento:30]]")
        app._start()

    r.step("trabajo largo", long_run, lambda: app._running(), 10)
    r.step(
        "captura en marcha",
        lambda: results.__setitem__(
            "U11",
            {
                "captura": r.shot("trabajo_en_marcha"),
                "fase": app.p_phase.get(),
                "cola": app.p_queue.get(),
            },
        ),
        timeout=1,
    )
    r.step("esperar 3 s", lambda: None, lambda: False, 3)
    r.step("cerrar con trabajo en marcha", lambda: app._close())

    r.run()
    results["dialogos"] = DIALOGS
    results["pasos"] = r.log
    (out / "arnes_ventana.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    # El estado que deja la ventana: ¿el trabajo cerrado quedó en el historial?
    import asyncio

    from athena_desktop.runtime import list_runs

    runs = asyncio.run(list_runs())
    (out / "historial_final.json").write_text(
        json.dumps(
            [
                {
                    "estado": s.status,
                    "objetivo": s.objective,
                    "reanudable": s.resumable,
                    "tipo": s.task_kind,
                }
                for s in runs
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    broker.close()
    core.close()
    print(f"base temporal: {base}")


def caida(state: Path, project: Path, broker_url: str) -> None:
    """Fase 1 de H-05: un trabajo de documentos que muere a mitad (os._exit)."""
    isolate(state)
    patch_messagebox()
    root, app = make_app(state)
    app.workspace.set(str(project))
    app.provider.set(app.provider.get())
    app.base_url.set(broker_url)
    app._switch_credential()
    app.token.set(os.environ.get("AUDIT_BROKER_TOKEN", "tok-broker"))
    app.task_kind.set("documents")
    app.writes.set("Permitir")
    app.deliverables.set("informe.txt")
    app.model.set(os.environ.get("AUDIT_MODEL", ""))
    set_objective(
        app,
        os.environ.get(
            "AUDIT_OBJETIVO", "Escribe el informe [[lento:6]] [[escribe:informe.txt:informe]]"
        ),
    )

    def go() -> None:
        app._start()

        def die() -> None:
            print("DIALOGOS", DIALOGS, "fase", app.p_phase.get(), flush=True)
            os._exit(0)

        root.after(4000, die)

    root.after(800, go)
    root.mainloop()


def reanudar(out: Path, state: Path, project: Path, broker_url: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    isolate(state)
    patch_messagebox()
    root, app = make_app(state)
    r = Runner(root, app, out)
    results: dict[str, Any] = {}

    def prepare() -> None:
        app.base_url.set(broker_url)
        app._switch_credential()
        app.token.set(os.environ.get("AUDIT_BROKER_TOKEN", "tok-broker"))
        app.model.set(os.environ.get("AUDIT_MODEL", ""))
        # La persona cambió a «Responder una pregunta» antes de volver al Historial
        app.task_kind.set("question")
        app.tabs.select(1)
        app._refresh_history()

    r.step("historial tras la caída", prepare, lambda: len(app.tree.get_children()) > 0, 20)

    def select() -> None:
        first = app.tree.get_children()[0]
        app.tree.selection_set(first)
        app._history_selected()
        results["antes"] = {
            "captura": r.shot("historial_interrumpido"),
            "reanudar": str(app.resume_button.cget("state")),
            "detalle": text_of(app.history_detail),
        }

    r.step("seleccionar", select)
    r.step(
        "reanudar",
        lambda: app._resume_selected(),
        lambda: not app._running() and app.p_phase.get() not in ("Arrancando",),
        float(os.environ.get("AUDIT_TIMEOUT", "90")),
    )
    r.step(
        "captura reanudado",
        lambda: results.__setitem__(
            "despues",
            {
                "captura": r.shot("reanudado_resultado"),
                "estado": app.status.get(),
                "fichero": (project / "informe.txt").exists(),
                "texto": answer_text(app)[:3000],
            },
        ),
    )
    r.run()
    results["dialogos"] = DIALOGS
    results["pasos"] = r.log
    (out / "arnes_reanudar.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "recorrido":
        recorrido(Path(sys.argv[2]))
    elif mode == "caida":
        caida(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])
    elif mode == "reanudar":
        reanudar(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5])
