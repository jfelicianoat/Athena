"""Lo que la ventana ensena, en palabras de persona y sin nada de Tk.

La actividad salia como JSON con nombres internos de eventos, y los estados finales
mezclaban espanol con enums en ingles (A27). Aqui se traduce todo eso; los detalles
tecnicos siguen disponibles, pero plegados.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from athena.agent_loop import AgentRunResult, AgentRunStatus
from athena.events import EventName, RuntimeEvent
from athena.permissions import PermissionRequest, RiskLevel
from athena.types import JSONObject
from athena.verification import VerificationStatus

_RISK = {
    RiskLevel.LOW: "bajo",
    RiskLevel.MEDIUM: "medio",
    RiskLevel.HIGH: "alto",
    RiskLevel.CRITICAL: "crítico",
}

_TOOLS = {
    "glob": "buscar archivos",
    "grep": "buscar texto",
    "read_file": "leer un archivo",
    "read_range": "leer parte de un archivo",
    "list_directory": "listar una carpeta",
    "write_file": "escribir un archivo",
    "edit_file": "editar un archivo",
    "bash": "ejecutar un comando",
    "git_status": "ver el estado de git",
    "git_diff": "ver los cambios de git",
    "git_log": "ver el historial de git",
    "git_show": "ver un commit",
    "git_commit": "hacer un commit",
    "delegate_task": "pedir ayuda a un especialista",
    "verification": "comprobar el trabajo",
    "tool_search": "buscar herramientas",
}

_INCONCLUSIVE = {
    "execution_not_authorized": (
        "No se ejecutaron las comprobaciones del proyecto porque «Ejecutar código del "
        "proyecto» está desactivado o no lo autorizaste."
    ),
    "no_checks_defined": "El proyecto no define comprobaciones que Athena pueda ejecutar.",
    "partial_verification": "Algunas comprobaciones ya fallaban antes y siguen fallando.",
    "dependency_missing": "Falta una dependencia en el entorno del proyecto.",
    "environment_incomplete": "El entorno del proyecto no está completo.",
    "tool_unavailable": "La comprobación no se pudo ejecutar.",
    "external_service_unavailable": "Un servicio externo no estaba disponible.",
    "ambiguous_result": "El resultado de la comprobación no es concluyente.",
}

_ERRORS = {
    "budget_exceeded": "Se agotaron las iteraciones permitidas.",
    "process_timeout": "Se agotó el tiempo máximo del trabajo.",
    "no_progress": "El modelo repetía lo mismo sin avanzar.",
    "verification_failure": "El trabajo no pasó las comprobaciones.",
    "verification_inconclusive": "No se pudo comprobar el trabajo.",
    "model_transient_error": "El proveedor del modelo no respondió.",
    "model_permanent_error": "El proveedor del modelo rechazó la petición.",
    "model_authentication_required": "AI_Broker necesita una credencial válida para continuar.",
    "model_authentication_backend_unavailable": (
        "El almacén de credenciales de AI_Broker no está disponible. "
        "Cambiar el token no lo resuelve."
    ),
    "approval_abandoned": "Nadie contestó a las peticiones de permiso.",
    "cancelled": "Detenido.",
}


def tool_label(name: str) -> str:
    return _TOOLS.get(name, name)


def tool_label_for_dialog(name: str) -> str:
    """Nombre legible de una herramienta, con mayuscula inicial para un dialogo."""
    return tool_label(name).capitalize()


@dataclass(frozen=True, slots=True)
class ActivityLine:
    text: str
    #: `info`, `ok`, `warn` o `error`: como se pinta la linea.
    tone: str = "info"


def describe_event(event: RuntimeEvent) -> ActivityLine | None:
    """Una linea legible por evento, o None si no aporta nada a una persona."""
    name = event.name
    payload = event.payload
    tool = str(payload.get("tool_name") or "")
    if name is EventName.AGENT_STARTED:
        verb = "Se reanuda" if payload.get("resumed") else "Empieza"
        return ActivityLine(f"{verb} el trabajo.")
    if name is EventName.MODEL_STARTED:
        attempt = payload.get("attempt")
        suffix = f" (intento {attempt})" if isinstance(attempt, int) and attempt > 1 else ""
        return ActivityLine(f"Consultando al modelo{suffix}…")
    if name is EventName.MODEL_COMPLETED:
        calls = payload.get("tool_call_count")
        model = payload.get("model")
        quien = f" ({model})" if model else ""
        if isinstance(calls, int) and calls:
            return ActivityLine(f"El modelo{quien} pide usar {calls} herramienta(s).")
        return ActivityLine(f"El modelo{quien} ha respondido.")
    if name is EventName.MODEL_FAILED:
        again = " Se reintenta." if payload.get("retrying") else ""
        return ActivityLine(f"El modelo no respondió bien.{again}", "warn")
    if name is EventName.PERMISSION_REQUESTED and payload.get("awaiting_decision"):
        return ActivityLine(f"Pide permiso para {tool_label(tool)}.", "warn")
    if name is EventName.PERMISSION_RESOLVED:
        decision = payload.get("decision")
        if decision == "allow":
            return ActivityLine(f"Permiso concedido para {tool_label(tool)}.", "ok")
        return ActivityLine(f"Permiso denegado para {tool_label(tool)}.", "warn")
    if name is EventName.TOOL_STARTED:
        return ActivityLine(f"Usando la herramienta: {tool_label(tool)}.")
    if name is EventName.TOOL_FAILED:
        message = payload.get("message") or payload.get("error_code") or ""
        return ActivityLine(f"Falló «{tool_label(tool)}»: {message}", "warn")
    if name is EventName.FILE_CHANGED:
        path = payload.get("path") or payload.get("relative_path") or ""
        return ActivityLine(f"Archivo cambiado: {path}", "ok")
    if name is EventName.VERIFICATION_STARTED:
        return ActivityLine("Comprobando el trabajo…")
    if name is EventName.VERIFICATION_CHECK_STARTED:
        return ActivityLine(f"Ejecutando la comprobación: {payload.get('command', '')}")
    if name is EventName.VERIFICATION_CHECK_COMPLETED:
        passed = payload.get("passed")
        return ActivityLine(
            f"Comprobación «{payload.get('check', '')}»: {'pasa' if passed else 'falla'}.",
            "ok" if passed else "warn",
        )
    if name is EventName.VERIFICATION_COMPLETED:
        status = payload.get("status")
        return ActivityLine(
            f"Verificación: {status_word(str(status))}.", "ok" if status == "passed" else "warn"
        )
    if name is EventName.RECOVERY_ACTION:
        action = payload.get("action")
        if action == "require_workspace_change":
            return ActivityLine(
                "El modelo quiso terminar sin cambiar ningún archivo; se le pide que lo haga.",
                "warn",
            )
        if action == "no_progress":
            return ActivityLine(
                "El modelo se está repitiendo; se le pide cambiar de enfoque.", "warn"
            )
        return ActivityLine("Athena intenta corregir un problema y seguir.", "warn")
    if name is EventName.GOAL_REVISED:
        return ActivityLine("El objetivo ha cambiado; se aplica desde este paso.", "ok")
    if name is EventName.AGENT_COMPLETED:
        return ActivityLine("Trabajo terminado.", "ok")
    if name is EventName.AGENT_FAILED:
        return ActivityLine("El trabajo no se ha podido completar.", "error")
    if name is EventName.AGENT_CANCELLED:
        return ActivityLine("Trabajo detenido.", "warn")
    return None


def technical_line(event: RuntimeEvent) -> str:
    details = json.dumps(event.payload, ensure_ascii=False, default=str)
    return f"    {event.name.value} {details}"


def status_word(status: str) -> str:
    return {
        "passed": "comprobado",
        "failed": "no pasa",
        "inconclusive": "sin comprobar",
    }.get(status, status)


@dataclass(frozen=True, slots=True)
class ResultView:
    """El resultado de un trabajo, listo para ensenar."""

    headline: str
    tone: str
    answer: str
    explanation: str
    files: tuple[str, ...]
    checks: tuple[str, ...]
    next_steps: tuple[str, ...]
    technical: str


def present_result(result: AgentRunResult, *, task_kind: str) -> ResultView:
    verification = result.verification
    files = tuple(result.working_state.files_modified) if result.working_state else ()
    if not files:
        raw = result.session.attributes.get("files_modified")
        if isinstance(raw, list):
            files = tuple(str(item) for item in raw)
    checks = tuple(
        _check_line(item.kind, item.summary, item.reference, item.metadata)
        for item in (verification.evidence if verification else ())
        if item.kind not in ("plan", "authorization")
    )
    reason = ""
    code = ""
    if result.error is not None:
        code = result.error.code
        details = result.error.details if isinstance(result.error.details, dict) else {}
        reason = str(details.get("reason") or "")
    technical = (
        f"estado={result.status.value}; verificación="
        f"{verification.status.value if verification else '-'}; error={code or '-'}"
    )
    if verification is not None:
        technical += f"\n{verification.summary}"
    if result.error is not None:
        technical += f"\n{result.error.message}"

    if result.status is AgentRunStatus.COMPLETED:
        if task_kind == "question":
            return ResultView(
                "✔ Respuesta dada — no se ha cambiado ningún archivo",
                "ok",
                (result.answer or "").strip(),
                "Esto no demuestra que la respuesta sea correcta: Athena solo comprueba que "
                "respondió sin tocar el proyecto.",
                files,
                checks,
                ("Si querías cambios, elige «Modificar el proyecto» y vuelve a pedirlo.",),
                technical,
            )
        headline = "✔ Terminado y comprobado"
        explanation = verification.summary if verification else ""
        return ResultView(
            headline,
            "ok",
            (result.answer or "").strip(),
            _plain_verification(explanation),
            files,
            checks,
            (
                ("Revisa los archivos cambiados. Si no te convence, pulsa «Deshacer cambios».",)
                if files
                else ()
            ),
            technical,
        )
    if result.status is AgentRunStatus.CANCELLED:
        return ResultView(
            "■ Detenido",
            "warn",
            (result.answer or "").strip(),
            "Se alcanzó el tiempo máximo del trabajo."
            if reason == "timed_out"
            else "Lo detuviste tú; lo que llegó a hacer queda en «Historial».",
            files,
            checks,
            (("Puedes deshacer lo que llegó a cambiar.",) if files else ()),
            technical,
        )
    if reason in _INCONCLUSIVE or (
        verification is not None and verification.status is VerificationStatus.INCONCLUSIVE
    ):
        steps = []
        if reason == "execution_not_authorized":
            steps.append(
                "Activa «Ejecutar código del proyecto» (Preguntar o Permitir) para que "
                "Athena pueda comprobar el trabajo."
            )
        if files:
            steps.append("Revisa tú los cambios, o deshazlos con «Deshacer cambios».")
        return ResultView(
            "⚠ Terminado sin comprobar",
            "warn",
            (result.answer or "").strip(),
            _INCONCLUSIVE.get(reason, "No se pudo comprobar que el trabajo esté bien."),
            files,
            checks,
            tuple(steps),
            technical,
        )
    if code in {"model_authentication_required", "model_authentication_backend_unavailable"}:
        renewal = (
            "Renueva el token de AI_Broker y pulsa «Probar conexión»."
            if code == "model_authentication_required"
            else "Restablece el almacén de credenciales del broker y pulsa «Probar conexión»."
        )
        explanation = _ERRORS[code]
        if result.error is not None and result.error.details.get("task_preserved") is True:
            explanation += " La tarea enviada al broker se ha conservado."
            technical += f"\nbroker_task={result.error.details.get('task', '')}"
        return ResultView(
            "⚠ Interrumpido por la conexión con AI_Broker",
            "warn",
            (result.answer or "").strip(),
            explanation,
            files,
            checks,
            (renewal,),
            technical,
        )
    message = _ERRORS.get(code, "")
    if not message and result.error is not None:
        message = result.error.message
    steps = ["Revisa la pestaña «Actividad» para ver en qué paso se quedó."]
    if code == "budget_exceeded":
        steps.insert(0, "Sube las iteraciones o el tiempo máximo y usa «Reintentar».")
    if code == "process_timeout":
        steps.insert(0, "Sube el tiempo máximo: un modelo local puede tardar minutos por turno.")
    if files:
        steps.append("Deshaz los cambios a medias con «Deshacer cambios» si no te sirven.")
    return ResultView(
        "✖ No se ha podido completar",
        "error",
        (result.answer or "").strip(),
        message,
        files,
        checks,
        tuple(steps),
        technical,
    )


_ATTRIBUTION = {
    "pre_existing": "ya fallaba antes de empezar",
    "introduced": "lo ha roto este cambio",
    "unattributed": "no se pudo comparar con el estado inicial",
}


def _check_line(kind: str, summary: str, reference: str | None, metadata: JSONObject) -> str:
    """Una comprobacion en una linea legible: que se ejecuto y como salio."""
    passed = bool(metadata.get("passed"))
    mark = "✔" if passed else "✖"
    if kind == "artifact":
        name = metadata.get("name") or reference or ""
        return f"{mark} {name}: {'producido' if passed else 'no producido'}"
    if kind == "answer":
        return "✔ Respondió sin cambiar ningún archivo" if passed else f"✖ {summary}"
    if kind == "integrity":
        return f"✖ Integridad: {summary}"
    command = metadata.get("command") or reference or metadata.get("name") or summary
    if passed:
        return f"✔ {command}: pasa"
    reason = _ATTRIBUTION.get(str(metadata.get("attribution")), "")
    return f"✖ {command}: falla" + (f" ({reason})" if reason else "")


def _plain_verification(summary: str) -> str:
    if summary.startswith("All project checks pass"):
        return "Todas las comprobaciones del proyecto pasan."
    if summary.startswith("This change broke no check"):
        return (
            "El cambio no rompió ninguna comprobación, aunque algunas ya fallaban antes. "
            "Eso no demuestra por sí solo que el encargo esté resuelto."
        )
    if "deliverable(s) produced" in summary:
        return (
            "Los entregables existen, no están vacíos y los escribió este trabajo. "
            "Eso no demuestra que su contenido sea correcto."
        )
    return summary


# ------------------------------------------------------------------ aprobaciones

#: Los motivos y efectos que dan las herramientas, en ingles porque los lee tambien
#: ChatyGPT, traducidos para la ventana. Lo que no se reconoce se muestra tal cual.
_FIXED_TEXTS = {
    "The agent requested a full-content write inside the workspace.": (
        "El agente quiere escribir el archivo completo dentro del proyecto."
    ),
    "The agent requested a literal in-place replacement inside the workspace.": (
        "El agente quiere sustituir un fragmento concreto de un archivo del proyecto."
    ),
    "The agent requested read-only git history or working-tree state.": (
        "El agente quiere consultar el historial o el estado de git, sin cambiar nada."
    ),
    "The agent requested read-only access inside the workspace.": (
        "El agente quiere leer contenido del proyecto, sin cambiar nada."
    ),
    "Recording a commit changes history that Athena cannot undo on its own.": (
        "Un commit cambia el historial de git y Athena no puede deshacerlo por su cuenta."
    ),
    "Leaves every other file untouched": "No toca ningún otro archivo",
    "Reads local state without writing": "Solo lee, no escribe nada",
    "Reads local git state": "Lee el estado de git",
    "Reads workspace content": "Lee contenido del proyecto",
    "Changes nothing": "No cambia nada",
    "Executes code the project defines (tests, build files, plugins)": (
        "Ejecuta código que define el proyecto (tests, ficheros de build, plugins)"
    ),
    "May write caches or build artefacts inside the workspace": (
        "Puede escribir cachés o artefactos dentro del proyecto"
    ),
    "May change or delete files or repository state": (
        "Puede cambiar o borrar archivos o el estado del repositorio"
    ),
    "May install dependencies, migrate data, or record a commit": (
        "Puede instalar dependencias, migrar datos o registrar un commit"
    ),
    "Executes arbitrary code from the project": "Ejecuta código arbitrario del proyecto",
    "Discards more than half of the current file content": (
        "Descarta más de la mitad del contenido actual del archivo"
    ),
    "Writes a new entry into local git history": "Añade una entrada al historial local de git",
    "Does not push, merge or publish anything": "No publica, fusiona ni sube nada",
    "Refused before execution": "Rechazado antes de ejecutarse",
}

_PATTERNS = (
    (re.compile(r"^Modifies (.+) in place$"), "Modifica {0}"),
    (re.compile(r"^Replaces (.+) in the workspace$"), "Sustituye {0} en el proyecto"),
    (re.compile(r"^Creates (.+) in the workspace$"), "Crea {0} en el proyecto"),
    (re.compile(r"^File size (\d+) -> (\d+) characters$"), "Tamaño: {0} → {1} caracteres"),
    (
        re.compile(r"^Runs the project script (.+) in (.+)$"),
        "Ejecuta el script del proyecto {0} en {1}",
    ),
    (re.compile(r"^Runs (.+) in (.+)$"), "Ejecuta {0} en {1}"),
    (re.compile(r"^(.+) builds or verifies locally$"), "{0} compila o comprueba en local"),
    (re.compile(r"^(.+) only inspects local state$"), "{0} solo consulta el estado local"),
    (re.compile(r"^Stages and commits: (.+)$"), "Prepara y registra en git: {0}"),
)


def spanish(text: str) -> str:
    """Un texto de herramienta en espanol, si se sabe decir; si no, el original."""
    fixed = _FIXED_TEXTS.get(text)
    if fixed is not None:
        return fixed
    for pattern, template in _PATTERNS:
        match = pattern.match(text)
        if match:
            parts = [_place(item) for item in match.groups()]
            return template.format(*parts)
    return text


def _place(value: str) -> str:
    return "la carpeta del proyecto" if value.strip() in (".", "./") else value


@dataclass(frozen=True, slots=True)
class ApprovalView:
    title: str
    action: str
    project: str
    risk: str
    reason: str
    effects: tuple[str, ...]
    preview_title: str
    preview: str
    #: Cuanto dura lo que se concede, dicho sin letra pequeña.
    scope: str = "El permiso vale solo para esta acción. Athena volverá a preguntar la próxima."


def present_approval(request: PermissionRequest) -> ApprovalView:
    """Todo lo que hace falta para decidir, incluido que se va a cambiar (A18)."""
    root = request.workspace.root
    arguments = request.arguments
    preview_title = ""
    preview = ""
    action = request.action or request.operation
    if request.tool_name == "write_file":
        path = str(arguments.get("path", ""))
        content = str(arguments.get("content", ""))
        action = f"Escribir el archivo {path}"
        preview_title, preview = _write_preview(root, path, content)
    elif request.tool_name == "edit_file":
        path = str(arguments.get("path", ""))
        action = f"Editar el archivo {path}"
        preview_title, preview = _edit_preview(
            root, path, str(arguments.get("old_string", "")), str(arguments.get("new_string", ""))
        )
    elif request.tool_name == "bash":
        command = str(arguments.get("command", ""))
        action = f"Ejecutar un comando en {_place(str(arguments.get('cwd', '.')))}"
        preview_title, preview = "Comando", f"$ {command}"
    elif request.tool_name == "verification":
        commands = arguments.get("commands")
        listed = commands if isinstance(commands, list) else []
        action = "Ejecutar las comprobaciones del proyecto"
        preview_title = "Comandos que se ejecutarán (los declara el propio proyecto)"
        preview = "\n".join(f"$ {item}" for item in listed)
    elif request.tool_name == "git_commit":
        action = "Registrar un commit en git"
        preview_title = "Mensaje y archivos"
        preview = json.dumps(arguments, ensure_ascii=False, indent=2, default=str)
    effects = tuple(spanish(item) for item in request.possible_effects if not item.startswith("$ "))
    scope = (
        "Este permiso cubre estos comandos durante este trabajo: antes de los cambios, para "
        "saber qué fallaba ya, y al terminar, para comprobar el resultado."
        if request.tool_name == "verification"
        else "El permiso vale solo para esta acción. Athena volverá a preguntar la próxima."
    )
    return ApprovalView(
        title=f"Athena necesita permiso para {tool_label(request.tool_name)}",
        action=action,
        project=str(root),
        risk=_RISK.get(request.risk, request.risk.value),
        reason=spanish(request.reason) if request.reason else "No se indicó.",
        effects=effects,
        preview_title=preview_title,
        preview=preview,
        scope=scope,
    )


_PREVIEW_LINES = 400


def _write_preview(root: Path, relative: str, content: str) -> tuple[str, str]:
    target = root / relative
    try:
        before = target.read_text(encoding="utf-8") if target.is_file() else None
    except (OSError, UnicodeError):
        before = None
    if before is None:
        lines = content.splitlines()
        shown = "\n".join(lines[:_PREVIEW_LINES])
        more = (
            f"\n… ({len(lines) - _PREVIEW_LINES} líneas más)" if len(lines) > _PREVIEW_LINES else ""
        )
        return f"Archivo nuevo ({len(lines)} líneas)", shown + more
    return "Cambios respecto al archivo actual", _diff(before, content, relative)


def _edit_preview(root: Path, relative: str, old: str, new: str) -> tuple[str, str]:
    target = root / relative
    try:
        before = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return "Sustitución", f"- {old}\n+ {new}"
    return "Cambios respecto al archivo actual", _diff(before, before.replace(old, new), relative)


def _diff(before: str, after: str, name: str) -> str:
    lines = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"{name} (ahora)",
            tofile=f"{name} (después)",
            lineterm="",
        )
    )
    if not lines:
        return "(el contenido no cambia)"
    shown = "\n".join(lines[:_PREVIEW_LINES])
    if len(lines) > _PREVIEW_LINES:
        shown += f"\n… ({len(lines) - _PREVIEW_LINES} líneas más de diferencia)"
    return shown


def status_label(status: str) -> str:
    return {
        "completed": "Completado",
        "failed": "No completado",
        "cancelled": "Detenido",
        "running": "En curso",
        "verifying": "Comprobando",
        "waiting_permission": "Esperando permiso",
        "recovery_pending": "Interrumpido (se puede reanudar)",
    }.get(status, status)


__all__ = [
    "ActivityLine",
    "ApprovalView",
    "ResultView",
    "describe_event",
    "present_approval",
    "present_result",
    "status_label",
    "status_word",
    "technical_line",
    "tool_label",
]
