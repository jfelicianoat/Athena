"""Athena en modo texto: lo mismo que la ventana, para quien usa un lector de pantalla.

La ventana es Tk 8.6, que no expone accesibilidad: medido con UI Automation (lo que leen
Narrador y NVDA), son 75 paneles sin nombre (A11Y-2, auditoria del 19/20-nov). Esto es la
misma aplicacion en lineas de texto y menus numerados: elegir proyecto, probar la conexion,
lanzar una pregunta, un cambio o documentos, aprobar cada accion leyendo que hara, ver el
resultado, el historial, reanudar y deshacer. Sin logica propia: usa `runtime` y
`presentacion`, las mismas reglas que la ventana.

    athena-texto            (o)   python -m athena_desktop.texto
"""

from __future__ import annotations

import asyncio
import getpass
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import cast

from athena import __version__
from athena.adapters.ai_os_core import broker_token_source
from athena.cancellation import CancellationSource
from athena.events import RuntimeEvent
from athena.permissions import PermissionDecision, PermissionRequest
from athena_desktop.config import CapabilityMode, SettingsStore, environment_token
from athena_desktop.presentacion import (
    describe_event,
    present_approval,
    present_result,
    status_label,
)
from athena_desktop.runtime import (
    RunConfiguration,
    TaskKind,
    check_connection,
    list_runs,
    requires_workspace_change,
    roll_back_run,
    run_athena,
    verification_warning,
)

Read = Callable[[str], str]
Write = Callable[[str], None]

_KINDS = {"1": "question", "2": "change", "3": "documents"}
_KIND_NAMES = {"question": "Pregunta", "change": "Modificar el proyecto", "documents": "Documentos"}
_MODES = {"1": "off", "2": "ask", "3": "allow"}
_MODE_NAMES = {"off": "No", "ask": "Preguntar", "allow": "Permitir"}


class TextApp:
    def __init__(
        self,
        read: Read = input,
        write: Write = print,
        secret: Read = getpass.getpass,
        store: SettingsStore | None = None,
        state_dir: Path | None = None,
    ) -> None:
        self.read, self.write, self.secret = read, write, secret
        self.store = store or SettingsStore()
        self.settings = self.store.load()
        self.state_dir = state_dir
        self.token = environment_token(self.settings.provider)

    # ------------------------------------------------------------------ utilidades

    def ask(self, question: str, default: str = "") -> str:
        hint = f" [{default}]" if default else ""
        answer = self.read(f"{question}{hint}: ").strip()
        return answer or default

    def choose(self, question: str, options: dict[str, str], default: str) -> str:
        self.write(question)
        for key, label in options.items():
            self.write(f"  {key}. {label}")
        while True:
            answer = self.ask("Escribe el número", default)
            if answer in options:
                return answer
            self.write("No es una de las opciones. Prueba otra vez.")

    def yes(self, question: str) -> bool:
        return self.ask(f"{question} (s/n)", "n").lower().startswith(("s", "y"))

    def configuration(self, objective: str, kind: str, writes: str, execution: str,
                      deliverables: tuple[str, ...] = ()) -> RunConfiguration:
        return RunConfiguration(
            workspace=Path(self.settings.workspace),
            objective=objective,
            provider=self.settings.provider,
            base_url=self.settings.base_url,
            model=self.settings.model,
            token=self.token,
            writes=cast(CapabilityMode, writes),
            execution=cast(CapabilityMode, execution),
            max_iterations=self.settings.max_iterations,
            timeout_seconds=self.settings.timeout_seconds,
            task_kind=cast(TaskKind, kind),
            deliverables=deliverables,
        )

    # ------------------------------------------------------------------ menu

    def run(self) -> int:
        self.write(f"Athena v{__version__}, modo texto.")
        self.write("En cada pregunta, Intro acepta lo que va entre corchetes.")
        while True:
            self.write("")
            self.write(f"Proyecto: {self.settings.workspace or '(ninguno)'}")
            self.write(f"Broker: {self.settings.base_url}  Modelo: "
                       f"{self.settings.model or '(lo elige el broker)'}")
            option = self.choose(
                "¿Qué quieres hacer?",
                {"1": "Nueva tarea", "2": "Historial", "3": "Elegir proyecto",
                 "4": "Conexión y modelo", "0": "Salir"},
                "1",
            )
            if option == "0":
                self.write("Hasta luego.")
                return 0
            {"1": self.new_task, "2": self.history, "3": self.pick_project,
             "4": self.connection}[option]()

    def pick_project(self) -> None:
        while True:
            folder = self.ask("Ruta completa de la carpeta del proyecto", self.settings.workspace)
            if folder and Path(folder).is_absolute() and Path(folder).is_dir():
                self.settings = replace(self.settings, workspace=folder)
                self.store.save(self.settings)
                self.write(f"Proyecto: {folder}")
                return
            self.write(
                "Esa carpeta no existe o no es una ruta completa "
                "(por ejemplo D:\\Proyectos\\app)."
            )

    def connection(self) -> None:
        url = self.ask("URL del broker", self.settings.base_url)
        model = self.ask("Modelo (vacío: lo elige el broker)", self.settings.model)
        self.settings = replace(self.settings, base_url=url, model=model)
        self.store.save(self.settings)
        if broker_token_source() is not None and not self.token:
            self.write("El token del broker lo da AI OS Core.")
        elif self.yes("¿Quieres escribir un token para esta sesión?"):
            self.token = self.secret("Token (no se muestra ni se guarda): ").strip()
        self.write("Probando la conexión…")
        report = asyncio.run(check_connection(self.configuration("probar", "question", "off", "off")))
        self.write(("Bien: " if report.ok else "Problema: ") + report.message)

    # ------------------------------------------------------------------ tareas

    def new_task(self) -> None:
        if not self.settings.workspace:
            self.write("Antes elige el proyecto (opción 3).")
            return
        kind = _KINDS[self.choose("Tipo de tarea:", {
            "1": "Responder una pregunta (no cambia nada)",
            "2": "Modificar el proyecto",
            "3": "Crear documentos",
        }, "1")]
        objective = self.ask("¿Qué quieres que haga?")
        if not objective:
            self.write("Sin encargo no hay tarea.")
            return
        if kind == "question" and requires_workspace_change(objective) and self.yes(
            "Tu encargo parece pedir cambios en archivos. ¿Lo hago como «Modificar el proyecto»?"
        ):
            kind = "change"
        writes, execution, deliverables = "off", "off", ()
        if kind != "question":
            writes = _MODES[self.choose("Cambios en archivos:", {
                "2": "Preguntar cada cambio", "3": "Permitir sin preguntar"}, "2")]
            execution = _MODES[self.choose("Ejecutar código del proyecto (tests):", {
                "1": "No", "2": "Preguntar", "3": "Permitir"}, "1")]
        if kind == "documents":
            raw = self.ask("Entregables, separados por comas (por ejemplo informe.md)")
            deliverables = tuple(item.strip() for item in raw.split(",") if item.strip())
        configuration = self.configuration(objective, kind, writes, execution, deliverables)
        try:
            configuration.validate()
        except ValueError as exc:
            self.write(f"No se puede empezar: {exc}")
            return
        warning = verification_warning(configuration)
        if warning and not self.yes(f"{warning} ¿Empezar igualmente?"):
            return
        self.execute(configuration)

    def execute(self, configuration: RunConfiguration, resume_run_id: str | None = None) -> None:
        self.write("Athena está trabajando. Te iré contando cada paso.")
        last_run: list[str] = []

        def on_event(event: RuntimeEvent) -> None:
            line = describe_event(event)
            if line is not None:
                self.write(f"  {line.text}")

        def on_permission(request: PermissionRequest) -> PermissionDecision:
            view = present_approval(request)
            self.write("")
            self.write(f"Athena pide permiso: {view.action}.")
            self.write(f"Riesgo: {view.risk}. Motivo: {view.reason}")
            for effect in view.effects:
                self.write(f"  Puede pasar: {effect}")
            if view.preview:
                self.write(f"{view.preview_title}:")
                for line in view.preview.splitlines()[:40]:
                    self.write(f"  {line}")
            return PermissionDecision.ALLOW if self.yes("¿Permitir esta acción una vez?") \
                else PermissionDecision.DENY

        try:
            result = asyncio.run(run_athena(
                configuration, CancellationSource(), on_event=on_event,
                on_permission=on_permission, state_dir=self.state_dir,
                on_started=lambda handle: last_run.append(handle.run_id),
                resume_run_id=resume_run_id,
            ))
        except Exception as exc:
            self.write(f"No se pudo empezar o terminar el trabajo: {exc}")
            return
        view = present_result(result, task_kind=configuration.task_kind)
        self.write("")
        self.write(view.headline)
        if view.explanation:
            self.write(view.explanation)
        if view.answer:
            self.write("Respuesta de Athena:")
            self.write(view.answer)
        if view.files:
            self.write("Archivos cambiados: " + ", ".join(view.files))
        for line in view.checks:
            self.write(f"Comprobación: {line}")
        for step in view.next_steps:
            self.write(f"Qué puedes hacer ahora: {step}")
        if view.files and last_run and self.yes("¿Deshacer los cambios de este trabajo?"):
            self.undo(last_run[0])

    # ------------------------------------------------------------------ historial

    def history(self) -> None:
        runs = asyncio.run(list_runs(self.state_dir))
        if not runs:
            self.write("Todavía no hay trabajos.")
            return
        shown = runs[:15]
        options = {
            str(index): f"{run.updated_at} · {status_label(run.status)} · "
            f"{_KIND_NAMES.get(run.task_kind, run.task_kind)} · {run.objective[:60]}"
            for index, run in enumerate(shown, start=1)
        }
        options["0"] = "Volver"
        choice = self.choose("Trabajos guardados, del más reciente al más antiguo:", options, "0")
        if choice == "0":
            return
        run = shown[int(choice) - 1]
        self.write(f"Objetivo: {run.objective}")
        self.write(f"Estado: {status_label(run.status)}. Proyecto: {run.project}")
        if run.files_modified:
            self.write("Archivos cambiados: " + ", ".join(run.files_modified))
        if run.resumable and self.yes("Se interrumpió. ¿Reanudarlo?"):
            self.settings = replace(self.settings, workspace=run.project)
            configuration = self.configuration(run.objective, run.task_kind, "ask", "off")
            self.execute(configuration, resume_run_id=run.run_id)
        elif run.undoable and self.yes(f"¿Deshacer sus {run.undoable} cambio(s)?"):
            self.undo(run.run_id)

    def undo(self, run_id: str) -> None:
        result = asyncio.run(roll_back_run(run_id, self.state_dir))
        if result.restored:
            self.write("Restaurados: " + ", ".join(result.restored))
        if result.conflicts:
            self.write("No se tocaron porque cambiaron después: " + ", ".join(result.conflicts))
        if not result.restored and not result.conflicts:
            self.write("No había nada que deshacer.")



def main() -> int:
    try:
        return TextApp().run()
    except (KeyboardInterrupt, EOFError):
        print("\nHasta luego.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
