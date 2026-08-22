"""The tool through which an agent asks for a task to be delegated.

Until now a subagent could only be started by Athena's own code. This is how the model
asks — and it asks for a *task*, not for an agent. The distinction is the whole naming
decision: `spawn_agent` would invite the model to think about infrastructure, and
infrastructure is not its business. It states a goal, what would count as done, and which
specialism it thinks fits; the runtime decides everything about how that happens.

Two rules make this safe to expose, and both are enforced rather than requested.

**A child's authority is a subset of its parent's.** Not "usually", not "by convention" —
`narrow` computes the intersection and there is no path that widens it. A parent that
cannot write cannot delegate a task that can, whatever role the model asked for.

**Delegation itself goes through the PermissionEngine.** The risk is not that a subagent
exists; it is what the subagent may do. Asking for a read-only explorer is an R0 question.
Asking for a coder that can run commands is not, and the person is asked.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

from athena.cancellation import CancellationToken
from athena.errors import ToolValidationError
from athena.permissions import PermissionPolicy, PermissionRequest, RiskLevel, RiskTier
from athena.subagent_provider import Continuable, Delegator
from athena.subagents import (
    DEFAULT_PROFILES,
    SubagentBrief,
    SubagentProfile,
    SubagentResult,
    SubagentRole,
)
from athena.tool_projection import DisplayView, ModelView, ResultKind, ToolProjection
from athena.tools import Tool, ToolContext, ToolLoadPolicy, ToolResult, ToolSpec
from athena.types import JSONObject, JSONSchema
from athena.workspace import Workspace

DELEGATE_TASK_NAME = "delegate_task"


def narrow(parent: PermissionPolicy, child: PermissionPolicy) -> PermissionPolicy:
    """The authority a child may actually have: the intersection, never the union.

    Written as arithmetic rather than as a check that raises, because the safe answer
    always exists — a child asked for more than its parent has simply gets less. Raising
    would make the caller decide what to do about it, and there is only one right answer.
    """
    return PermissionPolicy(
        allow_workspace_writes=parent.allow_workspace_writes and child.allow_workspace_writes,
        allow_local_execution=parent.allow_local_execution and child.allow_local_execution,
    )


def confine(
    profile: SubagentProfile,
    parent_policy: PermissionPolicy,
    available_tools: frozenset[str],
) -> SubagentProfile:
    """Fit a profile inside its parent's authority and inside what actually exists.

    Both halves matter. The policy narrowing stops a delegate from doing more than its
    parent could; the toolset intersection stops it from being handed a name the parent's
    own registry never had — which is how a delegate would otherwise reach a tool the
    parent was deliberately not given.
    """
    permitted = tuple(name for name in profile.toolsets if name in available_tools)
    if not permitted:
        raise ToolValidationError(
            f"A {profile.role.value} would have no tools it is allowed to use",
            details={"role": profile.role.value},
        )
    return replace(profile, policy=narrow(parent_policy, profile.policy), toolsets=permitted)


@dataclass(frozen=True, slots=True)
class DelegationRequest:
    """What the model asked for, after the runtime has read it properly."""

    goal: str
    role: SubagentRole
    expected_output: str = ""
    acceptance_criteria: tuple[str, ...] = ()
    context_refs: tuple[str, ...] = ()
    #: A quien se le vuelve a preguntar, si esto es un seguimiento y no un encargo nuevo.
    follow_up_to: str = ""

    @property
    def is_follow_up(self) -> bool:
        return bool(self.follow_up_to)

    def brief(self) -> SubagentBrief:
        constraints = (f"Expected output: {self.expected_output}",) if self.expected_output else ()
        return SubagentBrief(
            objective=self.goal,
            acceptance_criteria=self.acceptance_criteria,
            relevant_files=self.context_refs,
            constraints=constraints,
        )


def permission_request_for(
    profile: SubagentProfile, workspace: Workspace, goal: str
) -> PermissionRequest:
    """What the engine is asked about a delegation.

    The tier follows the *delegate's* authority, not the act of delegating. A read-only
    explorer changes nothing and is R0; one that can write is a workspace write; one that
    can run commands is local execution. Declaring a flat tier for "delegation" would make
    the cheapest and the most dangerous case indistinguishable.
    """
    if profile.policy.allow_local_execution:
        tier = RiskTier.R2_LOCAL_EXECUTION
        risk = RiskLevel.MEDIUM
    elif profile.policy.allow_workspace_writes:
        tier = RiskTier.R1_WORKSPACE_WRITE
        risk = RiskLevel.MEDIUM
    else:
        tier = RiskTier.R0_READ_ONLY
        risk = RiskLevel.LOW
    read_only = not (profile.policy.allow_workspace_writes or profile.policy.allow_local_execution)
    return PermissionRequest(
        tool_name=DELEGATE_TASK_NAME,
        operation="delegate",
        action=f"run a {profile.role.value} on: {goal}",
        workspace=workspace,
        risk=risk,
        tier=tier,
        is_read_only=read_only,
        is_destructive=False,
        reason=f"The agent wants a {profile.role.value} to handle part of the work.",
        possible_effects=(f"A delegate with these tools: {', '.join(profile.toolsets)}",),
        arguments={"role": profile.role.value, "goal": goal},
    )


_SCHEMA: JSONSchema = {
    "type": "object",
    "required": ["goal", "role", "acceptance_criteria"],
    "properties": {
        "goal": {
            "type": "string",
            "description": "One concrete objective, stated the way you would state it to a "
            "colleague who has not seen this conversation.",
        },
        "role": {
            "type": "string",
            "enum": [role.value for role in SubagentRole],
            "description": "explorer reads and reports; coder changes code; verifier runs "
            "the project's checks.",
        },
        "expected_output": {"type": "string"},
        "acceptance_criteria": {
            "type": "array",
            "items": {"type": "string"},
            "description": "How someone else would know this task is done. Required: a "
            "task nobody can check is not delegated.",
        },
        "context_refs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Files the delegate should start from. It does not see this "
            "conversation, so anything it needs must be named here.",
        },
        "follow_up_to": {
            "type": "string",
            "description": "The delegate_session_id of a delegate you already used, to "
            "ask it one more thing. It keeps what it already found out and shares its "
            "original budget, so this is cheaper than delegating again — but it can only "
            "be asked a limited number of times.",
        },
    },
}


def parse_delegation(arguments: JSONObject) -> DelegationRequest:
    """Read the model's request, refusing rather than guessing.

    An unrecognised role is refused instead of defaulted, for the same reason the plan
    parser refuses one: quietly turning an invented specialism into `coder` would hand a
    write-capable toolset to work that was meant to be read-only.
    """
    goal = arguments.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise ToolValidationError("delegate_task needs a goal")
    seguimiento = arguments.get("follow_up_to")
    if isinstance(seguimiento, str) and seguimiento.strip():
        # Un seguimiento no vuelve a declarar rol ni criterios: el delegado ya los tiene,
        # y pedirselos otra vez invitaria a cambiarselos por la puerta de atras.
        return DelegationRequest(
            goal=goal.strip(),
            role=SubagentRole.EXPLORER,
            follow_up_to=seguimiento.strip(),
        )
    raw_role = arguments.get("role")
    try:
        role = SubagentRole(raw_role) if isinstance(raw_role, str) else None
    except ValueError as exc:
        raise ToolValidationError(f"Unknown role: {raw_role!r}") from exc
    if role is None:
        raise ToolValidationError("delegate_task needs a role")
    criteria = _strings(arguments.get("acceptance_criteria"))
    if not criteria:
        # The same bar the planner applies. A delegate that cannot be checked will report
        # success on its own word, which is what verification exists to refuse.
        raise ToolValidationError("delegate_task needs at least one acceptance criterion")
    expected = arguments.get("expected_output")
    return DelegationRequest(
        goal=goal.strip(),
        role=role,
        expected_output=expected.strip() if isinstance(expected, str) else "",
        acceptance_criteria=criteria,
        context_refs=_strings(arguments.get("context_refs")),
    )


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


#: What a delegation returns, so a caller knows the shape without running one.
_OUTPUT_SCHEMA: JSONSchema = {
    "type": "object",
    "properties": {
        "role": {"type": "string"},
        "status": {"type": "string"},
        "summary": {"type": "string"},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "commands_run": {"type": "array", "items": {"type": "string"}},
        # Sin esto el modelo no puede volver a preguntarle: tendria un delegado
        # continuable y ninguna forma de nombrarlo.
        "delegate_session_id": {"type": "string"},
        "follow_ups_left": {"type": "integer"},
    },
    "required": ["role", "status", "summary", "delegate_session_id"],
    "additionalProperties": False,
}

#: Cuanto se le deja a una delegacion, derivado de lo que dura el delegado mas largo.
#:
#: Derivado y no escrito a mano: si alguien sube el presupuesto de un perfil, este techo
#: sube con el. Escribirlo aparte lo dejaria por debajo en cuanto cambiase el otro, y una
#: delegacion cortada por el reloj del que llama se lee como un delegado que fallo.
_DELEGATION_TIMEOUT = (
    max(profile.budget.timeout_seconds for profile in DEFAULT_PROFILES.values()) + 60.0
)

DELEGATE_TASK_SPEC = ToolSpec(
    name=DELEGATE_TASK_NAME,
    description=(
        "Hand one self-contained task to a specialist that does not see this "
        "conversation. Use it when a part of the work has its own objective and its own "
        "way of being checked. Do not use it to split work that has a single output."
    ),
    input_schema=_SCHEMA,
    output_schema=_OUTPUT_SCHEMA,
    risk=RiskLevel.MEDIUM,
    #: A delegate's answer is a summary, not a transcript, so it stays small by design.
    max_result_size_chars=8_000,
    timeout_seconds=_DELEGATION_TIMEOUT,
    load_policy=ToolLoadPolicy.CORE,
    search_hint="delegate subagent explorer coder verifier task",
)


def profile_for_request(
    request: DelegationRequest,
    parent_policy: PermissionPolicy,
    available_tools: frozenset[str],
) -> SubagentProfile:
    """The profile a delegation actually gets, after narrowing."""
    profile = DEFAULT_PROFILES.get(request.role)
    if profile is None:  # pragma: no cover - SubagentRole is closed
        raise ToolValidationError(f"No profile for role: {request.role.value}")
    return confine(profile, parent_policy, available_tools)


def describe_result(role: SubagentRole, summary: str, files: tuple[str, ...]) -> ToolResult:
    """What the parent sees. A summary and what changed, never the child's transcript."""
    lines = [f"{role.value} finished.", summary.strip()]
    if files:
        lines.append("Files changed: " + ", ".join(files))
    return ToolResult(call_id="", output="\n".join(line for line in lines if line))


class DelegateTaskTool:
    """Pedir un especialista, con el motor de permisos de por medio como todo lo demás.

    Las piezas existían desde H6 —el esquema, el parser, `narrow`, `confine`, la petición
    de permiso— y no había nada que las juntara, así que ningún modelo podía delegar. Esto
    es esa unión, y deliberadamente no añade política: decide el motor, ejecuta el servicio
    de subagentes, y aquí sólo se traduce.

    Lo que sí impone es el orden. Primero se lee lo pedido, luego se recorta a la autoridad
    del padre, y sólo entonces se pregunta. Preguntar por lo pedido en vez de por lo
    concedido dejaría que un explorer sin permiso de escritura obtuviera un coder que sí
    escribe — un escalado indirecto que ninguna respuesta posterior deshace.
    """

    def __init__(
        self,
        delegator: Delegator,
        catalog: Mapping[str, Tool],
        parent_policy: PermissionPolicy,
        *,
        profiles: Mapping[SubagentRole, SubagentProfile] | None = None,
    ) -> None:
        self._delegator = delegator
        self._catalog = dict(catalog)
        self._parent_policy = parent_policy
        self._profiles = dict(profiles or DEFAULT_PROFILES)

    @property
    def spec(self) -> ToolSpec:
        return DELEGATE_TASK_SPEC

    def validate(self, arguments: JSONObject) -> JSONObject:
        parse_delegation(arguments)
        return dict(arguments)

    def is_read_only(self, arguments: JSONObject) -> bool:
        """Sólo si el delegado tampoco puede escribir.

        La lectura del padre no dice nada: lo que importa es lo que podrá hacer el hijo,
        porque es él quien va a tocar el workspace.
        """
        return not self._confined(arguments).policy.allow_workspace_writes

    def is_destructive(self, arguments: JSONObject) -> bool:
        del arguments
        return False

    def is_concurrency_safe(self, arguments: JSONObject) -> bool:
        """Nunca.

        Un delegado abre su propio bucle y puede tocar cualquier cosa dentro de su
        autoridad; solaparlo con otra llamada del mismo turno sería conceder paralelismo
        sin saber sobre qué.
        """
        del arguments
        return False

    def permission(self, context: ToolContext, arguments: JSONObject) -> PermissionRequest:
        return permission_request_for(
            self._confined(arguments),
            context.workspace,
            parse_delegation(arguments).goal,
        )

    async def execute(
        self,
        context: ToolContext,
        arguments: JSONObject,
        cancellation: CancellationToken,
    ) -> ToolResult:
        request = parse_delegation(arguments)
        if request.is_follow_up:
            result = await self._continue(request, context, cancellation)
        else:
            confined = self._confined(arguments)
            result = await self._delegator.delegate(
                request.role,
                request.brief(),
                context.workspace,
                cancellation,
                parent_session_id=context.session_id,
                budget=confined.budget,
            )
        return ToolResult(
            {
                "role": result.role.value,
                "status": result.status.value,
                "summary": result.answer or "",
                "files_changed": list(result.files_modified),
                "commands_run": list(result.commands_run),
                "delegate_session_id": result.session_id,
                "follow_ups_left": self._follow_ups_left(result.session_id),
            }
        )

    async def _continue(
        self,
        request: DelegationRequest,
        context: ToolContext,
        cancellation: CancellationToken,
    ) -> SubagentResult:
        """Volver a preguntarle a un delegado que ya trabajo.

        No se recorta otra vez el perfil: el delegado ya existe con la autoridad que se le
        concedio, y recalcularla aqui abriria la puerta a que un seguimiento obtuviera mas
        de lo que obtuvo el encargo original.
        """
        if not isinstance(self._delegator, Continuable):
            raise ToolValidationError(
                "Este despliegue no puede continuar delegados: pide uno nuevo"
            )
        return await self._delegator.follow_up(
            request.follow_up_to,
            request.goal,
            context.workspace,
            cancellation,
            parent_session_id=context.session_id,
        )

    def _follow_ups_left(self, session_id: str) -> int:
        """Cuantas veces mas se le puede preguntar. Cero si no es continuable.

        Se le dice al modelo porque es lo que decide si le sale a cuenta seguir con este o
        pedir otro, y porque un limite que no se ve se descubre chocando con el.
        """
        if not isinstance(self._delegator, Continuable):
            return 0
        return self._delegator.follow_ups_left(session_id)

    def project(self, result: ToolResult) -> ToolProjection:
        """Lo que el delegado contesto, y como volver a preguntarle.

        El caso general miraba la primera lista del resultado —`files_changed`— y decidia
        que eso era lo que habia que enumerar. Para un explorer, que no cambia ficheros,
        eso significa una lista vacia: al modelo se le devolvia «(sin resultados)» despues
        de una delegacion que habia ido bien y traia hallazgos. Se vio en un run real, y
        es exactamente para esto para lo que existe esta costura.
        """
        salida = result.output if isinstance(result.output, dict) else {}
        resumen = str(salida.get("summary") or "")
        ficheros = salida.get("files_changed")
        ficheros = ficheros if isinstance(ficheros, list) else []
        hijo = str(salida.get("delegate_session_id") or "")
        quedan = salida.get("follow_ups_left")
        quedan = quedan if isinstance(quedan, int) else 0
        lineas = [
            f"The {salida.get('role', 'delegate')} reported ({salida.get('status')}):",
            resumen,
        ]
        if ficheros:
            lineas.append("Files it changed: " + ", ".join(str(item) for item in ficheros))
        if quedan > 0 and hijo:
            # Solo si de verdad quedan: ofrecerselo cuando no puede usarlo le haria
            # gastar una llamada en descubrir que no.
            lineas.append(
                f"You can ask this same delegate {quedan} more question(s) with "
                f'delegate_task using follow_up_to="{hijo}". It keeps what it already '
                "found out and shares its original budget."
            )
        return ToolProjection(
            model=ModelView("\n".join(item for item in lineas if item)),
            display=DisplayView(
                kind=ResultKind.RECORD,
                title=f"{salida.get('role', 'delegate')} · {salida.get('status', '')}".strip(),
                summary=resumen.splitlines()[0][:200] if resumen else "",
                items=tuple(str(item) for item in ficheros),
                facts={
                    "role": salida.get("role"),
                    "status": salida.get("status"),
                    "delegate_session_id": hijo,
                    "follow_ups_left": quedan,
                    "files_changed": len(ficheros),
                },
            ),
        )

    def _confined(self, arguments: JSONObject) -> SubagentProfile:
        """El perfil que de verdad va a correr: lo pedido ∩ lo que el padre tiene.

        Se calcula aquí y se usa para las tres respuestas —si es de sólo lectura, qué se
        pregunta y qué se ejecuta— para que no puedan discrepar entre sí.
        """
        request = parse_delegation(arguments)
        return confine(self._profiles[request.role], self._parent_policy, frozenset(self._catalog))


__all__ = [
    "DELEGATE_TASK_NAME",
    "DELEGATE_TASK_SPEC",
    "DelegateTaskTool",
    "DelegationRequest",
    "confine",
    "describe_result",
    "narrow",
    "parse_delegation",
    "permission_request_for",
    "profile_for_request",
]
