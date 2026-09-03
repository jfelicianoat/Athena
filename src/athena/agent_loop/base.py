"""Dependencias del bucle y utilidades sin fase propia.

Las estaticas de aqui son transformaciones puras sobre estado: compactar una
peticion, anotar una herramienta usada, aplicar un presupuesto. Ninguna
publica eventos ni persiste nada, y por eso se pueden leer aisladas.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

from athena.agent_loop.tipos import AgentLoopConfig, _RunData
from athena.budget import RuntimeBudget
from athena.concurrency import ConcurrencyScheduler
from athena.context import ContextBuilder
from athena.events import (
    EventBus,
)
from athena.hooks import (
    HookRegistry,
)
from athena.memory import ContextWindowManager
from athena.models import (
    ModelMessage,
    ModelProvider,
    ModelRequest,
    ModelRole,
    ModelToolCall,
)
from athena.recovery import RecoveryLimits, RecoveryPolicy
from athena.registry import ToolRegistry
from athena.session_store import (
    SessionStore,
)
from athena.skills import SkillRegistry
from athena.state import (
    AgentStatus,
    BudgetState,
    SessionState,
)
from athena.tool_executor import ToolExecutor
from athena.types import JSONObject, JSONValue
from athena.verification import (
    LoopCompletionVerificationPolicy,
    VerificationPolicy,
)
from athena.working_state import WorkingState


class BucleBase:
    """Dependencias inyectadas y helpers puros del bucle."""

    def __init__(
        self,
        provider: ModelProvider,
        registry: ToolRegistry,
        executor: ToolExecutor,
        context_builder: ContextBuilder,
        event_bus: EventBus,
        *,
        verification: VerificationPolicy | None = None,
        recovery: RecoveryPolicy | None = None,
        session_store: SessionStore | None = None,
        context_window: ContextWindowManager | None = None,
        hooks: HookRegistry | None = None,
        skills: SkillRegistry | None = None,
        config: AgentLoopConfig | None = None,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.scheduler = ConcurrencyScheduler()
        self.executor = executor
        self.context_builder = context_builder
        self.event_bus = event_bus
        self.verification = verification or LoopCompletionVerificationPolicy()
        self.session_store = session_store
        self.context_window = context_window or ContextWindowManager()
        self.hooks = hooks or HookRegistry()
        self.skills = skills or SkillRegistry()
        self.config = config or AgentLoopConfig()
        self.recovery = recovery or RecoveryPolicy(
            RecoveryLimits(
                model_retries=self.config.max_model_retries,
                model_backoff_seconds=self.config.retry_backoff_seconds,
            )
        )

    @staticmethod
    def _reveal(output: JSONValue, data: _RunData) -> None:
        """A searched-for tool becomes visible on the next turn, and only then."""
        if not isinstance(output, dict):
            return
        revealed = output.get("revealed")
        if not isinstance(revealed, list):
            return
        data.revealed_tools.update(name for name in revealed if isinstance(name, str))

    @staticmethod
    def _compact(request: ModelRequest) -> ModelRequest:
        """Drop the middle of the conversation, keeping the framing and the latest turns."""
        messages = request.messages
        if len(messages) <= 4:
            return request
        return replace(request, messages=(*messages[:2], *messages[-2:]))

    @staticmethod
    def _record_tool_use(working: WorkingState, call: ModelToolCall) -> WorkingState:
        """Operational state comes from the call itself, not from re-reading the chat.

        Only ever called after the call succeeded, so the record is of fact, not intent.
        """
        arguments = call.arguments
        path = arguments.get("path")
        if call.name in ("write_file", "edit_file") and isinstance(path, str):
            return working.modifying(files_modified=(path,))
        if call.name == "bash":
            command = arguments.get("command")
            if isinstance(command, str):
                return working.ran(command)
        if isinstance(path, str):
            return working.observing(files_examined=(path,))
        return working

    @staticmethod
    def _tool_message(call: ModelToolCall, payload: JSONObject) -> ModelMessage:
        return ModelMessage(
            ModelRole.TOOL,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            name=call.name,
            tool_call_id=call.call_id,
        )

    @staticmethod
    def _remember_paths(call: ModelToolCall, data: _RunData) -> None:
        for key in ("path", "glob", "pattern"):
            value = call.arguments.get(key)
            if isinstance(value, str) and "*" not in value and "?" not in value:
                data.discovered_paths.add(value)

    @staticmethod
    def _with_budget(
        session: SessionState,
        budget: RuntimeBudget,
        status: AgentStatus,
    ) -> SessionState:
        return replace(
            session,
            agent=replace(
                session.agent,
                status=status,
                budget=BudgetState(
                    max_steps=budget.limits.max_iterations,
                    used_steps=budget.usage.iterations,
                ),
            ),
            updated_at=datetime.now(UTC),
        )

    @staticmethod
    def _set_status(
        session: SessionState,
        status: AgentStatus,
        error_code: str | None = None,
    ) -> SessionState:
        return replace(
            session,
            agent=replace(
                session.agent,
                status=status,
                active_model_request_id=None,
                active_tool_call_ids=(),
                last_error_code=error_code,
            ),
            updated_at=datetime.now(UTC),
        )
