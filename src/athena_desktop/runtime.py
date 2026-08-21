"""Application layer shared by the desktop window and tests."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from athena.adapters.ai_broker import AiBrokerModelProvider
from athena.adapters.openai_compatible import OpenAICompatibleModelProvider
from athena.agent_loop import AgentLoop, AgentLoopConfig, AgentRunResult
from athena.cancellation import CancellationSource
from athena.context import ContextBuilder
from athena.events import InMemoryEventBus, RuntimeEvent
from athena.git_tools import GitCommitTool, git_read_tools
from athena.models import ModelProvider
from athena.mutation_tools import workspace_mutation_tools
from athena.permissions import (
    PermissionDecision,
    PermissionPolicy,
    PermissionRequest,
    PolicyPermissionEngine,
)
from athena.process_tools import BashTool
from athena.registry import ToolRegistry
from athena.repository_tools import repository_read_tools
from athena.session_store import SqliteSessionStore
from athena.stores import SqliteToolResultStore
from athena.tool_executor import ToolExecutor
from athena.tool_search import ToolSearchTool
from athena.tools import Tool
from athena.verification import (
    CommandVerificationPolicy,
    LoopCompletionVerificationPolicy,
    VerificationPlanner,
    VerificationPolicy,
)
from athena.workspace import Workspace
from athena_desktop.config import CapabilityMode, ProviderKind

EventCallback = Callable[[RuntimeEvent], None]
PermissionCallback = Callable[[PermissionRequest], PermissionDecision]

_FILE_VERB_PREFIXES = (
    "añad",
    "crae",
    "crea",
    "corrig",
    "edit",
    "escrib",
    "genera",
    "guard",
    "modific",
    "add",
    "create",
    "fix",
    "generate",
    "modify",
    "save",
    "write",
)
_FILE_NOUN_PREFIXES = ("archivo", "documento", "fichero", "document", "file")


@dataclass(frozen=True, slots=True)
class RunConfiguration:
    workspace: Path
    objective: str
    provider: ProviderKind
    base_url: str
    model: str = ""
    token: str = ""
    writes: CapabilityMode = "off"
    execution: CapabilityMode = "off"
    max_iterations: int = 12
    timeout_seconds: float = 120.0

    def validate(self) -> None:
        if not self.workspace.is_dir():
            raise ValueError("Selecciona una carpeta de trabajo válida")
        if not self.objective.strip():
            raise ValueError("Describe qué quieres que haga Athena")
        parsed = urlsplit(self.base_url.rstrip("/"))
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("La URL del proveedor debe comenzar por http:// o https://")
        if self.provider is ProviderKind.AI_BROKER and not self.token.strip():
            raise ValueError("AI_Broker necesita un token")
        if self.provider is ProviderKind.OPENAI_COMPATIBLE and not self.model.strip():
            raise ValueError("El proveedor OpenAI-compatible necesita un modelo")
        if self.max_iterations <= 0 or self.timeout_seconds <= 0:
            raise ValueError("Los límites de ejecución deben ser mayores que cero")


class DesktopPermissionPrompt:
    def __init__(self, callback: PermissionCallback) -> None:
        self._callback = callback

    async def confirm(self, request: PermissionRequest) -> PermissionDecision:
        return await asyncio.to_thread(self._callback, request)


def build_provider(configuration: RunConfiguration) -> ModelProvider:
    configuration.validate()
    if configuration.provider is ProviderKind.AI_BROKER:
        return AiBrokerModelProvider(
            configuration.base_url,
            configuration.token,
            preferred_model=configuration.model.strip() or None,
        )
    return OpenAICompatibleModelProvider(
        configuration.base_url,
        configuration.model,
        api_key=configuration.token or None,
    )


def build_tools(configuration: RunConfiguration, event_bus: InMemoryEventBus) -> tuple[Tool, ...]:
    tools: list[Tool] = [*repository_read_tools(), *git_read_tools()]
    if configuration.writes != "off":
        tools.extend(workspace_mutation_tools(event_bus))
        tools.append(GitCommitTool())
    if configuration.execution != "off":
        tools.append(BashTool(event_bus=event_bus))
    return tuple(tools)


def build_verification(workspace: Workspace, event_bus: InMemoryEventBus) -> VerificationPolicy:
    command_policy = CommandVerificationPolicy(VerificationPlanner(workspace), event_bus=event_bus)
    if command_policy.plan.is_empty:
        return LoopCompletionVerificationPolicy()
    return command_policy


def requires_workspace_change(objective: str) -> bool:
    words = re.findall(r"[^\W_]+", objective.casefold(), flags=re.UNICODE)
    has_action = any(word.startswith(_FILE_VERB_PREFIXES) for word in words)
    has_target = any(word.startswith(_FILE_NOUN_PREFIXES) for word in words)
    return has_action and has_target


async def run_athena(
    configuration: RunConfiguration,
    cancellation: CancellationSource,
    *,
    on_event: EventCallback,
    on_permission: PermissionCallback,
) -> AgentRunResult:
    configuration.validate()
    workspace = Workspace.from_path(configuration.workspace)
    state_dir = workspace.root / ".athena"
    session_store = SqliteSessionStore(state_dir / "sessions.db")
    await session_store.mark_interrupted()

    event_bus = InMemoryEventBus()
    event_bus.subscribe(on_event)
    registry = ToolRegistry(build_tools(configuration, event_bus))
    if registry.deferred_names():
        registry.register(ToolSearchTool(registry))
    permission_engine = PolicyPermissionEngine(
        PermissionPolicy(
            allow_workspace_writes=configuration.writes == "allow",
            allow_local_execution=configuration.execution == "allow",
        )
    )
    executor = ToolExecutor(
        registry,
        permission_engine,
        SqliteToolResultStore(state_dir / "results.db"),
        event_bus,
        prompt=DesktopPermissionPrompt(on_permission),
    )
    loop = AgentLoop(
        build_provider(configuration),
        registry,
        executor,
        ContextBuilder(workspace),
        event_bus,
        verification=build_verification(workspace, event_bus),
        session_store=session_store,
        config=AgentLoopConfig(
            max_iterations=configuration.max_iterations,
            session_timeout_seconds=configuration.timeout_seconds,
            require_workspace_change=(
                configuration.writes != "off" and requires_workspace_change(configuration.objective)
            ),
        ),
    )
    return await loop.run(configuration.objective.strip(), workspace, cancellation.token)


__all__ = [
    "DesktopPermissionPrompt",
    "EventCallback",
    "PermissionCallback",
    "RunConfiguration",
    "build_provider",
    "build_tools",
    "build_verification",
    "requires_workspace_change",
    "run_athena",
]
