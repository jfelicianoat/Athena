"""Sesion: hooks, persistencia, habilidades, contexto y capacidades.

Todo lo que rodea a una vuelta del bucle sin ser la vuelta en si. El
contexto se selecciona aqui porque es una decision de sesion, no de paso.
"""

from __future__ import annotations

from athena.agent_loop.base import BucleBase
from athena.agent_loop.tipos import _RunData
from athena.cancellation import CancellationToken
from athena.capabilities import UnsupportedCapabilityError, match, requirements_for
from athena.events import (
    AgentEvent,
    EventName,
    ModelEvent,
)
from athena.hooks import (
    HookBlockedError,
    HookContext,
    HookEvent,
)
from athena.memory import ConversationContext
from athena.models import (
    ModelMessage,
    ModelRequest,
)
from athena.session_store import (
    EventCheckpoint,
    SessionRecord,
)
from athena.state import (
    AgentStatus,
)
from athena.tools import ToolResultReference
from athena.types import JSONObject
from athena.workspace import Workspace


class SesionMixin(BucleBase):
    """Sesion: hooks, persistencia, habilidades, contexto y capacidades."""

    async def _hook(self, event: HookEvent, session_id: str, payload: JSONObject) -> None:
        """Extensions may refuse an action. They can never authorize one."""
        report = await self.hooks.run(HookContext(event, session_id, payload))
        if report.blocked:
            raise HookBlockedError(
                f"{event.value} blocked by {report.blocked_by}: {report.reason}",
                details={"event": event.value, "hook": report.blocked_by},
            )

    async def _finish(
        self,
        data: _RunData,
        outcome: str,
        error_code: str | None = None,
        message: str = "",
    ) -> None:
        """Announce the end of the run, and any typed error that ended it."""
        session_id = data.session.session_id
        if error_code is not None:
            await self._hook_quietly(
                HookEvent.ON_ERROR,
                session_id,
                {"error_code": error_code, "message": message, "outcome": outcome},
            )
        await self._hook_quietly(
            HookEvent.SESSION_END,
            session_id,
            {
                "outcome": outcome,
                "error_code": error_code,
                "files_modified": list(data.working.files_modified),
                "repair_cycles": data.repair_cycles,
            },
        )

    async def _hook_quietly(self, event: HookEvent, session_id: str, payload: JSONObject) -> None:
        """Terminal notifications: a refusal here has nothing left to refuse."""
        await self.hooks.run(HookContext(event, session_id, payload))

    def _select_skills(self, objective: str, data: _RunData) -> None:
        """Skills describe how to work. They never add a tool or widen a permission."""
        data.skills = self.skills.select(objective, self.registry.names())
        if data.skills:
            data.working = data.working.noting(
                decisions=tuple(
                    f"Following skill {selection.skill.name} v{selection.skill.version}"
                    for selection in data.skills
                )
            )

    async def _persist(
        self,
        data: _RunData,
        workspace: Workspace,
        status: AgentStatus,
        checkpoint: str,
        payload: JSONObject | None = None,
    ) -> None:
        """Write the session out. Losing power must not lose what Athena learned."""
        if self.session_store is None:
            return
        data.checkpoints.append(EventCheckpoint(checkpoint, payload or {}))
        record = SessionRecord(
            session_id=data.session.session_id,
            workspace_id=workspace.workspace_id,
            status=status,
            working_memory=data.working,
            tool_references=tuple(
                reference
                for reference in data.references
                if isinstance(reference, ToolResultReference)
            ),
            verification=dict(data.working.verification),
            checkpoints=tuple(data.checkpoints[-50:]),
            created_at=data.session.created_at,
        )
        await self.session_store.save(record)
        await self.event_bus.publish(
            AgentEvent(
                EventName.SESSION_PERSISTED,
                data.session.session_id,
                {"status": status.value, "checkpoint": checkpoint},
            )
        )

    def _select_context(self, data: _RunData) -> tuple[ModelMessage, ...]:
        """Choose what to send. The durable facts are re-rendered from working memory."""
        selected, report = self.context_window.select(
            ConversationContext(tuple(data.history)), data.working
        )
        if report is not None and report.changed:
            data.history = list(selected.messages)
            data.compactions += 1
            data.pending_compaction = report
        return selected.messages

    async def _capture_baseline(
        self, workspace: Workspace, cancellation: CancellationToken
    ) -> None:
        """Record which checks were already failing, so blame lands where it belongs."""
        if not self.config.capture_baseline:
            return
        capture = getattr(self.verification, "capture_baseline", None)
        if capture is None:
            return
        await capture(workspace, cancellation)

    async def _require_capabilities(
        self, request: ModelRequest, data: _RunData, request_id: str
    ) -> None:
        """Comprobar que el proveedor ofrece lo que esta petición necesita.

        Lo requerido sale de la petición, no de una configuración: si lleva herramientas,
        hace falta un proveedor que las admita, y eso no es una opción de despliegue. Dos
        fuentes para la misma verdad acabarían discrepando.

        Lo preferido que falte se anuncia y no impide nada, que es la diferencia entre
        «esto no puede hacerse» y «esto podría hacerse mejor».
        """
        needed = requirements_for(
            offers_tools=bool(request.tools),
            needs_schema=request.response_schema is not None,
        )
        if not needed:
            return
        result = match("model_provider", self.provider.capabilities(), needed)
        if result.missing_preferred:
            await self.event_bus.publish(
                ModelEvent(
                    EventName.CAPABILITY_MISSING,
                    data.session.session_id,
                    {"missing": list(result.missing_preferred), "required": False},
                    request_id,
                )
            )
        if result.usable:
            return
        await self.event_bus.publish(
            ModelEvent(
                EventName.CAPABILITY_MISSING,
                data.session.session_id,
                {"missing": list(result.missing_required), "required": True},
                request_id,
            )
        )
        raise UnsupportedCapabilityError(
            "The model provider does not offer what this request requires",
            details=result.to_json(),
        )
