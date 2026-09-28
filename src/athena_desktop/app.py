"""Ventana de escritorio de Athena (Tk).

Tk viene con Python, asi que el escritorio no anade dependencias. Todo el trabajo corre
en hilos aparte; el hilo de Tk solo pinta mensajes y resuelve las peticiones de permiso.

La ventana es una interfaz y nada mas: no contiene logica de agente. Lo que hace un
trabajo lo decide `runtime`, que corre sobre el mismo `RunRegistry` que el servicio.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import os
import queue
import sys
import threading
import time
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from athena import __version__
from athena.agent_loop import AgentRunResult
from athena.cancellation import CancellationSource
from athena.events import EventName, RuntimeEvent
from athena.permissions import PermissionDecision, PermissionRequest
from athena.rollback import RollbackResult
from athena_desktop.config import (
    CapabilityMode,
    DesktopSettings,
    ProviderKind,
    SettingsStore,
    environment_token,
)
from athena_desktop.presentacion import (
    ActivityLine,
    ResultView,
    describe_event,
    present_approval,
    present_result,
    status_label,
    technical_line,
)
from athena_desktop.runtime import (
    ConnectionReport,
    RunConfiguration,
    RunHandle,
    RunSummary,
    TaskKind,
    check_connection,
    default_state_dir,
    list_runs,
    requires_workspace_change,
    roll_back_run,
    run_athena,
)
from athena_desktop.service import (
    ManagedAthenaService,
    ManagedServiceRequest,
    ServiceAlreadyRunning,
    ServiceState,
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

_WRITES_HELP = {
    "off": "Athena no puede crear ni cambiar archivos.",
    "ask": "Athena te enseña cada cambio (con sus diferencias) y espera tu permiso.",
    "allow": "Athena cambia archivos sin preguntar. Puedes deshacerlo después.",
}
_EXEC_HELP = {
    "off": "No se ejecuta nada del proyecto: ni tests ni scripts. Sin esto Athena no puede "
    "comprobar un cambio de código.",
    "ask": "Athena te enseña cada comando (tests, lint…) antes de ejecutarlo.",
    "allow": "Athena ejecuta tests y comandos permitidos sin preguntar. Es código del proyecto.",
}

_TASKS: tuple[tuple[TaskKind, str, str], ...] = (
    ("question", "Responder una pregunta", "Lee el proyecto y responde. No cambia nada."),
    ("change", "Modificar el proyecto", "Cambia código o archivos y lo comprueba."),
    ("documents", "Crear documentos", "Escribe los documentos que indiques como entregables."),
)

#: Lineas de actividad que se conservan en pantalla. El historial completo esta en disco.
_MAX_ACTIVITY_LINES = 3000


@dataclass(slots=True)
class _PermissionQuestion:
    request: PermissionRequest
    completed: threading.Event = field(default_factory=threading.Event)
    #: El trabajo se detuvo mientras la pregunta estaba en pantalla: ya no vale.
    withdrawn: threading.Event = field(default_factory=threading.Event)
    decision: PermissionDecision = PermissionDecision.DENY


class ApprovalDialog:
    """Una peticion de permiso con todo lo necesario para decidir (A18)."""

    def __init__(self, root: tk.Misc, question: _PermissionQuestion) -> None:
        self.question = question
        view = present_approval(question.request)
        self.window = tk.Toplevel(root)
        self.window.title("Athena necesita permiso")
        self.window.transient(root.winfo_toplevel())
        self.window.geometry("780x580")
        self.window.minsize(560, 420)
        self.window.protocol("WM_DELETE_WINDOW", self.deny)
        body = ttk.Frame(self.window, padding=16)
        body.pack(fill=tk.BOTH, expand=True)

        ttk.Label(body, text=view.title, style="DialogTitle.TLabel", wraplength=740).pack(
            anchor=tk.W
        )
        facts = ttk.Frame(body)
        facts.pack(fill=tk.X, pady=(10, 6))
        for row, (label, value) in enumerate(
            (
                ("Acción", view.action),
                ("Proyecto", view.project),
                ("Riesgo", view.risk),
                ("Motivo", view.reason),
            )
        ):
            ttk.Label(facts, text=f"{label}:", style="Field.TLabel").grid(
                row=row, column=0, sticky=tk.NW, padx=(0, 8), pady=1
            )
            ttk.Label(facts, text=value, wraplength=640).grid(
                row=row, column=1, sticky=tk.W, pady=1
            )
        if view.effects:
            ttk.Label(
                body,
                text="Qué puede pasar:\n" + "\n".join(f"• {item}" for item in view.effects),
                wraplength=740,
            ).pack(anchor=tk.W, pady=(4, 6))
        if view.preview:
            ttk.Label(body, text=view.preview_title, style="Field.TLabel").pack(anchor=tk.W)
            frame = ttk.Frame(body)
            frame.pack(fill=tk.BOTH, expand=True, pady=(4, 8))
            scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL)
            preview = tk.Text(
                frame, wrap=tk.NONE, font=("Cascadia Mono", 9), height=14, relief=tk.FLAT
            )
            preview.configure(yscrollcommand=scroll.set)
            scroll.configure(command=preview.yview)
            scroll.pack(side=tk.RIGHT, fill=tk.Y)
            preview.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            preview.tag_configure("add", foreground="#1a7f37")
            preview.tag_configure("del", foreground="#cf222e")
            preview.tag_configure("hunk", foreground="#0550ae")
            for line in view.preview.splitlines():
                tag = ""
                if line.startswith("+") and not line.startswith("+++"):
                    tag = "add"
                elif line.startswith("-") and not line.startswith("---"):
                    tag = "del"
                elif line.startswith("@@"):
                    tag = "hunk"
                preview.insert(tk.END, line + "\n", tag)
            preview.configure(state=tk.DISABLED)
        else:
            ttk.Frame(body).pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            body,
            text=view.scope,
            style="Hint.TLabel",
        ).pack(anchor=tk.W)
        buttons = ttk.Frame(body)
        buttons.pack(fill=tk.X, pady=(10, 0))
        self.deny_button = ttk.Button(buttons, text="Denegar", command=self.deny)
        self.deny_button.pack(side=tk.RIGHT)
        self.allow_button = ttk.Button(
            buttons, text="Permitir una vez", command=self.allow, style="Run.TButton"
        )
        self.allow_button.pack(side=tk.RIGHT, padx=(0, 8))
        self.window.bind("<Escape>", lambda _: self.deny())
        self.deny_button.focus_set()
        self.window.grab_set()
        self.window.after(200, self._watch)

    def _watch(self) -> None:
        if self.question.completed.is_set():
            return
        if self.question.withdrawn.is_set():
            self._finish(PermissionDecision.DENY)
            return
        self.window.after(200, self._watch)

    def allow(self) -> None:
        self._finish(PermissionDecision.ALLOW)

    def deny(self) -> None:
        self._finish(PermissionDecision.DENY)

    def _finish(self, decision: PermissionDecision) -> None:
        if not self.question.completed.is_set():
            self.question.decision = decision
            self.question.completed.set()
        try:
            self.window.grab_release()
            self.window.destroy()
        except tk.TclError:
            pass


class ScrollableFrame(ttk.Frame):
    """Un marco con barra de desplazamiento propia, para pantallas pequeñas (A27)."""

    def __init__(self, parent: tk.Misc, *, width: int) -> None:
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0, width=width, borderwidth=0)
        self.scroll = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.inner.bind(
            "<Configure>",
            lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor=tk.NW)
        self.canvas.bind(
            "<Configure>", lambda event: self.canvas.itemconfigure(self._window, width=event.width)
        )
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.inner.bind("<Enter>", lambda _: self._bind_wheel(True))
        self.inner.bind("<Leave>", lambda _: self._bind_wheel(False))

    def _bind_wheel(self, active: bool) -> None:
        if active:
            self.canvas.bind_all("<MouseWheel>", self._on_wheel)
        else:
            self.canvas.unbind_all("<MouseWheel>")

    def _on_wheel(self, event: tk.Event[tk.Misc]) -> None:
        if self.canvas.yview() != (0.0, 1.0):
            self.canvas.yview_scroll(int(-event.delta / 120), "units")


class AthenaDesktopApp:
    def __init__(
        self,
        root: tk.Tk,
        store: SettingsStore | None = None,
        *,
        state_dir: Path | None = None,
    ) -> None:
        self.root = root
        self.store = store or SettingsStore()
        self.settings = self.store.load()
        self.state_dir = state_dir or default_state_dir()
        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancellation: CancellationSource | None = None
        self.handle: RunHandle | None = None
        self.active: RunConfiguration | None = None
        self.last_run_id: str | None = None
        self.last_result_files: tuple[str, ...] = ()
        self.pending_question: _PermissionQuestion | None = None
        self.managed_service: ManagedAthenaService | None = None
        self.service_worker: threading.Thread | None = None
        self.closing = False
        self.started_at = 0.0
        self.model_calls = 0
        self.history: list[RunSummary] = []
        #: Credenciales en memoria, por destino (proveedor y URL). Nunca se reutiliza la
        #: de un destino en otro (A03).
        self._tokens: dict[tuple[str, str], str] = {}
        self._config_widgets: list[ttk.Widget] = []

        provider = self.settings.provider
        self.workspace = tk.StringVar(value=self.settings.workspace)
        self.provider = tk.StringVar(value=_PROVIDER_NAMES[provider])
        self.base_url = tk.StringVar(value=self.settings.url_for(provider))
        self.model = tk.StringVar(value=self.settings.model_for(provider))
        self.token = tk.StringVar(value=environment_token(provider))
        self._token_key = self._destination()
        self.writes = tk.StringVar(value=_MODE_NAMES[self.settings.writes])
        self.execution = tk.StringVar(value=_MODE_NAMES[self.settings.execution])
        self.max_iterations = tk.StringVar(value=str(self.settings.max_iterations))
        self.timeout = tk.StringVar(value=f"{self.settings.timeout_seconds:g}")
        self.task_kind = tk.StringVar(value=self.settings.task_kind)
        self.deliverables = tk.StringVar(value=self.settings.deliverables)
        self.status = tk.StringVar()
        self.project_info = tk.StringVar()
        self.provider_hint = tk.StringVar()
        self.connection_result = tk.StringVar(value="Sin probar")
        self.writes_help = tk.StringVar()
        self.execution_help = tk.StringVar()
        self.show_technical = tk.BooleanVar(value=False)
        self.service_status = tk.StringVar(value="Servicio detenido")
        self.service_url = tk.StringVar(value="")
        self.service_token = tk.StringVar(value="")
        self.p_objective = tk.StringVar(value="—")
        self.p_phase = tk.StringVar(value="Sin empezar")
        self.p_calls = tk.StringVar(value="0")
        self.p_elapsed = tk.StringVar(value="—")
        self.p_last = tk.StringVar(value="—")
        self.p_permission = tk.StringVar(value="Ninguno")
        self.p_config = tk.StringVar(value="—")

        self._configure_window()
        self._build_ui()
        self._provider_changed(initial=True)
        for variable in (self.workspace, self.task_kind, self.writes, self.execution):
            variable.trace_add("write", lambda *_: self._refresh())
        self._refresh()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(75, self._drain_messages)
        self.root.after(2000, self._watch_service)
        self.root.after(300, self._refresh_history)

    # ------------------------------------------------------------------ construccion

    def _configure_window(self) -> None:
        self.root.title(f"Athena Desktop v{__version__}")
        self.root.geometry("1220x820")
        self.root.minsize(920, 640)
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Header.TLabel", font=("Segoe UI", 20, "bold"))
        style.configure("Version.TLabel", foreground="#5f6b7a", font=("Segoe UI", 10))
        style.configure("Subtitle.TLabel", foreground="#5f6b7a")
        style.configure("Hint.TLabel", foreground="#5f6b7a", font=("Segoe UI", 9))
        style.configure("Field.TLabel", font=("Segoe UI", 9, "bold"))
        style.configure("Section.TLabelframe.Label", font=("Segoe UI", 10, "bold"))
        style.configure("Run.TButton", font=("Segoe UI", 10, "bold"), padding=(16, 6))
        style.configure("Status.TLabel", foreground="#2563a6", font=("Segoe UI", 10, "bold"))
        style.configure("Ok.TLabel", foreground="#1a7f37")
        style.configure("Bad.TLabel", foreground="#cf222e")
        style.configure("DialogTitle.TLabel", font=("Segoe UI", 13, "bold"))

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=(16, 12, 16, 12))
        outer.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(outer)
        header.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(header, text="Athena", style="Header.TLabel").pack(side=tk.LEFT)
        ttk.Label(header, text=f"v{__version__}", style="Version.TLabel").pack(
            side=tk.LEFT, padx=(8, 0), pady=(10, 0)
        )
        ttk.Label(
            header,
            text="Agente autónomo para trabajar sobre tus proyectos",
            style="Subtitle.TLabel",
        ).pack(side=tk.LEFT, padx=(14, 0), pady=(10, 0))
        ttk.Label(header, textvariable=self.status, style="Status.TLabel").pack(
            side=tk.RIGHT, pady=(10, 0)
        )

        panes = ttk.Panedwindow(outer, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True)
        left = ScrollableFrame(panes, width=350)
        right = ttk.Frame(panes, padding=(12, 0, 0, 0))
        panes.add(left, weight=0)
        panes.add(right, weight=1)
        self._build_configuration(left.inner)

        self.tabs = ttk.Notebook(right)
        self.tabs.pack(fill=tk.BOTH, expand=True)
        work = ttk.Frame(self.tabs, padding=10)
        history = ttk.Frame(self.tabs, padding=10)
        service = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(work, text="Trabajo")
        self.tabs.add(history, text="Historial")
        self.tabs.add(service, text="Servicio para otras apps")
        self._build_work(work)
        self._build_history(history)
        self._build_service(service)

    def _section(self, parent: tk.Misc, title: str) -> ttk.Frame:
        frame = ttk.LabelFrame(parent, text=title, style="Section.TLabelframe")
        frame.pack(fill=tk.X, pady=(0, 10), padx=(0, 6))
        body = ttk.Frame(frame, padding=8)
        body.pack(fill=tk.X)
        return body

    def _build_configuration(self, parent: ttk.Frame) -> None:
        body = self._section(parent, "1. Proyecto")
        row = ttk.Frame(body)
        row.pack(fill=tk.X)
        entry = ttk.Entry(row, textvariable=self.workspace)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        choose = ttk.Button(row, text="Elegir…", command=self._choose_workspace)
        choose.pack(side=tk.LEFT, padx=(6, 0))
        self.project_label = ttk.Label(
            body, textvariable=self.project_info, wraplength=320, style="Hint.TLabel"
        )
        self.project_label.pack(fill=tk.X, pady=(4, 0))
        self._config_widgets += [entry, choose]

        body = self._section(parent, "2. Conexión con el modelo")
        ttk.Label(body, text="Proveedor").pack(anchor=tk.W)
        combo = ttk.Combobox(
            body, textvariable=self.provider, values=tuple(_PROVIDER_LABELS), state="readonly"
        )
        combo.pack(fill=tk.X, pady=(0, 6))
        combo.bind("<<ComboboxSelected>>", lambda _: self._provider_changed())
        ttk.Label(body, text="URL").pack(anchor=tk.W)
        url = ttk.Entry(body, textvariable=self.base_url)
        url.pack(fill=tk.X, pady=(0, 6))
        url.bind("<FocusOut>", lambda _: self._switch_credential())
        ttk.Label(body, text="Modelo o preferencia").pack(anchor=tk.W)
        model = ttk.Entry(body, textvariable=self.model)
        model.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(body, text="Token de este proveedor").pack(anchor=tk.W)
        token = ttk.Entry(body, textvariable=self.token, show="●")
        token.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(body, textvariable=self.provider_hint, wraplength=320, style="Hint.TLabel").pack(
            fill=tk.X
        )
        test_row = ttk.Frame(body)
        test_row.pack(fill=tk.X, pady=(6, 0))
        self.test_button = ttk.Button(
            test_row, text="Probar conexión", command=self._test_connection
        )
        self.test_button.pack(side=tk.LEFT)
        self.connection_label = ttk.Label(
            body, textvariable=self.connection_result, wraplength=320, style="Hint.TLabel"
        )
        self.connection_label.pack(fill=tk.X, pady=(4, 0))
        self._config_widgets += [combo, url, model, token, self.test_button]

        body = self._section(parent, "3. Permisos")
        ttk.Label(body, text="Cambios en archivos").pack(anchor=tk.W)
        writes = ttk.Combobox(
            body, textvariable=self.writes, values=tuple(_MODE_LABELS), state="readonly"
        )
        writes.pack(fill=tk.X)
        ttk.Label(body, textvariable=self.writes_help, wraplength=320, style="Hint.TLabel").pack(
            fill=tk.X, pady=(2, 8)
        )
        ttk.Label(body, text="Ejecutar código del proyecto").pack(anchor=tk.W)
        execution = ttk.Combobox(
            body, textvariable=self.execution, values=tuple(_MODE_LABELS), state="readonly"
        )
        execution.pack(fill=tk.X)
        ttk.Label(body, textvariable=self.execution_help, wraplength=320, style="Hint.TLabel").pack(
            fill=tk.X, pady=(2, 0)
        )
        self._config_widgets += [writes, execution]

        body = self._section(parent, "4. Límites")
        first = ttk.Frame(body)
        first.pack(fill=tk.X)
        ttk.Label(first, text="Iteraciones máximas").pack(side=tk.LEFT)
        iterations = ttk.Entry(first, textvariable=self.max_iterations, width=8)
        iterations.pack(side=tk.RIGHT)
        second = ttk.Frame(body)
        second.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(second, text="Tiempo máximo (s)").pack(side=tk.LEFT)
        timeout = ttk.Entry(second, textvariable=self.timeout, width=8)
        timeout.pack(side=tk.RIGHT)
        ttk.Label(
            body,
            text="Un modelo local puede tardar varios minutos por turno.",
            wraplength=320,
            style="Hint.TLabel",
        ).pack(fill=tk.X, pady=(4, 0))
        self._config_widgets += [iterations, timeout]

    def _build_work(self, parent: ttk.Frame) -> None:
        kinds = ttk.LabelFrame(parent, text="Tipo de tarea", style="Section.TLabelframe")
        kinds.pack(fill=tk.X, pady=(0, 6))
        kinds_body = ttk.Frame(kinds, padding=(8, 2, 8, 6))
        kinds_body.pack(fill=tk.X)
        radios = ttk.Frame(kinds_body)
        radios.pack(fill=tk.X)
        for value, label, _ in _TASKS:
            radio = ttk.Radiobutton(radios, text=label, value=value, variable=self.task_kind)
            radio.pack(side=tk.LEFT, padx=(0, 16))
            self._config_widgets.append(radio)
        self.task_hint = tk.StringVar()
        self._wrapping(
            ttk.Label(kinds_body, textvariable=self.task_hint, style="Hint.TLabel")
        ).pack(fill=tk.X, padx=(20, 0))
        self.deliverables_row = ttk.Frame(kinds_body)
        ttk.Label(self.deliverables_row, text="Entregables:").pack(side=tk.LEFT)
        self.deliverables_entry = ttk.Entry(self.deliverables_row, textvariable=self.deliverables)
        self.deliverables_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 6))
        ttk.Label(self.deliverables_row, text="(separados por comas)", style="Hint.TLabel").pack(
            side=tk.LEFT
        )
        self._config_widgets.append(self.deliverables_entry)

        objective = ttk.LabelFrame(
            parent, text="¿Qué quieres que haga?", style="Section.TLabelframe"
        )
        objective.pack(fill=tk.X, pady=(0, 6))
        self.objective = tk.Text(
            objective,
            height=3,
            wrap=tk.WORD,
            font=("Segoe UI", 10),
            relief=tk.FLAT,
            padx=8,
            pady=6,
            undo=True,
        )
        self.objective.pack(fill=tk.X, padx=1, pady=1)
        self.objective.bind("<KeyRelease>", lambda _: self._refresh())
        self.objective.bind("<Control-Return>", self._start_from_keyboard)
        self._wrapping(
            ttk.Label(
                objective,
                text="Ejemplos: «Explica cómo se calcula el total» · «Corrige el test que "
                "falla en tests/test_calc.py» · Ctrl+Intro para empezar",
                style="Hint.TLabel",
            )
        ).pack(fill=tk.X, padx=6, pady=(0, 4))

        actions = ttk.Frame(parent)
        actions.pack(fill=tk.X, pady=(0, 6))
        self.run_button = ttk.Button(
            actions, text="Iniciar", command=self._start, style="Run.TButton"
        )
        self.run_button.pack(side=tk.LEFT)
        self.cancel_button = ttk.Button(
            actions, text="Detener", command=self._cancel, state=tk.DISABLED
        )
        self.cancel_button.pack(side=tk.LEFT, padx=(8, 0))
        self.revise_button = ttk.Button(
            actions, text="Cambiar objetivo…", command=self._revise_goal, state=tk.DISABLED
        )
        self.revise_button.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(actions, text="Limpiar pantalla", command=self._clear_screen).pack(side=tk.RIGHT)

        progress = ttk.LabelFrame(parent, text="Estado del trabajo", style="Section.TLabelframe")
        # Oculto hasta el primer trabajo: antes solo enseña guiones y quita sitio al
        # resultado en una ventana pequeña.
        self.progress_frame = progress
        grid = ttk.Frame(progress, padding=(8, 2))
        grid.pack(fill=tk.X)
        # Compacto, a dos columnas: en una ventana pequeña el resultado tiene que seguir
        # viéndose debajo (A27).
        pairs = (
            (("Fase", self.p_phase), ("Tiempo", self.p_elapsed)),
            (("Llamadas al modelo", self.p_calls), ("Permiso pendiente", self.p_permission)),
        )
        for row, pair in enumerate(pairs):
            for half, (label, variable) in enumerate(pair):
                ttk.Label(grid, text=label, style="Field.TLabel").grid(
                    row=row, column=half * 2, sticky=tk.NW, padx=(0 if half == 0 else 16, 8)
                )
                ttk.Label(grid, textvariable=variable).grid(
                    row=row, column=half * 2 + 1, sticky=tk.W
                )
        for row, (label, variable) in enumerate(
            (
                ("Objetivo", self.p_objective),
                ("Último paso", self.p_last),
                ("Configuración", self.p_config),
            ),
            start=2,
        ):
            ttk.Label(grid, text=label, style="Field.TLabel").grid(
                row=row, column=0, sticky=tk.NW, padx=(0, 8)
            )
            self._wrapping(ttk.Label(grid, textvariable=variable)).grid(
                row=row, column=1, columnspan=3, sticky=tk.EW
            )
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(3, weight=1)
        self.progress_bar = ttk.Progressbar(progress, mode="determinate", maximum=100)
        self.progress_bar.pack(fill=tk.X, padx=8, pady=(2, 6))

        panes = ttk.Notebook(parent)
        panes.pack(fill=tk.BOTH, expand=True)
        self._results_anchor = panes
        result_tab = ttk.Frame(panes)
        activity_tab = ttk.Frame(panes)
        panes.add(result_tab, text="Resultado")
        panes.add(activity_tab, text="Actividad")
        self.results_notebook = panes

        result_actions = ttk.Frame(result_tab, padding=(0, 4))
        result_actions.pack(side=tk.BOTTOM, fill=tk.X)
        self.undo_button = ttk.Button(
            result_actions, text="Deshacer cambios", command=self._undo_last, state=tk.DISABLED
        )
        self.undo_button.pack(side=tk.LEFT)
        self.retry_button = ttk.Button(
            result_actions, text="Reintentar", command=self._retry, state=tk.DISABLED
        )
        self.retry_button.pack(side=tk.LEFT, padx=(8, 0))
        self.answer = self._text_panel(result_tab, font=("Segoe UI", 10))
        self.answer.tag_configure("head", font=("Segoe UI", 12, "bold"))
        self.answer.tag_configure("ok", foreground="#1a7f37")
        self.answer.tag_configure("warn", foreground="#9a6700")
        self.answer.tag_configure("error", foreground="#cf222e")
        self.answer.tag_configure("section", font=("Segoe UI", 10, "bold"))
        self.answer.tag_configure("muted", foreground="#5f6b7a")

        technical = ttk.Frame(activity_tab, padding=(0, 4))
        technical.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Checkbutton(
            technical,
            text="Mostrar detalles técnicos (eventos y datos)",
            variable=self.show_technical,
        ).pack(side=tk.LEFT)
        self.activity = self._text_panel(activity_tab, font=("Segoe UI", 9))
        self.activity.tag_configure("ok", foreground="#1a7f37")
        self.activity.tag_configure("warn", foreground="#9a6700")
        self.activity.tag_configure("error", foreground="#cf222e")
        self.activity.tag_configure("tech", foreground="#8b949e", font=("Cascadia Mono", 8))
        self._write_answer(
            [
                ("Cómo empezar\n", "head"),
                (
                    "1. Elige la carpeta del proyecto.\n2. Configura la conexión y pulsa «Probar "
                    "conexión».\n3. Elige el tipo de tarea y los permisos.\n4. Describe el "
                    "encargo y pulsa «Iniciar».\n\n",
                    "",
                ),
                (
                    "Cada trabajo queda guardado en «Historial», desde donde puedes reanudarlo "
                    "o deshacer sus cambios.",
                    "muted",
                ),
            ]
        )

    def _build_history(self, parent: ttk.Frame) -> None:
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(toolbar, text="Actualizar", command=self._refresh_history).pack(side=tk.LEFT)
        self.resume_button = ttk.Button(
            toolbar, text="Reanudar", command=self._resume_selected, state=tk.DISABLED
        )
        self.resume_button.pack(side=tk.LEFT, padx=(8, 0))
        self.history_undo_button = ttk.Button(
            toolbar,
            text="Deshacer cambios",
            command=self._undo_selected,
            state=tk.DISABLED,
        )
        self.history_undo_button.pack(side=tk.LEFT, padx=(8, 0))
        self.reuse_button = ttk.Button(
            toolbar,
            text="Usar como nuevo encargo",
            command=self._reuse_selected,
            state=tk.DISABLED,
        )
        self.reuse_button.pack(side=tk.LEFT, padx=(8, 0))
        self.history_status = tk.StringVar(value="")
        ttk.Label(toolbar, textvariable=self.history_status, style="Hint.TLabel").pack(
            side=tk.RIGHT
        )

        columns = ("fecha", "estado", "proyecto", "objetivo")
        tree_frame = ttk.Frame(parent)
        tree_frame.pack(fill=tk.BOTH, expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=10)
        for column, title, width in (
            ("fecha", "Fecha", 130),
            ("estado", "Estado", 190),
            ("proyecto", "Proyecto", 140),
            ("objetivo", "Objetivo", 460),
        ):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, stretch=column == "objetivo")
        scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda _: self._history_selected())

        detail = ttk.LabelFrame(parent, text="Detalle", style="Section.TLabelframe")
        detail.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self.history_detail = self._text_panel(detail, font=("Segoe UI", 9))

    def _build_service(self, parent: ttk.Frame) -> None:
        ttk.Label(
            parent,
            text=(
                "Esto no hace falta para trabajar desde esta ventana. Sirve para que otras "
                "aplicaciones —ChatyGPT, Agora— usen Athena en este equipo."
            ),
            wraplength=760,
        ).pack(anchor=tk.W, pady=(0, 10))
        service = ttk.LabelFrame(
            parent, text="Servicio local de Athena", style="Section.TLabelframe"
        )
        service.pack(fill=tk.X)
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
        ttk.Entry(credential, textvariable=self.service_token, state="readonly", show="●").pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 8)
        )
        self.copy_service_token_button = ttk.Button(
            credential, text="Copiar", command=self._copy_service_token, state=tk.DISABLED
        )
        self.copy_service_token_button.pack(side=tk.RIGHT)
        ttk.Label(
            body,
            text=(
                "Usa la conexión con AI_Broker configurada a la izquierda. Esta credencial "
                "es distinta del token de AI_Broker: Athena la genera al iniciar el servicio "
                "y no la guarda en disco."
            ),
            style="Hint.TLabel",
            wraplength=740,
        ).pack(fill=tk.X, pady=(7, 0))

    @staticmethod
    def _wrapping(label: ttk.Label) -> ttk.Label:
        """Una etiqueta que corta sus lineas al ancho que tenga, no a uno fijo."""
        label.bind(
            "<Configure>", lambda event: label.configure(wraplength=max(120, event.width - 4))
        )
        return label

    @staticmethod
    def _text_panel(parent: tk.Misc, *, font: tuple[str, int]) -> tk.Text:
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL)
        panel = tk.Text(
            frame, wrap=tk.WORD, font=font, state=tk.DISABLED, padx=10, pady=8, relief=tk.FLAT
        )
        panel.configure(yscrollcommand=scroll.set)
        scroll.configure(command=panel.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        return panel

    # ------------------------------------------------------------------ formulario

    def _choose_workspace(self) -> None:
        current = self.workspace.get().strip()
        selected = filedialog.askdirectory(
            title="Selecciona la carpeta del proyecto",
            initialdir=current if current and Path(current).is_dir() else str(Path.home()),
            mustexist=True,
        )
        if selected:
            self.workspace.set(str(Path(selected)))

    def _project_problem(self) -> str | None:
        text = self.workspace.get().strip()
        if not text:
            return "Elige la carpeta del proyecto con «Elegir…»."
        path = Path(text)
        if not path.is_absolute():
            return "Usa una ruta completa (por ejemplo, D:\\Proyectos\\mi-app)."
        if not path.is_dir():
            return "Esa carpeta no existe."
        return None

    def _refresh(self) -> None:
        """Lo que se puede hacer ahora, dicho en la propia ventana."""
        problem = self._project_problem()
        if problem is None:
            path = Path(self.workspace.get().strip()).resolve()
            parent = path.parent.name
            short = os.sep.join(("…", parent, path.name)) if parent else str(path)
            self.project_info.set(f"✔ {path.name}  ({short})")
            self.project_label.configure(style="Ok.TLabel")
        else:
            self.project_info.set(f"⚠ {problem}")
            self.project_label.configure(style="Bad.TLabel")
        writes = _MODE_LABELS.get(self.writes.get(), "off")
        execution = _MODE_LABELS.get(self.execution.get(), "off")
        self.writes_help.set(_WRITES_HELP[writes])
        self.execution_help.set(_EXEC_HELP[execution])
        documents = self.task_kind.get() == "documents"
        running = self._running()
        self.task_hint.set(
            next((hint for value, _, hint in _TASKS if value == self.task_kind.get()), "")
        )
        if documents:
            self.deliverables_row.pack(fill=tk.X, pady=(4, 0))
        else:
            self.deliverables_row.pack_forget()
        self.deliverables_entry.configure(
            state=tk.NORMAL if documents and not running else tk.DISABLED
        )
        objective = self.objective.get("1.0", tk.END).strip()
        ready = problem is None and bool(objective) and not running
        self.run_button.configure(state=tk.NORMAL if ready else tk.DISABLED)
        if running:
            return
        if problem is not None:
            self.status.set("Empieza eligiendo el proyecto")
        elif not objective:
            self.status.set("Describe el encargo")
        else:
            self.status.set("Lista para empezar")

    def _destination(self) -> tuple[str, str]:
        provider = _PROVIDER_LABELS[self.provider.get()]
        return provider.value, self.base_url.get().strip().rstrip("/")

    def _switch_credential(self) -> None:
        """La credencial del destino actual, nunca la del anterior (A03)."""
        current = self._destination()
        if current == self._token_key:
            return
        self._tokens[self._token_key] = self.token.get()
        provider = _PROVIDER_LABELS[self.provider.get()]
        self.token.set(self._tokens.get(current) or environment_token(provider))
        self._token_key = current
        self.connection_result.set("Sin probar")
        self.connection_label.configure(style="Hint.TLabel")

    def _provider_changed(self, *, initial: bool = False) -> None:
        provider = _PROVIDER_LABELS[self.provider.get()]
        if not initial:
            previous = ProviderKind(self._token_key[0])
            # Lo escrito para el anterior se queda con el anterior.
            self.settings.provider_urls[previous.value] = self.base_url.get().strip()
            self.settings.provider_models[previous.value] = self.model.get().strip()
            self.base_url.set(self.settings.url_for(provider))
            self.model.set(self.settings.model_for(provider))
            self._switch_credential()
        if provider is ProviderKind.AI_BROKER:
            self.provider_hint.set(
                "Token del broker (x-admin-token). También puedes definir ATHENA_BROKER_TOKEN. "
                "Se guarda solo en memoria y solo para esta URL."
            )
        else:
            self.provider_hint.set(
                "Clave del endpoint (Bearer). También puedes definir ATHENA_API_KEY. "
                "Se guarda solo en memoria y solo para esta URL."
            )

    def _settings_from_form(self) -> DesktopSettings:
        provider = _PROVIDER_LABELS[self.provider.get()]
        urls = dict(self.settings.provider_urls)
        models = dict(self.settings.provider_models)
        urls[provider.value] = self.base_url.get().strip()
        models[provider.value] = self.model.get().strip()
        try:
            iterations = int(self.max_iterations.get())
            timeout = float(self.timeout.get())
        except ValueError:
            raise ValueError("Iteraciones y tiempo máximo tienen que ser números") from None
        return DesktopSettings(
            provider=provider,
            base_url=self.base_url.get().strip(),
            model=self.model.get().strip(),
            workspace=self.workspace.get().strip(),
            writes=_MODE_LABELS[self.writes.get()],
            execution=_MODE_LABELS[self.execution.get()],
            max_iterations=iterations,
            timeout_seconds=timeout,
            task_kind=self.task_kind.get(),
            deliverables=self.deliverables.get().strip(),
            provider_urls=urls,
            provider_models=models,
        )

    def _configuration_from_form(self) -> RunConfiguration:
        self._switch_credential()
        settings = self._settings_from_form()
        problem = self._project_problem()
        if problem is not None:
            raise ValueError(problem)
        deliverables = tuple(
            item.strip() for item in settings.deliverables.replace("\n", ",").split(",")
        )
        kind: TaskKind = (
            "change"
            if settings.task_kind == "change"
            else "documents"
            if settings.task_kind == "documents"
            else "question"
        )
        return RunConfiguration(
            workspace=Path(settings.workspace).resolve(),
            objective=self.objective.get("1.0", tk.END).strip(),
            provider=settings.provider,
            base_url=settings.base_url,
            model=settings.model,
            token=self.token.get().strip(),
            writes=settings.writes,
            execution=settings.execution,
            max_iterations=settings.max_iterations,
            timeout_seconds=settings.timeout_seconds,
            task_kind=kind,
            deliverables=tuple(item for item in deliverables if item)
            if kind == "documents"
            else (),
        )

    # ------------------------------------------------------------------ probar conexion

    def _test_connection(self) -> None:
        self._switch_credential()
        try:
            settings = self._settings_from_form()
        except ValueError as exc:
            messagebox.showerror("No se puede probar", str(exc), parent=self.root)
            return
        configuration = RunConfiguration(
            workspace=Path.home(),
            objective="probar",
            provider=settings.provider,
            base_url=settings.base_url,
            model=settings.model or "prueba",
            token=self.token.get().strip(),
            timeout_seconds=30,
        )
        self.connection_result.set("Probando…")
        self.connection_label.configure(style="Hint.TLabel")
        self.test_button.configure(state=tk.DISABLED)

        def work() -> None:
            try:
                report = asyncio.run(check_connection(configuration))
            except Exception as exc:
                report = ConnectionReport(False, f"Error inesperado: {exc}")
            self.messages.put(("connection", report))

        threading.Thread(target=work, name="athena-desktop-connection", daemon=True).start()

    def _on_connection(self, payload: object) -> None:
        if isinstance(payload, ConnectionReport):
            self._show_connection(payload)

    def _complain(self, title: str, error: object) -> None:
        if not self.closing:
            messagebox.showerror(title, str(error), parent=self.root)

    def _show_connection(self, report: ConnectionReport) -> None:
        self.test_button.configure(state=tk.NORMAL if not self._running() else tk.DISABLED)
        self.connection_result.set(("✔ " if report.ok else "✖ ") + report.message)
        self.connection_label.configure(style="Ok.TLabel" if report.ok else "Bad.TLabel")

    # ------------------------------------------------------------------ ejecutar

    def _running(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def _start_from_keyboard(self, event: tk.Event[tk.Misc]) -> str:
        del event
        self._start()
        return "break"

    def _start(self, *, resume_run_id: str | None = None) -> None:
        if self._running():
            return
        try:
            configuration = self._configuration_from_form()
            if resume_run_id is None:
                confirmed = self._confirm_task_kind(configuration)
                if confirmed is None:
                    return
                configuration = confirmed
            configuration.validate()
            self.settings = self._settings_from_form()
            self.store.save(self.settings)
        except (ValueError, OSError) as exc:
            messagebox.showerror("No se puede iniciar", str(exc), parent=self.root)
            return
        self._begin(configuration, resume_run_id)

    def _confirm_task_kind(self, configuration: RunConfiguration) -> RunConfiguration | None:
        """Si el encargo parece pedir cambios y la tarea es una consulta, preguntarlo."""
        if configuration.task_kind != "question":
            return configuration
        if not requires_workspace_change(configuration.objective):
            return configuration
        answer = messagebox.askyesnocancel(
            "¿Consulta o cambio?",
            "Tu encargo parece pedir cambios en archivos, pero el tipo de tarea es "
            "«Responder una pregunta», que no cambia nada.\n\n"
            "¿Quieres cambiarlo a «Modificar el proyecto»?\n\n"
            "Sí: modificar el proyecto · No: solo responder · Cancelar: volver",
            parent=self.root,
        )
        if answer is None:
            return None
        if not answer:
            return configuration
        self.task_kind.set("change")
        if self.writes.get() == _MODE_NAMES["off"]:
            self.writes.set(_MODE_NAMES["ask"])
        return self._configuration_from_form()

    def _begin(self, configuration: RunConfiguration, resume_run_id: str | None) -> None:
        self.active = configuration
        self.handle = None
        self.last_run_id = resume_run_id
        self.model_calls = 0
        self.started_at = time.monotonic()
        self._clear_screen()
        self._append_activity(
            ActivityLine("Preparando el trabajo… (los trabajos anteriores siguen en «Historial»)")
        )
        self.p_objective.set(configuration.objective if resume_run_id is None else "(reanudado)")
        self.p_phase.set("Arrancando")
        self.p_calls.set("0")
        self.p_last.set("—")
        self.p_permission.set("Ninguno")
        self.p_config.set(
            f"{Path(configuration.workspace).name} · {self.provider.get()} "
            f"{configuration.model or '(modelo por defecto)'} · archivos: {self.writes.get()} "
            f"· ejecutar: {self.execution.get()}"
        )
        self.status.set("Athena está trabajando")
        self._set_config_enabled(False)
        self.run_button.configure(state=tk.DISABLED)
        self.cancel_button.configure(state=tk.NORMAL)
        self.undo_button.configure(state=tk.DISABLED)
        self.retry_button.configure(state=tk.DISABLED)
        if not self.progress_frame.winfo_ismapped():
            self.progress_frame.pack(fill=tk.X, pady=(0, 6), before=self._results_anchor)
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.start(12)
        _select_tab(self.results_notebook, 1)
        self.cancellation = CancellationSource()
        self.worker = threading.Thread(
            target=self._run_worker,
            args=(configuration, self.cancellation, resume_run_id),
            name="athena-desktop-run",
            daemon=True,
        )
        self.worker.start()
        self.root.after(1000, self._tick)

    def _run_worker(
        self,
        configuration: RunConfiguration,
        cancellation: CancellationSource,
        resume_run_id: str | None,
    ) -> None:
        try:
            result = asyncio.run(
                run_athena(
                    configuration,
                    cancellation,
                    on_event=lambda event: self.messages.put(("event", event)),
                    on_permission=self._request_permission,
                    state_dir=self.state_dir,
                    on_started=lambda handle: self.messages.put(("handle", handle)),
                    resume_run_id=resume_run_id,
                )
            )
        except BaseException as exc:
            self.messages.put(("error", exc))
        else:
            self.messages.put(("result", result))

    def _tick(self) -> None:
        if not self._running() or self.active is None:
            return
        elapsed = int(time.monotonic() - self.started_at)
        limit = int(self.active.timeout_seconds)
        self.p_elapsed.set(
            f"{elapsed // 60}:{elapsed % 60:02d} de un máximo de {limit // 60}:{limit % 60:02d}"
        )
        self.root.after(1000, self._tick)

    def _set_config_enabled(self, enabled: bool) -> None:
        for widget in self._config_widgets:
            # El estado de ttk: un combobox de solo lectura sigue siendolo al reactivarlo.
            with contextlib.suppress(tk.TclError):
                _set_ttk_state(widget, enabled=enabled)
        self.objective.configure(state=tk.NORMAL if enabled else tk.DISABLED)
        if enabled:
            self._refresh()

    def _cancel(self) -> None:
        if self.cancellation is None:
            return
        self.cancellation.cancel()
        if self.pending_question is not None:
            self.pending_question.withdrawn.set()
        self.status.set("Deteniendo…")
        self.p_phase.set("Deteniendo")
        self.cancel_button.configure(state=tk.DISABLED)
        self.revise_button.configure(state=tk.DISABLED)

    def _revise_goal(self) -> None:
        handle = self.handle
        if handle is None:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Cambiar el objetivo")
        dialog.transient(self.root)
        dialog.geometry("620x280")
        body = ttk.Frame(dialog, padding=12)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            body,
            text="Escribe el nuevo encargo. Athena lo aplicará en su siguiente paso; lo que "
            "ya haya hecho no se deshace solo.",
            wraplength=580,
        ).pack(anchor=tk.W)
        text = tk.Text(body, height=6, wrap=tk.WORD, font=("Segoe UI", 10))
        text.pack(fill=tk.BOTH, expand=True, pady=8)
        text.insert("1.0", self.p_objective.get())
        buttons = ttk.Frame(body)
        buttons.pack(fill=tk.X)

        def apply() -> None:
            new = text.get("1.0", tk.END).strip()
            dialog.destroy()
            if not new:
                return

            def work() -> None:
                try:
                    goal = handle.revise_goal(new)
                except Exception as exc:
                    self.messages.put(("goal_error", exc))
                else:
                    self.messages.put(("goal", (goal.revision, goal.text)))

            threading.Thread(target=work, name="athena-desktop-goal", daemon=True).start()

        ttk.Button(buttons, text="Cancelar", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Aplicar", command=apply, style="Run.TButton").pack(
            side=tk.RIGHT, padx=(0, 8)
        )
        dialog.grab_set()
        text.focus_set()

    def _request_permission(self, request: PermissionRequest) -> PermissionDecision:
        """Llamado desde el hilo del trabajo: espera a que la persona conteste."""
        question = _PermissionQuestion(request)
        self.messages.put(("permission", question))
        while not question.completed.wait(0.1):
            if self.closing or question.withdrawn.is_set():
                return PermissionDecision.DENY
        return question.decision

    # ------------------------------------------------------------------ mensajes

    def _drain_messages(self) -> None:
        handlers: dict[str, Callable[[object], None]] = {
            "event": self._on_event,
            "permission": self._on_permission,
            "result": self._on_result,
            "error": self._on_error,
            "handle": self._on_handle,
            "connection": self._on_connection,
            "goal": self._on_goal,
            "goal_error": lambda value: self._complain("No se pudo cambiar el objetivo", value),
            "history": self._on_history,
            "rollback": self._on_rollback,
            "rollback_error": lambda value: self._complain("No se pudo deshacer", value),
            "service_started": self._show_service_started,
            "service_stopped": self._show_service_stopped,
            "service_existing": self._show_existing_service,
            "service_error": self._show_service_error,
        }
        for _ in range(200):
            try:
                kind, payload = self.messages.get_nowait()
            except queue.Empty:
                break
            handler = handlers.get(kind)
            if handler is not None:
                handler(payload)
        if not self.closing:
            self.root.after(75, self._drain_messages)

    def _on_handle(self, payload: object) -> None:
        if isinstance(payload, RunHandle):
            self.handle = payload
            self.last_run_id = payload.run_id
            if self.active is not None and self.active.task_kind != "question":
                self.revise_button.configure(state=tk.NORMAL)

    def _on_event(self, payload: object) -> None:
        if not isinstance(payload, RuntimeEvent):
            return
        event = payload
        line = describe_event(event)
        if line is not None:
            self._append_activity(line)
            self.p_last.set(line.text)
        if self.show_technical.get():
            self._append_activity(ActivityLine(technical_line(event), "tech"))
        phases = {
            EventName.AGENT_STARTED: "Trabajando",
            EventName.MODEL_STARTED: "Esperando al modelo",
            EventName.TOOL_STARTED: "Usando herramientas",
            EventName.VERIFICATION_STARTED: "Comprobando el trabajo",
        }
        if event.name in phases:
            self.p_phase.set(phases[event.name])
        if event.name is EventName.MODEL_STARTED:
            self.model_calls += 1
            self.p_calls.set(str(self.model_calls))
        if event.name is EventName.GOAL_REVISED:
            text = event.payload.get("text") or event.payload.get("objective")
            if isinstance(text, str) and text:
                self.p_objective.set(text)

    def _on_permission(self, payload: object) -> None:
        if not isinstance(payload, _PermissionQuestion):
            return
        self.pending_question = payload
        view = present_approval(payload.request)
        self.p_permission.set(view.action)
        self.p_phase.set("Esperando tu permiso")
        self.status.set("Athena espera tu permiso")
        with contextlib.suppress(tk.TclError):
            self.root.bell()
        dialog = ApprovalDialog(self.root, payload)

        def settled() -> None:
            if payload.completed.is_set():
                self.pending_question = None
                self.p_permission.set("Ninguno")
                if self._running():
                    self.status.set("Athena está trabajando")
                return
            self.root.after(200, settled)

        del dialog
        self.root.after(200, settled)

    def _on_goal(self, payload: object) -> None:
        if isinstance(payload, tuple) and len(payload) == 2:
            revision, text = payload
            self.p_objective.set(str(text))
            self._append_activity(
                ActivityLine(
                    f"Objetivo cambiado (revisión {revision}); se aplica en el siguiente paso.",
                    "ok",
                )
            )

    def _on_result(self, payload: object) -> None:
        if not isinstance(payload, AgentRunResult):
            return
        kind = self.active.task_kind if self.active is not None else "change"
        view = present_result(payload, task_kind=kind)
        self._show_view(view)
        self.last_result_files = view.files
        self._finish_run()
        # Despues de reactivar el formulario, que si no pisa el titular con «Lista…».
        self.status.set(view.headline.split(" ", 1)[-1])
        self.p_phase.set(status_label(payload.status.value))
        self.undo_button.configure(state=tk.NORMAL if view.files else tk.DISABLED)
        self.retry_button.configure(state=tk.NORMAL)
        _select_tab(self.results_notebook, 0)
        self._refresh_history()

    def _on_error(self, payload: object) -> None:
        error = payload if isinstance(payload, BaseException) else RuntimeError(str(payload))
        self._write_answer(
            [
                ("✖ No se pudo empezar o terminar el trabajo\n\n", "head"),
                (f"{error}\n", "error"),
            ]
        )
        self._finish_run()
        self.status.set("Error")
        self.p_phase.set("Error")
        self.retry_button.configure(state=tk.NORMAL)
        _select_tab(self.results_notebook, 0)
        if not self.closing:
            messagebox.showerror("Athena", str(error), parent=self.root)

    def _show_view(self, view: ResultView) -> None:
        parts: list[tuple[str, str]] = [(view.headline + "\n", "head")]
        if view.explanation:
            parts.append((view.explanation + "\n", view.tone))
        if view.answer:
            parts += [("\nRespuesta de Athena\n", "section"), (view.answer + "\n", "")]
        if view.files:
            parts.append(("\nArchivos cambiados\n", "section"))
            parts += [(f"• {path}\n", "") for path in view.files]
        if view.checks:
            parts.append(("\nComprobaciones\n", "section"))
            parts += [(f"{line}\n", "") for line in view.checks]
        if view.next_steps:
            parts.append(("\nQué puedes hacer ahora\n", "section"))
            parts += [(f"• {step}\n", "") for step in view.next_steps]
        parts.append((f"\nDetalle técnico: {view.technical}\n", "muted"))
        self._write_answer(parts)

    def _finish_run(self) -> None:
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate", value=0)
        self.cancel_button.configure(state=tk.DISABLED)
        self.revise_button.configure(state=tk.DISABLED)
        self.cancellation = None
        self.handle = None
        if self.pending_question is not None:
            self.pending_question.withdrawn.set()
            self.pending_question = None
        self.p_permission.set("Ninguno")
        self.worker = None
        self._set_config_enabled(True)

    def _retry(self) -> None:
        if self.active is not None and not self.objective.get("1.0", tk.END).strip():
            self.objective.insert("1.0", self.active.objective)
        self._start()

    def _undo_last(self) -> None:
        if self.last_run_id is None:
            return
        self._confirm_undo(self.last_run_id, self.last_result_files)

    def _confirm_undo(self, run_id: str, files: tuple[str, ...]) -> None:
        listed = "\n".join(f"• {path}" for path in files[:15]) or "(los que escribió)"
        if not messagebox.askyesno(
            "Deshacer cambios",
            "Se devolverán a su estado anterior los archivos que escribió este trabajo:\n\n"
            f"{listed}\n\nSi alguno lo cambiaste tú después, se deja como está y te lo "
            "diré. ¿Continuar?",
            parent=self.root,
        ):
            return

        def work() -> None:
            try:
                result = asyncio.run(roll_back_run(run_id, self.state_dir))
            except Exception as exc:
                self.messages.put(("rollback_error", exc))
            else:
                self.messages.put(("rollback", result))

        threading.Thread(target=work, name="athena-desktop-undo", daemon=True).start()

    def _on_rollback(self, payload: object) -> None:
        if not isinstance(payload, RollbackResult):
            return
        lines = []
        if payload.restored:
            lines.append("Restaurados:\n" + "\n".join(f"• {p}" for p in payload.restored))
        if payload.conflicts:
            lines.append(
                "No se tocaron porque cambiaron después de que Athena los escribiera:\n"
                + "\n".join(f"• {p}" for p in payload.conflicts)
            )
        if payload.failed:
            lines.append(
                "No se pudieron restaurar (copia dañada o ausente):\n"
                + "\n".join(f"• {p}" for p in payload.failed)
            )
        if not lines:
            lines.append("No había nada que deshacer de este trabajo.")
        messagebox.showinfo("Deshacer cambios", "\n\n".join(lines), parent=self.root)
        self.undo_button.configure(state=tk.DISABLED)
        self._refresh_history()

    # ------------------------------------------------------------------ historial

    def _refresh_history(self) -> None:
        self.history_status.set("Cargando…")

        def work() -> None:
            try:
                runs = asyncio.run(list_runs(self.state_dir))
            except Exception as exc:
                self.messages.put(("history", exc))
            else:
                self.messages.put(("history", runs))

        threading.Thread(target=work, name="athena-desktop-history", daemon=True).start()

    def _on_history(self, payload: object) -> None:
        if isinstance(payload, BaseException):
            self.history_status.set(f"No se pudo leer el historial: {payload}")
            return
        if not isinstance(payload, list):
            return
        self.history = [item for item in payload if isinstance(item, RunSummary)]
        self.tree.delete(*self.tree.get_children())
        for summary in self.history:
            self.tree.insert(
                "",
                tk.END,
                iid=summary.run_id,
                values=(
                    summary.updated_at,
                    status_label(summary.status),
                    summary.label,
                    summary.objective.replace("\n", " ")[:200],
                ),
            )
        count = len(self.history)
        self.history_status.set(f"{count} trabajo(s) guardados")
        self._history_selected()

    def _selected(self) -> RunSummary | None:
        selection = self.tree.selection()
        if not selection:
            return None
        return next((item for item in self.history if item.run_id == selection[0]), None)

    def _history_selected(self) -> None:
        summary = self._selected()
        running = self._running()
        self.resume_button.configure(
            state=tk.NORMAL if summary and summary.resumable and not running else tk.DISABLED
        )
        self.history_undo_button.configure(
            state=tk.NORMAL if summary and summary.undoable and not running else tk.DISABLED
        )
        self.reuse_button.configure(state=tk.NORMAL if summary else tk.DISABLED)
        self.history_detail.configure(state=tk.NORMAL)
        self.history_detail.delete("1.0", tk.END)
        if summary is not None:
            kinds = {"question": "Pregunta", "change": "Modificación", "documents": "Documentos"}
            lines = [
                f"Objetivo: {summary.objective}",
                f"Estado: {status_label(summary.status)}",
                f"Tipo: {kinds.get(summary.task_kind, summary.task_kind)}",
                f"Proyecto: {summary.project}",
                f"Última actualización: {summary.updated_at}",
            ]
            if summary.verification:
                lines.append(f"Verificación: {summary.verification}")
            if summary.files_modified:
                lines.append("Archivos cambiados: " + ", ".join(summary.files_modified))
            if summary.undoable:
                lines.append(f"Cambios que se pueden deshacer: {summary.undoable}")
            if summary.errors:
                lines.append("Últimos problemas:\n  " + "\n  ".join(summary.errors))
            lines.append(f"Identificador: {summary.run_id}")
            self.history_detail.insert("1.0", "\n".join(lines))
        elif self.history:
            self.history_detail.insert(
                "1.0",
                "Selecciona un trabajo para ver su detalle. Desde aquí puedes reanudar uno "
                "interrumpido, deshacer los cambios que hizo o usarlo como nuevo encargo.",
            )
        else:
            self.history_detail.insert(
                "1.0", "Todavía no hay trabajos. Cada encargo que lances aparecerá aquí."
            )
        self.history_detail.configure(state=tk.DISABLED)

    def _resume_selected(self) -> None:
        summary = self._selected()
        if summary is None or not summary.resumable:
            return
        if not messagebox.askyesno(
            "Reanudar",
            f"Se reanudará este trabajo en {summary.project}, con los permisos y el "
            "presupuesto que le quedaban cuando se interrumpió, usando la conexión actual. "
            "¿Continuar?",
            parent=self.root,
        ):
            return
        self.workspace.set(summary.project)
        self.objective.configure(state=tk.NORMAL)
        self.objective.delete("1.0", tk.END)
        self.objective.insert("1.0", summary.objective)
        _select_tab(self.tabs, 0)
        self._start(resume_run_id=summary.run_id)

    def _undo_selected(self) -> None:
        summary = self._selected()
        if summary is not None and summary.undoable:
            self._confirm_undo(summary.run_id, summary.files_modified)

    def _reuse_selected(self) -> None:
        summary = self._selected()
        if summary is None or self._running():
            return
        self.workspace.set(summary.project)
        if summary.task_kind in ("question", "change", "documents"):
            self.task_kind.set(summary.task_kind)
        self.objective.configure(state=tk.NORMAL)
        self.objective.delete("1.0", tk.END)
        self.objective.insert("1.0", summary.objective)
        _select_tab(self.tabs, 0)
        self._refresh()

    # ------------------------------------------------------------------ servicio

    def _start_service(self) -> None:
        if self.service_worker is not None and self.service_worker.is_alive():
            return
        if self.managed_service is not None and self.managed_service.process.poll() is None:
            return
        try:
            self._switch_credential()
            settings = self._settings_from_form()
            if settings.provider is not ProviderKind.AI_BROKER:
                raise ValueError(
                    "El servicio usa AI_Broker: elige AI_Broker como proveedor a la izquierda"
                )
            request = ManagedServiceRequest(
                broker_base_url=settings.base_url,
                broker_token=self.token.get().strip(),
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

    def _watch_service(self) -> None:
        """Mirar si el servicio sigue vivo, no suponerlo (A15)."""
        service = self.managed_service
        if service is not None and service.state is ServiceState.FAILED:
            code = service.process.returncode
            self.managed_service = None
            self.service_url.set("")
            self.service_token.set("")
            self.service_status.set(f"El servicio se detuvo inesperadamente (código {code})")
            self.service_start_button.configure(state=tk.NORMAL)
            self.service_stop_button.configure(state=tk.DISABLED)
            self.copy_service_token_button.configure(state=tk.DISABLED)
        if not self.closing:
            self.root.after(2000, self._watch_service)

    def _copy_service_token(self) -> None:
        token = self.service_token.get()
        if not token:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(token)
        self.root.update_idletasks()
        self.service_status.set("Token copiado al portapapeles")

    def _show_service_started(self, payload: object) -> None:
        if not isinstance(payload, ManagedAthenaService):
            return
        if self.closing:
            # Se cerro la ventana mientras arrancaba: no se deja un servicio sin dueno.
            payload.stop()
            return
        self.managed_service = payload
        self.service_url.set(payload.endpoint.base_url)
        self.service_token.set(payload.endpoint.token)
        self.service_status.set("Servicio disponible")
        self.service_start_button.configure(state=tk.DISABLED)
        self.service_stop_button.configure(state=tk.NORMAL)
        self.copy_service_token_button.configure(state=tk.NORMAL)

    def _show_service_stopped(self, payload: object) -> None:
        if self.managed_service is payload:
            self.managed_service = None
        self.service_url.set("")
        self.service_token.set("")
        self.service_status.set("Servicio detenido")
        self.service_start_button.configure(state=tk.NORMAL)
        self.service_stop_button.configure(state=tk.DISABLED)
        self.copy_service_token_button.configure(state=tk.DISABLED)

    def _show_existing_service(self, payload: object) -> None:
        if not isinstance(payload, ServiceAlreadyRunning):
            return
        self.managed_service = None
        self.service_url.set(payload.base_url)
        self.service_token.set("")
        self.service_status.set("Servicio iniciado por otra aplicación")
        self.service_start_button.configure(state=tk.NORMAL)
        self.service_stop_button.configure(state=tk.DISABLED)
        self.copy_service_token_button.configure(state=tk.DISABLED)
        messagebox.showinfo(
            "Servicio de Athena ya iniciado",
            (
                f"Athena ya está funcionando en {payload.base_url}.\n\n"
                "Esta ventana no la inició y por seguridad no puede recuperar su token ni "
                "detenerla. La aplicación que inició el servicio —por ejemplo ChatyGPT— "
                "debe conservar el token anunciado por Athena."
            ),
            parent=self.root,
        )

    def _show_service_error(self, payload: object) -> None:
        self.service_status.set("Error del servicio")
        self.service_start_button.configure(state=tk.NORMAL)
        self.service_stop_button.configure(state=tk.DISABLED)
        self.copy_service_token_button.configure(state=tk.DISABLED)
        if not self.closing:
            messagebox.showerror("Servicio de Athena", str(payload), parent=self.root)

    # ------------------------------------------------------------------ texto

    def _append_activity(self, line: ActivityLine) -> None:
        panel = self.activity
        at_bottom = panel.yview()[1] >= 0.999
        timestamp = time.strftime("%H:%M:%S")
        panel.configure(state=tk.NORMAL)
        text = line.text if line.tone == "tech" else f"{timestamp}  {line.text}"
        panel.insert(tk.END, text + "\n", line.tone if line.tone != "info" else "")
        excess = int(panel.index("end-1c").split(".")[0]) - _MAX_ACTIVITY_LINES
        if excess > 0:
            panel.delete("1.0", f"{excess + 1}.0")
        if at_bottom:
            # Solo si ya se estaba mirando el final: quien lee lo de arriba no salta.
            panel.see(tk.END)
        panel.configure(state=tk.DISABLED)

    def _write_answer(self, parts: list[tuple[str, str]]) -> None:
        self.answer.configure(state=tk.NORMAL)
        self.answer.delete("1.0", tk.END)
        for text, tag in parts:
            self.answer.insert(tk.END, text, tag or ())
        self.answer.see("1.0")
        self.answer.configure(state=tk.DISABLED)

    def _clear_screen(self) -> None:
        """Solo la pantalla: el historial guardado no se toca."""
        for panel in (self.answer, self.activity):
            panel.configure(state=tk.NORMAL)
            panel.delete("1.0", tk.END)
            panel.configure(state=tk.DISABLED)

    # ------------------------------------------------------------------ cierre

    def _close(self) -> None:
        """Cerrar sin dejar procesos ni trabajos sin dueño (A15)."""
        if self.closing:
            return
        if self._running() and not messagebox.askyesno(
            "Cerrar Athena",
            "Hay un trabajo en marcha. Si cierras, se detendrá y quedará guardado en "
            "«Historial». ¿Cerrar?",
            parent=self.root,
        ):
            return
        self.closing = True
        self.status.set("Cerrando…")
        if self.cancellation is not None:
            self.cancellation.cancel()
        if self.pending_question is not None:
            self.pending_question.withdrawn.set()
        deadline = time.monotonic() + 15.0
        self._finish_close(deadline)

    def _finish_close(self, deadline: float) -> None:
        busy = self._running() or (
            self.service_worker is not None and self.service_worker.is_alive()
        )
        if busy and time.monotonic() < deadline:
            self.root.after(100, lambda: self._finish_close(deadline))
            return
        if self.managed_service is not None:
            # Al cerrar se hace lo que se puede: un fallo aqui no debe impedir salir.
            with contextlib.suppress(Exception):
                self.managed_service.stop(timeout_seconds=5.0)
            self.managed_service = None
        self.root.destroy()


def _set_ttk_state(widget: ttk.Widget, *, enabled: bool) -> None:
    # `Widget.state` tampoco tiene tipos en typeshed.
    widget.state(["!disabled"] if enabled else ["disabled"])  # type: ignore[no-untyped-call]


def _select_tab(notebook: ttk.Notebook, index: int) -> None:
    # `Notebook.select` no tiene tipos en typeshed; un solo sitio en vez de cinco.
    notebook.select(index)  # type: ignore[no-untyped-call]


def main() -> int:
    if sys.platform == "win32":
        with contextlib.suppress(AttributeError, OSError):
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
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


__all__ = ["ApprovalDialog", "AthenaDesktopApp", "main"]
