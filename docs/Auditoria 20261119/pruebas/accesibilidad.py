"""Accesibilidad medible de la ventana (20-nov-2026).

Tk 8.6 no expone UI Automation, así que un lector de pantalla no se puede medir aquí. Sí se
mide sobre la ventana real (en proceso, con el Broker falso):
  A1  Ctrl+Intro en el encargo empieza el trabajo
  A2  Escape en el diálogo de permiso deniega (y no escribe)
  A3  con Tab se llega a los controles principales, en un orden razonable
  A4  contraste WCAG de los estilos de texto sobre su fondo (≥ 4,5:1 para texto normal)

Uso: python accesibilidad.py <carpeta_salida>
"""

from __future__ import annotations

import json
import sys
import tempfile
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import arnes_ventana as a  # noqa: E402
from dobles import FakeBroker  # noqa: E402


def luminance(rgb: tuple[int, int, int]) -> float:
    def channel(value: int) -> float:
        c = value / 65535
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(root: tk.Misc, fg: str, bg: str) -> float:
    l1, l2 = sorted((luminance(root.winfo_rgb(fg)), luminance(root.winfo_rgb(bg))), reverse=True)
    return round((l1 + 0.05) / (l2 + 0.05), 2)


def label_of(widget: tk.Misc) -> str:
    for option in ("text", "textvariable"):
        try:
            value = str(widget.cget(option))
        except tk.TclError:
            continue
        if value:
            return value[:30]
    return ""


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="athena-a11y-"))
    broker = FakeBroker()
    a.isolate(base)
    a.patch_messagebox()
    project = base / "proyecto"
    project.mkdir()
    (project / "README.md").write_text("# demo\n", encoding="utf-8")
    root, app = a.make_app(base)
    r = a.Runner(root, app, out)
    results: dict[str, Any] = {}

    def setup() -> None:
        app.workspace.set(str(project))
        app.base_url.set(broker.url)
        app._switch_credential()
        app.token.set(broker.token)

    r.step("preparar", setup)

    def tab_order() -> None:
        # Con el encargo escrito: un botón deshabilitado no entra en la tabulación.
        a.set_objective(app, "¿Qué hay aquí?")
        start = app.root.focus_get() or app.root
        widget = app.root.nametowidget(str(start)).tk_focusNext()
        seen: list[str] = []
        for _ in range(60):
            if widget is None:
                break
            seen.append(f"{widget.winfo_class()}:{label_of(widget)}")
            widget = widget.tk_focusNext()
        joined = " | ".join(seen)
        results["A3_tabulacion"] = {
            "recorrido": seen,
            "llega_a": {
                name: name in joined
                for name in ("Elegir…", "Probar conexión", "Iniciar", "Limpiar pantalla")
            },
        }
        results["A3_tabulacion"]["estado"] = (
            "PASA" if all(results["A3_tabulacion"]["llega_a"].values()) else "FALLA"
        )

    r.step("orden de tabulación", tab_order)

    def ctrl_enter() -> None:
        app.task_kind.set("question")
        a.set_objective(app, "¿Qué hay aquí?")
        app.objective.focus_force()
        app.objective.event_generate("<Control-Return>")

    r.step(
        "Ctrl+Intro",
        ctrl_enter,
        lambda: not app._running() and app.p_phase.get() != "Sin empezar",
        30,
    )
    r.step(
        "registro A1",
        lambda: results.__setitem__(
            "A1_ctrl_intro",
            {
                "fase": app.p_phase.get(),
                "estado": "PASA"
                if app.p_phase.get() not in ("Sin empezar", "Arrancando")
                else "FALLA",
            },
        ),
    )

    def ask_permission() -> None:
        app.task_kind.set("documents")
        app.writes.set("Preguntar")
        app.deliverables.set("nota.txt")
        a.set_objective(app, "Escribe [[escribe:nota.txt:hola]]")
        app._start()

    r.step(
        "documento con permiso",
        ask_permission,
        lambda: a.find_button(root, "Permitir una vez") is not None,
        30,
    )

    def escape() -> None:
        top = a.find_button(root, "Permitir una vez").winfo_toplevel()
        top.focus_force()
        top.event_generate("<Escape>")

    r.step("Escape en el diálogo", escape, lambda: not app._running(), 30)
    r.step(
        "registro A2",
        lambda: results.__setitem__(
            "A2_escape_deniega",
            {
                "fichero_escrito": (project / "nota.txt").exists(),
                "estado": "PASA" if not (project / "nota.txt").exists() else "FALLA",
            },
        ),
    )

    def colours() -> None:
        style = ttk.Style(root)
        background = style.lookup("TFrame", "background") or root.cget("background")
        rows = {}
        for name in ("TLabel", "Hint.TLabel", "Ok.TLabel", "Bad.TLabel", "Header.TLabel"):
            fg = style.lookup(name, "foreground") or "black"
            bg = style.lookup(name, "background") or background
            rows[name] = {"fg": fg, "bg": bg, "contraste": contrast(root, fg, bg)}
        results["A4_contraste"] = {
            "estilos": rows,
            "por_debajo_de_4_5": [n for n, v in rows.items() if v["contraste"] < 4.5],
        }
        results["A4_contraste"]["estado"] = (
            "PASA" if not results["A4_contraste"]["por_debajo_de_4_5"] else "FALLA"
        )

    r.step("contraste", colours)
    r.run()
    broker.close()
    results["dialogos"] = a.DIALOGS
    (out / "accesibilidad.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(
        json.dumps(
            {k: v for k, v in results.items() if k.startswith("A")},
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
