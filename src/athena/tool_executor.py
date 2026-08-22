"""Validation, permission, execution, correlation, and result-size enforcement."""

from __future__ import annotations

import json
from dataclasses import replace

from athena.async_utils import await_cancellable
from athena.cancellation import CancellationToken
from athena.errors import (
    AthenaRuntimeError,
    PermissionDeniedError,
    ToolContractError,
    ToolValidationError,
    WorkspaceBoundaryError,
)
from athena.events import EventBus, EventName, PermissionEvent, ToolEvent
from athena.hooks import (
    HookBlockedError,
    HookContext,
    HookEvent,
    HookRegistry,
    HookReport,
)
from athena.models import ModelToolCall
from athena.permissions import (
    DenyingPermissionPrompt,
    PermissionDecision,
    PermissionEngine,
    PermissionPrompt,
    PermissionRequest,
    RiskTier,
)
from athena.registry import ToolRegistry
from athena.schema import violations
from athena.stores import ToolResultStore
from athena.tool_projection import ToolProjection, project
from athena.tools import (
    OutputContract,
    ToolContext,
    ToolResult,
    ToolResultSizePolicy,
    ToolSpec,
)
from athena.types import JSONValue
from athena.workspace import Workspace


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        permissions: PermissionEngine,
        result_store: ToolResultStore,
        event_bus: EventBus,
        *,
        prompt: PermissionPrompt | None = None,
        hooks: HookRegistry | None = None,
        tool_timeout_seconds: float | None = 30.0,
        summary_chars: int = 500,
    ) -> None:
        self.registry = registry
        self.permissions = permissions
        self.result_store = result_store
        self.event_bus = event_bus
        self.prompt = prompt or DenyingPermissionPrompt()
        self.hooks = hooks or HookRegistry()
        self.tool_timeout_seconds = tool_timeout_seconds
        self.summary_chars = summary_chars

    async def execute(
        self,
        call: ModelToolCall,
        *,
        session_id: str,
        workspace: Workspace,
        cancellation: CancellationToken,
    ) -> ToolResult:
        context = ToolContext(session_id, workspace, call.call_id)
        try:
            if not call.call_id.strip():
                raise ToolValidationError("Tool call ID must be non-empty")
            tool = self.registry.get(call.name)
            try:
                arguments = tool.validate(call.arguments)
            except AthenaRuntimeError:
                raise
            except (TypeError, ValueError) as exc:
                raise ToolValidationError(
                    f"Invalid input for {call.name}: {exc}",
                    details={"tool_name": call.name, "call_id": call.call_id},
                ) from exc
            await self._hook(
                HookEvent.PRE_TOOL_USE,
                session_id,
                {"tool_name": call.name, "call_id": call.call_id, "arguments": dict(arguments)},
            )
            try:
                request = tool.permission(context, arguments)
            except WorkspaceBoundaryError:
                await self.event_bus.publish(
                    PermissionEvent(
                        EventName.PERMISSION_RESOLVED,
                        session_id,
                        {"tool_name": call.name, "decision": PermissionDecision.DENY.value},
                        call.call_id,
                    )
                )
                raise
            await self.event_bus.publish(
                PermissionEvent(
                    EventName.PERMISSION_REQUESTED,
                    session_id,
                    {
                        "tool_name": call.name,
                        "risk": request.risk.value,
                        "tier": request.tier.value,
                        "action": request.action,
                        "reason": request.reason,
                        "possible_effects": list(request.possible_effects),
                    },
                    call.call_id,
                )
            )
            decision = self.permissions.decide(request)
            asked = decision is PermissionDecision.ASK
            if asked:
                decision = await self._ask(request, cancellation)
            await self.event_bus.publish(
                PermissionEvent(
                    EventName.PERMISSION_RESOLVED,
                    session_id,
                    {"tool_name": call.name, "decision": decision.value, "asked": asked},
                    call.call_id,
                )
            )
            if decision is not PermissionDecision.ALLOW:
                raise PermissionDeniedError(
                    f"Permission {decision.value} for tool {call.name}",
                    details={"decision": decision.value, "call_id": call.call_id},
                )
            await self.event_bus.publish(
                ToolEvent(
                    EventName.TOOL_STARTED,
                    session_id,
                    {"tool_name": call.name},
                    call.call_id,
                )
            )
            editing = request.tier is RiskTier.R1_WORKSPACE_WRITE
            if editing:
                await self._hook(
                    HookEvent.PRE_EDIT,
                    session_id,
                    {
                        "tool_name": call.name,
                        "call_id": call.call_id,
                        "resources": list(request.resources),
                    },
                )
            result = await await_cancellable(
                tool.execute(context, arguments, cancellation),
                cancellation,
                # Lo que la tool declare, y si no declara nada, el techo generico. Al
                # reves —el generico siempre— una delegacion moria a los 30 s pasara lo
                # que pasara, y el fallo se atribuia al delegado en vez de al reloj.
                timeout=tool.spec.timeout_seconds or self.tool_timeout_seconds,
            )
            correlated = replace(result, call_id=call.call_id)
            # El contrato se comprueba sobre el resultado canonico, antes de externalizar:
            # despues, lo que hay es el recibo del almacen y no lo que la tool prometio, y
            # comprobar el recibo contra el esquema del resultado daria por incumplido
            # cualquier resultado grande.
            await self._check_contract(tool.spec, correlated, session_id, call.call_id)
            final = await self._apply_result_policy(tool.spec, correlated, cancellation)
            projection = project(tool, tool.spec, final)
            if editing:
                await self._hook(
                    HookEvent.POST_EDIT,
                    session_id,
                    {
                        "tool_name": call.name,
                        "call_id": call.call_id,
                        "resources": list(request.resources),
                    },
                )
            await self._hook(
                HookEvent.POST_TOOL_USE,
                session_id,
                {
                    "tool_name": call.name,
                    "call_id": call.call_id,
                    "externalized": final.reference is not None,
                },
            )
            await self.event_bus.publish(
                ToolEvent(
                    EventName.TOOL_COMPLETED,
                    session_id,
                    {
                        "tool_name": call.name,
                        "externalized": final.reference is not None,
                        "size_chars": final.reference.size_chars if final.reference else None,
                        # Lo que una interfaz necesita para dibujar esto, ya derivado. Sin
                        # ello cada cliente vuelve a deducir la presentacion leyendo un
                        # payload interno, y acaban discrepando entre si.
                        "display": projection.display.to_json(),
                    },
                    call.call_id,
                )
            )
            return _with_projection(final, projection)
        except AthenaRuntimeError as exc:
            await self.event_bus.publish(
                ToolEvent(
                    EventName.TOOL_FAILED,
                    session_id,
                    {"tool_name": call.name, "error_code": exc.code, "message": exc.message},
                    call.call_id,
                )
            )
            await self._hook_report(
                HookEvent.ON_ERROR,
                session_id,
                {
                    "tool_name": call.name,
                    "call_id": call.call_id,
                    "error_code": exc.code,
                    "message": exc.message,
                },
            )
            raise

    async def _check_contract(
        self, spec: ToolSpec, result: ToolResult, session_id: str, call_id: str | None
    ) -> None:
        """Comprobar que la tool devolvio lo que dijo que devolveria."""
        desviaciones = violations(spec.output_schema, result.output, where=spec.name)
        if not desviaciones:
            return
        if spec.output_contract is OutputContract.ENFORCED:
            raise ToolContractError(
                f"{spec.name} devolvio algo que no cumple su contrato: {desviaciones[0]}",
                details={"tool_name": spec.name, "violations": list(desviaciones)},
            )
        await self.event_bus.publish(
            ToolEvent(
                EventName.TOOL_CONTRACT_VIOLATED,
                session_id,
                {"tool_name": spec.name, "violations": list(desviaciones)},
                call_id,
            )
        )

    async def _hook(self, event: HookEvent, session_id: str, payload: JSONValue) -> HookReport:
        """Run an extension point. A BLOCK stops the action; nothing can unblock one."""
        report = await self._hook_report(event, session_id, payload)
        if report.blocked:
            raise HookBlockedError(
                f"{event.value} blocked by {report.blocked_by}: {report.reason}",
                details={"event": event.value, "hook": report.blocked_by},
            )
        return report

    async def _hook_report(
        self, event: HookEvent, session_id: str, payload: JSONValue
    ) -> HookReport:
        if not isinstance(payload, dict):
            payload = {}
        return await self.hooks.run(HookContext(event, session_id, payload))

    async def _ask(
        self, request: PermissionRequest, cancellation: CancellationToken
    ) -> PermissionDecision:
        """Resolve an ASK through the interface. Approval is single-use, never cached."""
        answer = await await_cancellable(self.prompt.confirm(request), cancellation)
        return answer if answer is PermissionDecision.ALLOW else PermissionDecision.DENY

    async def _apply_result_policy(
        self,
        spec: ToolSpec,
        result: ToolResult,
        cancellation: CancellationToken,
    ) -> ToolResult:
        serialized = _serialize(result.output)
        externalize = (
            spec.result_size_policy is ToolResultSizePolicy.ALWAYS_EXTERNALIZE
            or len(serialized) > spec.max_result_size_chars
        )
        if not externalize:
            return result
        reference = await self.result_store.put(
            serialized,
            media_type="application/json" if not isinstance(result.output, str) else "text/plain",
            cancellation=cancellation,
        )
        summary = serialized[: self.summary_chars]
        if len(serialized) > self.summary_chars:
            summary += "…"
        output: JSONValue = {
            "summary": summary,
            "externalized": True,
            "size_chars": len(serialized),
            "reference_uri": reference.uri,
        }
        return replace(result, output=output, reference=reference)


def _with_projection(result: ToolResult, projection: ToolProjection) -> ToolResult:
    """Adjuntar las vistas sin tocar el resultado canonico.

    Van en `metadata` y no en `output` a proposito: `output` es lo que la tool prometio y
    lo que se comprobo contra su esquema, y meterle ahi una vista lo convertiria en algo
    que ya no cumple su propio contrato.
    """
    return replace(
        result,
        metadata={
            **result.metadata,
            "model_view": projection.model.to_json(),
            "display": projection.display.to_json(),
        },
    )


def _serialize(value: JSONValue) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
