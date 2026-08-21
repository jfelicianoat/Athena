"""Tk desktop shell for Athena.

Tk is part of the Python distribution, keeping the desktop entry point optional and the
runtime dependency-free. All agent work runs on a background thread; the Tk thread only
renders messages and resolves explicit permission requests.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import queue
import sys
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from athena.agent_loop import AgentRunResult
from athena.cancellation import CancellationSource
from athena.events import EventName, RuntimeEvent
from athena.permissions import PermissionDecision, PermissionRequest
from athena_desktop.config import (
    CapabilityMode,
    DesktopSettings,
    ProviderKind,
    SettingsStore,
    resolve_token,
)
from athena_desktop.runtime import RunConfiguration, run_athena
from athena_desktop.service import (
    ManagedAthenaService,
    ManagedServiceRequest,
    ServiceAlreadyRunning,
    default_service_state_dir,
    start_managed_service,
)

_PROVIDER_LABELS = {
    "AI_Broker": ProviderKind.AI_BROKER,
    "OpenAI compatible": ProviderKind.OPENAI_COMPATIBLE,
}
_PROVIDER_NAMES = {value: key for key, value in _PROVIDER_LABELS.items()}
_MODE_LABELS: dict[str, CapabilityMode] = {
    "Desactivado": "off",
    "Preguntar": "ask",
    "Permitir": "allow",
}
_MODE_NAMES = {value: key for key, value in _MODE_LABELS.items()}


@dataclass(slots=True)
class _PermissionQuestion:
    request: PermissionRequest
    completed: threading.Event
    decision: PermissionDecision = PermissionDecision.DENY


class AthenaDesktopApp:
    def __init__(self, root: tk.Tk, store: SettingsStore | None = None) -> None:
        self.root = root
        self.store = store or SettingsStore()
        self.settings = self.store.load()
        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancellation: CancellationSource | None = None
        self.managed_service: ManagedAthenaService | None = None
        self.service_worker: threading.Thread | None = None
        self.closing = False

        self.workspace = tk.StringVar(value=self.settings.workspace)
        self.provider = tk.StringVar(value=_PROVIDER_NAMES[self.settings.provider])
        self.base_url = tk.StringVar(value=self.settings.base_url)
        self.model = tk.StringVar(value=self.settings.model)
        self.token = tk.StringVar(value=resolve_token(self.settings.provider, ""))
        self.writes = tk.StringVar(value=_MODE_NAMES[self.settings.writes])
        self.execution = tk.StringVar(value=_MODE_NAMES[self.settings.execution])
        self.max_iterations = tk.StringVar(value=str(self.settings.max_iterations))
        self.timeout = tk.StringVar(value=f"{self.settings.timeout_seconds:g}")
        self.status = tk.StringVar(value="Lista para empezar")
        self.provider_hint = tk.StringVar()
        self.service_status = tk.StringVar(value="Servicio detenido")
        self.service_url = tk.StringVar(value="")
        self.service_token = tk.StringVar(value="")

        self._configure_window()
        self._build_ui()
        self._provider_changed()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(75, self._drain_messages)

    def _configure_window(self) -> None:
        self.root.title("Athena Desktop")
        self.root.geometry("1180x780")
        self.root.minsize(920, 640)
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Header.TLabel", font=("Segoe UI", 20, "bold"))
        style.configure("Subtitle.TLabel", foreground="#5f6b7a")
        style.configure("Section.TLabelframe.Label", font=("Segoe UI", 10, "bold"))
        style.configure("Run.TButton", font=("Segoe UI", 10, "bold"), padding=(18, 8))
        style.configure("Status.TLabel", foreground="#2563a6", font=("Segoe UI", 10, "bold"))

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=20)
        outer.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(outer)
        header.pack(fill=tk.X, pady=(0, 16))
        ttk.Label(header, text="Athena", style="Header.TLabel").pack(side=tk.LEFT)
        ttk.Label(
            header,
            text="Agente autónomo para trabajar sobre tus proyectos",
            style="Subtitle.TLabel",
        ).pack(side=tk.LEFT, padx=(14, 0), pady=(8, 0))
        ttk.Label(header, textvariable=self.status, style="Status.TLabel").pack(
            side=tk.RIGHT, pady=(8, 0)
        )

        panes = ttk.Panedwindow(outer, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True)
        configuration = ttk.Frame(panes, padding=(0, 0, 16, 0), width=360)
        work = ttk.Frame(panes, padding=(16, 0, 0, 0))
        panes.add(configuration, weight=0)
        panes.add(work, weight=1)
        self._build_configuration(configuration)
        self._build_work_area(work)

    def _build_configuration(self, parent: ttk.Frame) -> None:
        project = ttk.LabelFrame(parent, text="Proyecto", style="Section.TLabelframe")
        project.pack(fill=tk.X, pady=(0, 12))
        row = ttk.Frame(project, padding=10)
        row.pack(fill=tk.X)
        ttk.Entry(row, textvariable=self.workspace).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(row, text="Elegir…", command=self._choose_workspace).pack(
            side=tk.LEFT, padx=(8, 0)
        )

        provider = ttk.LabelFrame(parent, text="Proveedor", style="Section.TLabelframe")
        provider.pack(fill=tk.X, pady=(0, 12))
        body = ttk.Frame(provider, padding=10)
        body.pack(fill=tk.X)
        self._field_label(body, "Tipo")
        combo = ttk.Combobox(
            body,
            textvariable=self.provider,
            values=tuple(_PROVIDER_LABELS),
            state="readonly",
        )
        combo.pack(fill=tk.X, pady=(0, 9))
        combo.bind("<<ComboboxSelected>>", lambda _: self._provider_changed())
        self._field_label(body, "URL base")
        ttk.Entry(body, textvariable=self.base_url).pack(fill=tk.X, pady=(0, 9))
        self._field_label(body, "Modelo o preferencia")
        ttk.Entry(body, textvariable=self.model).pack(fill=tk.X, pady=(0, 9))
        self._field_label(body, "Token")
        ttk.Entry(body, textvariable=self.token, show="●").pack(fill=tk.X, pady=(0, 6))
        ttk.Label(
            body,
            textvariable=self.provider_hint,
            style="Subtitle.TLabel",
            wraplength=315,
        ).pack(fill=tk.X)

        permissions = ttk.LabelFrame(parent, text="Permisos", style="Section.TLabelframe")
        permissions.pack(fill=tk.X, pady=(0, 12))
        body = ttk.Frame(permissions, padding=10)
        body.pack(fill=tk.X)
        self._field_label(body, "Cambios en archivos")
        ttk.Combobox(
            body,
            textvariable=self.writes,
            values=tuple(_MODE_LABELS),
            state="readonly",
        ).pack(fill=tk.X, pady=(0, 9))
        self._field_label(body, "Ejecución local")
        ttk.Combobox(
            body,
            textvariable=self.execution,
            values=tuple(_MODE_LABELS),
            state="readonly",
        ).pack(fill=tk.X)

        limits = ttk.LabelFrame(parent, text="Límites", style="Section.TLabelframe")
        limits.pack(fill=tk.X)
        body = ttk.Frame(limits, padding=10)
        body.pack(fill=tk.X)
        first = ttk.Frame(body)
        first.pack(fill=tk.X)
        ttk.Label(first, text="Iteraciones").pack(side=tk.LEFT)
        ttk.Entry(first, textvariable=self.max_iterations, width=8).pack(side=tk.RIGHT)
        second = ttk.Frame(body)
        second.pack(fill=tk.X, pady=(9, 0))
        ttk.Label(second, text="Tiempo máximo (s)").pack(side=tk.LEFT)
        ttk.Entry(second, textvariable=self.timeout, width=8).pack(side=tk.RIGHT)

    def _build_work_area(self, parent: ttk.Frame) -> None:
        self._build_service_area(parent)

        objective_frame = ttk.LabelFrame(
            parent, text="¿Qué quieres que haga?", style="Section.TLabelframe"
        )
        objective_frame.pack(fill=tk.X, pady=(0, 12))
        self.objective = tk.Text(
            objective_frame,
            height=5,
            wrap=tk.WORD,
            font=("Segoe UI", 10),
            relief=tk.FLAT,
            padx=10,
            pady=10,
        )
        self.objective.pack(fill=tk.X, padx=1, pady=1)

        actions = ttk.Frame(parent)
        actions.pack(fill=tk.X, pady=(0, 12))
        self.run_button = ttk.Button(
            actions, text="Iniciar Athena", command=self._start, style="Run.TButton"
        )
        self.run_button.pack(side=tk.LEFT)
        self.cancel_button = ttk.Button(
            actions, text="Detener", command=self._cancel, state=tk.DISABLED
        )
        self.cancel_button.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(actions, text="Limpiar", command=self._clear_output).pack(side=tk.RIGHT)

        notebook = ttk.Notebook(parent)
        notebook.pack(fill=tk.BOTH, expand=True)
        answer_tab = ttk.Frame(notebook)
        activity_tab = ttk.Frame(notebook)
        notebook.add(answer_tab, text="Resultado")
        notebook.add(activity_tab, text="Actividad")
        self.answer = self._text_panel(answer_tab, font=("Segoe UI", 10))
        self.activity = self._text_panel(activity_tab, font=("Cascadia Mono", 9))

    def _build_service_area(self, parent: ttk.Frame) -> None:
        service = ttk.LabelFrame(
            parent, text="Servicio local para ChatyGPT", style="Section.TLabelframe"
        )
        service.pack(fill=tk.X, pady=(0, 12))
        body = ttk.Frame(service, padding=10)
        body.pack(fill=tk.X)

        heading = ttk.Frame(body)
        heading.pack(fill=tk.X)
        ttk.Label(heading, textvariable=self.service_status, style="Status.TLabel").pack(
            side=tk.LEFT
        )
        self.service_start_button = ttk.Button(
            heading, text="Iniciar servicio", command=self._start_service
        )
        self.service_start_button.pack(side=tk.RIGHT)
        self.service_stop_button = ttk.Button(
            heading, text="Detener", command=self._stop_service, state=tk.DISABLED
        )
        self.service_stop_button.pack(side=tk.RIGHT, padx=(0, 8))

        connection = ttk.Frame(body)
        connection.pack(fill=tk.X, pady=(9, 0))
        ttk.Label(connection, text="URL").pack(side=tk.LEFT)
        ttk.Entry(connection, textvariable=self.service_url, state="readonly").pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0)
        )

        credential = ttk.Frame(body)
        credential.pack(fill=tk.X, pady=(7, 0))
        ttk.Label(credential, text="Token de Athena").pack(side=tk.LEFT)
        ttk.Entry(credential, textvariable=self.service_token, state="readonly").pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 8)
        )
        self.copy_service_token_button = ttk.Button(
            credential, text="Copiar", command=self._copy_service_token, state=tk.DISABLED
        )
        self.copy_service_token_button.pack(side=tk.RIGHT)
        ttk.Label(
            body,
            text=(
                "Esta credencial es distinta del token de AI_Broker. Athena la genera "
                "al iniciar el servicio y no la guarda en disco."
            ),
            style="Subtitle.TLabel",
            wraplength=650,
        ).pack(fill=tk.X, pady=(7, 0))

    @staticmethod
    def _field_label(parent: ttk.Frame, text: str) -> None:
        ttk.Label(parent, text=text).pack(anchor=tk.W, pady=(0, 3))

    @staticmethod
    def _text_panel(parent: ttk.Frame, *, font: tuple[str, int]) -> tk.Text:
        scroll = ttk.Scrollbar(parent, orient=tk.VERTICAL)
        panel = tk.Text(parent, wrap=tk.WORD, font=font, state=tk.DISABLED, padx=12, pady=12)
        panel.configure(yscrollcommand=scroll.set)
        scroll.configure(command=panel.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        return panel

    def _choose_workspace(self) -> None:
        selected = filedialog.askdirectory(
            title="Selecciona el proyecto",
            initialdir=self.workspace.get() or str(Path.cwd()),
        )
        if selected:
            self.workspace.set(selected)

    def _provider_changed(self) -> None:
        provider = _PROVIDER_LABELS[self.provider.get()]
        if provider is ProviderKind.AI_BROKER:
            self.provider_hint.set(
                "Athena traduce sus herramientas al contrato estructurado del broker y "
                "mantiene el control de permisos. Token: ATHENA_BROKER_TOKEN."
            )
            if self.base_url.get() == "http://localhost:1234/v1":
                self.base_url.set("http://localhost:8000")
        else:
            self.provider_hint.set("Se envía como Bearer. También puedes definir ATHENA_API_KEY.")
            if self.base_url.get() == "http://localhost:8000":
                self.base_url.set("http://localhost:1234/v1")

    def _settings_from_form(self) -> DesktopSettings:
        return DesktopSettings(
            provider=_PROVIDER_LABELS[self.provider.get()],
            base_url=self.base_url.get().strip(),
            model=self.model.get().strip(),
            workspace=self.workspace.get().strip(),
            writes=_MODE_LABELS[self.writes.get()],
            execution=_MODE_LABELS[self.execution.get()],
            max_iterations=int(self.max_iterations.get()),
            timeout_seconds=float(self.timeout.get()),
        )

    def _configuration_from_form(self) -> RunConfiguration:
        settings = self._settings_from_form()
        token = resolve_token(settings.provider, self.token.get())
        return RunConfiguration(
            workspace=Path(settings.workspace),
            objective=self.objective.get("1.0", tk.END).strip(),
            provider=settings.provider,
            base_url=settings.base_url,
            model=settings.model,
            token=token,
            writes=settings.writes,
            execution=settings.execution,
            max_iterations=settings.max_iterations,
            timeout_seconds=settings.timeout_seconds,
        )

    def _start(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        try:
            configuration = self._configuration_from_form()
            configuration.validate()
            self.store.save(self._settings_from_form())
        except (ValueError, OSError) as exc:
            messagebox.showerror("No se puede iniciar", str(exc), parent=self.root)
            return
        self._clear_output()
        self._append(self.activity, "Preparando la ejecución…\n")
        self.status.set("Athena está trabajando")
        self.run_button.configure(state=tk.DISABLED)
        self.cancel_button.configure(state=tk.NORMAL)
        self.cancellation = CancellationSource()
        self.worker = threading.Thread(
            target=self._run_worker,
            args=(configuration, self.cancellation),
            name="athena-desktop-run",
            daemon=True,
        )
        self.worker.start()

    def _run_worker(
        self, configuration: RunConfiguration, cancellation: CancellationSource
    ) -> None:
        try:
            result = asyncio.run(
                run_athena(
                    configuration,
                    cancellation,
                    on_event=lambda event: self.messages.put(("event", event)),
                    on_permission=self._request_permission,
                )
            )
        except BaseException as exc:
            self.messages.put(("error", exc))
        else:
            self.messages.put(("result", result))

    def _start_service(self) -> None:
        if self.service_worker is not None and self.service_worker.is_alive():
            return
        if self.managed_service is not None and self.managed_service.process.poll() is None:
            return
        try:
            settings = self._settings_from_form()
            if settings.provider is not ProviderKind.AI_BROKER:
                raise ValueError("El servicio gestionado necesita AI_Broker como proveedor")
            broker_token = resolve_token(settings.provider, self.token.get())
            request = ManagedServiceRequest(
                broker_base_url=settings.base_url,
                broker_token=broker_token,
                preferred_model=settings.model,
                state_dir=default_service_state_dir(),
            )
            self.store.save(settings)
        except (ValueError, OSError) as exc:
            messagebox.showerror("No se puede iniciar el servicio", str(exc), parent=self.root)
            return

        self.service_status.set("Iniciando servicio…")
        self.service_start_button.configure(state=tk.DISABLED)
        self.service_stop_button.configure(state=tk.DISABLED)
        self.copy_service_token_button.configure(state=tk.DISABLED)
        self.service_worker = threading.Thread(
            target=self._start_service_worker,
            args=(request,),
            name="athena-desktop-service-start",
            daemon=True,
        )
        self.service_worker.start()

    def _start_service_worker(self, request: ManagedServiceRequest) -> None:
        try:
            service = start_managed_service(request)
        except ServiceAlreadyRunning as exc:
            self.messages.put(("service_existing", exc))
        except BaseException as exc:
            self.messages.put(("service_error", exc))
        else:
            self.messages.put(("service_started", service))

    def _stop_service(self) -> None:
        service = self.managed_service
        if service is None:
            return
        self.service_status.set("Deteniendo servicio…")
        self.service_stop_button.configure(state=tk.DISABLED)
        self.service_worker = threading.Thread(
            target=self._stop_service_worker,
            args=(service,),
            name="athena-desktop-service-stop",
            daemon=True,
        )
        self.service_worker.start()

    def _stop_service_worker(self, service: ManagedAthenaService) -> None:
        try:
            service.stop()
        except BaseException as exc:
            self.messages.put(("service_error", exc))
        else:
            self.messages.put(("service_stopped", service))

    def _copy_service_token(self) -> None:
        token = self.service_token.get()
        if not token:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(token)
        self.root.update_idletasks()
        self.service_status.set("Token copiado al portapapeles")

    def _request_permission(self, request: PermissionRequest) -> PermissionDecision:
        question = _PermissionQuestion(request, threading.Event())
        self.messages.put(("permission", question))
        while not question.completed.wait(0.1):
            if self.closing:
                return PermissionDecision.DENY
        return question.decision

    def _drain_messages(self) -> None:
        while True:
            try:
                kind, payload = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == "event" and isinstance(payload, RuntimeEvent):
                self._show_event(payload)
            elif kind == "permission" and isinstance(payload, _PermissionQuestion):
                self._show_permission(payload)
            elif kind == "result" and isinstance(payload, AgentRunResult):
                self._show_result(payload)
            elif kind == "error" and isinstance(payload, BaseException):
                self._show_error(payload)
            elif kind == "service_started" and isinstance(payload, ManagedAthenaService):
                self._show_service_started(payload)
            elif kind == "service_stopped" and isinstance(payload, ManagedAthenaService):
                self._show_service_stopped(payload)
            elif kind == "service_existing" and isinstance(payload, ServiceAlreadyRunning):
                self._show_existing_service(payload)
            elif kind == "service_error" and isinstance(payload, BaseException):
                self._show_service_error(payload)
        if not self.closing:
            self.root.after(75, self._drain_messages)

    def _show_event(self, event: RuntimeEvent) -> None:
        timestamp = event.occurred_at.astimezone().strftime("%H:%M:%S")
        details = json.dumps(event.payload, ensure_ascii=False, default=str)
        self._append(self.activity, f"{timestamp}  {event.name.value}  {details}\n")
        status_by_event = {
            EventName.MODEL_STARTED: "Consultando el modelo",
            EventName.TOOL_STARTED: "Usando una herramienta",
            EventName.VERIFICATION_STARTED: "Verificando el resultado",
            EventName.AGENT_COMPLETED: "Ejecución completada",
            EventName.AGENT_CANCELLED: "Ejecución detenida",
            EventName.AGENT_FAILED: "La ejecución ha fallado",
        }
        if event.name in status_by_event:
            self.status.set(status_by_event[event.name])

    def _show_permission(self, question: _PermissionQuestion) -> None:
        request = question.request
        effects = "\n".join(f"• {effect}" for effect in request.possible_effects)
        message = (
            f"Athena solicita usar: {request.tool_name}\n\n"
            f"Acción: {request.action or request.operation}\n"
            f"Riesgo: {request.risk.value}\n"
            f"Motivo: {request.reason or 'No especificado'}"
        )
        if effects:
            message += f"\n\nPosibles efectos:\n{effects}"
        allowed = messagebox.askyesno(
            "Athena necesita permiso", message, icon=messagebox.WARNING, parent=self.root
        )
        question.decision = PermissionDecision.ALLOW if allowed else PermissionDecision.DENY
        question.completed.set()

    def _show_result(self, result: AgentRunResult) -> None:
        if result.answer:
            self._append(self.answer, result.answer.strip() + "\n")
        if result.verification is not None:
            self._append(
                self.answer,
                f"\nVerificación: {result.verification.status.value} — "
                f"{result.verification.summary}\n",
            )
        if result.error is not None:
            self._append(self.answer, f"\nError: {result.error}\n")
        self.status.set(f"Finalizada: {result.status.value}")
        self._finish_run()

    def _show_error(self, error: BaseException) -> None:
        self._append(self.answer, f"No se pudo completar la ejecución:\n{error}\n")
        self.status.set("Error de ejecución")
        self._finish_run()
        messagebox.showerror("Athena", str(error), parent=self.root)

    def _show_service_started(self, service: ManagedAthenaService) -> None:
        self.managed_service = service
        self.service_url.set(service.endpoint.base_url)
        self.service_token.set(service.endpoint.token)
        self.service_status.set("Servicio disponible")
        self.service_start_button.configure(state=tk.DISABLED)
        self.service_stop_button.configure(state=tk.NORMAL)
        self.copy_service_token_button.configure(state=tk.NORMAL)

    def _show_service_stopped(self, service: ManagedAthenaService) -> None:
        if self.managed_service is service:
            self.managed_service = None
        self.service_url.set("")
        self.service_token.set("")
        self.service_status.set("Servicio detenido")
        self.service_start_button.configure(state=tk.NORMAL)
        self.service_stop_button.configure(state=tk.DISABLED)
        self.copy_service_token_button.configure(state=tk.DISABLED)

    def _show_existing_service(self, service: ServiceAlreadyRunning) -> None:
        self.managed_service = None
        self.service_url.set(service.base_url)
        self.service_token.set("")
        self.service_status.set("Servicio iniciado por otra aplicación")
        self.service_start_button.configure(state=tk.NORMAL)
        self.service_stop_button.configure(state=tk.DISABLED)
        self.copy_service_token_button.configure(state=tk.DISABLED)
        messagebox.showinfo(
            "Servicio de Athena ya iniciado",
            (
                f"Athena ya está funcionando en {service.base_url}.\n\n"
                "Esta ventana no la inició y por seguridad no puede recuperar su token ni "
                "detenerla. La aplicación que inició el servicio —por ejemplo ChatyGPT— "
                "debe conservar el token anunciado por Athena."
            ),
            parent=self.root,
        )

    def _show_service_error(self, error: BaseException) -> None:
        self.service_status.set("Error del servicio")
        self.service_start_button.configure(state=tk.NORMAL)
        self.service_stop_button.configure(state=tk.DISABLED)
        self.copy_service_token_button.configure(state=tk.DISABLED)
        messagebox.showerror("Servicio de Athena", str(error), parent=self.root)

    def _cancel(self) -> None:
        if self.cancellation is not None:
            self.cancellation.cancel()
            self.status.set("Deteniendo Athena…")
            self.cancel_button.configure(state=tk.DISABLED)

    def _finish_run(self) -> None:
        self.run_button.configure(state=tk.NORMAL)
        self.cancel_button.configure(state=tk.DISABLED)
        self.cancellation = None

    def _clear_output(self) -> None:
        for panel in (self.answer, self.activity):
            panel.configure(state=tk.NORMAL)
            panel.delete("1.0", tk.END)
            panel.configure(state=tk.DISABLED)

    @staticmethod
    def _append(panel: tk.Text, text: str) -> None:
        panel.configure(state=tk.NORMAL)
        panel.insert(tk.END, text)
        panel.see(tk.END)
        panel.configure(state=tk.DISABLED)

    def _close(self) -> None:
        self.closing = True
        if self.cancellation is not None:
            self.cancellation.cancel()
        if self.managed_service is not None:
            self.managed_service.stop()
        self.root.destroy()


def main() -> int:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        _report_graphics_error(exc)
        return 1
    AthenaDesktopApp(root)
    root.mainloop()
    return 0


def _report_graphics_error(error: tk.TclError) -> None:
    message = (
        "Athena Desktop no puede abrir el sistema gráfico de Python.\n\n"
        "Repara o reinstala Python incluyendo la opción Tcl/Tk and IDLE y vuelve a "
        f"ejecutar athena-desktop.\n\nDetalle: {error}"
    )
    if sys.platform == "win32":
        try:
            ctypes.windll.user32.MessageBoxW(0, message, "Athena Desktop", 0x10)
            return
        except (AttributeError, OSError):
            pass
    print(message, file=sys.stderr)


__all__ = ["AthenaDesktopApp", "main"]
