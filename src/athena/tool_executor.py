"""Validation, permission, execution, correlation, and result-size enforcement."""

from __future__ import annotations

import json
from dataclasses import replace

from athena.async_utils import await_cancellable
from athena.cancellation import CancellationToken
from athena.errors import (
    AthenaRuntimeError,
    PermissionDeniedError,
    ToolValidationError,
    WorkspaceBoundaryError,
)
from athena.events import EventBus, EventName, PermissionEvent, ToolEvent
from athena.models import ModelToolCall
from athena.permissions import (
    DenyingPermissionPrompt,
    PermissionDecision,
    PermissionEngine,
    PermissionPrompt,
    PermissionRequest,
)
from athena.registry import ToolRegistry
from athena.stores import ToolResultStore
from athena.tools import ToolContext, ToolResult, ToolResultSizePolicy, ToolSpec
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
        tool_timeout_seconds: float | None = 30.0,
        summary_chars: int = 500,
    ) -> None:
        self.registry = registry
        self.permissions = permissions
        self.result_store = result_store
        self.event_bus = event_bus
        self.prompt = prompt or DenyingPermissionPrompt()
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
            result = await await_cancellable(
                tool.execute(context, arguments, cancellation),
                cancellation,
                timeout=self.tool_timeout_seconds,
            )
            correlated = replace(result, call_id=call.call_id)
            final = await self._apply_result_policy(tool.spec, correlated, cancellation)
            await self.event_bus.publish(
                ToolEvent(
                    EventName.TOOL_COMPLETED,
                    session_id,
                    {
                        "tool_name": call.name,
                        "externalized": final.reference is not None,
                        "size_chars": final.reference.size_chars if final.reference else None,
                    },
                    call.call_id,
                )
            )
            return final
        except AthenaRuntimeError as exc:
            await self.event_bus.publish(
                ToolEvent(
                    EventName.TOOL_FAILED,
                    session_id,
                    {"tool_name": call.name, "error_code": exc.code, "message": exc.message},
                    call.call_id,
                )
            )
            raise

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


def _serialize(value: JSONValue) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
