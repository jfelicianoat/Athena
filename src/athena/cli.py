"""Minimal development CLI that assembles and observes the runtime.

The CLI contains no agent logic. It wires a provider, a registry and a policy, prints
runtime events, and answers permission prompts on the user's behalf.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import sys
from pathlib import Path

from athena.adapters import OpenAICompatibleModelProvider
from athena.agent_loop import AgentLoop, AgentLoopConfig, AgentRunStatus
from athena.cancellation import CancellationSource
from athena.context import ContextBuilder
from athena.events import InMemoryEventBus, RuntimeEvent
from athena.git_tools import GitCommitTool, git_read_tools
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
from athena.stores import InMemoryToolResultStore
from athena.tool_executor import ToolExecutor
from athena.tools import Tool
from athena.workspace import Workspace

_CAPABILITY_MODES = ("off", "ask", "allow")


class ConsolePermissionPrompt:
    """Asks the operator to approve one specific action, once.

    The answer applies to this call only. There is no standing approval, so a later
    request for the same tool is asked again.
    """

    def __init__(self, stream: object | None = None) -> None:
        self.stream = stream or sys.stderr

    async def confirm(self, request: PermissionRequest) -> PermissionDecision:
        lines = [
            "",
            "--- Athena needs permission -------------------------------------",
            f"  tool     : {request.tool_name}",
            f"  action   : {request.action or request.operation}",
            f"  risk     : {request.risk.value} ({request.tier.value})",
            f"  reason   : {request.reason}",
            f"  workspace: {request.workspace.root}",
        ]
        lines.extend(f"  effect   : {effect}" for effect in request.possible_effects)
        lines.append("-----------------------------------------------------------------")
        print("\n".join(lines), file=sys.stderr)
        answer = await asyncio.to_thread(input, "Allow this one action? [y/N] ")
        if answer.strip().lower() in ("y", "yes"):
            return PermissionDecision.ALLOW
        return PermissionDecision.DENY


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Investigate a repository with Athena")
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--objective", "-o")
    parser.add_argument(
        "--base-url",
        default=os.getenv("ATHENA_BASE_URL", "http://localhost:1234/v1"),
    )
    parser.add_argument("--model", default=os.getenv("ATHENA_MODEL", "local-model"))
    parser.add_argument("--max-iterations", type=int, default=12)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--writes",
        choices=_CAPABILITY_MODES,
        default="off",
        help="off: no mutation tools; ask: offered but confirmed per call; allow: granted",
    )
    parser.add_argument(
        "--exec",
        dest="execution",
        choices=_CAPABILITY_MODES,
        default="off",
        help="off: no command tool; ask: confirmed per call; allow: granted by policy",
    )
    return parser


def _tools(arguments: argparse.Namespace, event_bus: InMemoryEventBus) -> tuple[Tool, ...]:
    tools: list[Tool] = [*repository_read_tools(), *git_read_tools()]
    if arguments.writes != "off":
        tools.extend(workspace_mutation_tools(event_bus))
        tools.append(GitCommitTool())
    if arguments.execution != "off":
        tools.append(BashTool(event_bus=event_bus))
    return tuple(tools)


def _install_cancellation(source: CancellationSource) -> None:
    """Route the interactive interrupt into the runtime's cancellation token.

    Replacing the default SIGINT handler stops Python from raising KeyboardInterrupt,
    so the loop observes cancellation and unwinds session -> model -> tool itself.
    """
    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGINT, source.cancel)
        return
    except (NotImplementedError, RuntimeError, ValueError, AttributeError):
        pass
    with contextlib.suppress(OSError, ValueError):
        signal.signal(signal.SIGINT, lambda *_: source.cancel())


async def _run(arguments: argparse.Namespace, source: CancellationSource | None = None) -> int:
    workspace = Workspace.from_path(arguments.workspace)
    objective = arguments.objective or input("Objective: ").strip()
    if not objective:
        raise ValueError("Objective must be non-empty")
    event_bus = InMemoryEventBus()

    def print_event(event: RuntimeEvent) -> None:
        print(
            json.dumps(
                {"event": event.name, "correlation_id": event.correlation_id, **event.payload},
                ensure_ascii=False,
                default=str,
            ),
            file=sys.stderr,
        )

    event_bus.subscribe(print_event)
    registry = ToolRegistry(_tools(arguments, event_bus))
    store = InMemoryToolResultStore()
    engine = PolicyPermissionEngine(
        PermissionPolicy(
            allow_workspace_writes=arguments.writes == "allow",
            allow_local_execution=arguments.execution == "allow",
        )
    )
    executor = ToolExecutor(registry, engine, store, event_bus, prompt=ConsolePermissionPrompt())
    provider = OpenAICompatibleModelProvider(
        arguments.base_url,
        arguments.model,
        api_key=os.getenv("ATHENA_API_KEY"),
    )
    loop = AgentLoop(
        provider,
        registry,
        executor,
        ContextBuilder(workspace),
        event_bus,
        config=AgentLoopConfig(
            max_iterations=arguments.max_iterations,
            session_timeout_seconds=arguments.timeout,
        ),
    )
    cancellation = source or CancellationSource()
    _install_cancellation(cancellation)
    result = await loop.run(objective, workspace, cancellation.token)
    if result.answer:
        print(result.answer)
    if result.status is AgentRunStatus.COMPLETED:
        return 0
    return 130 if result.status is AgentRunStatus.CANCELLED else 1


def main() -> int:
    arguments = _parser().parse_args()
    try:
        return asyncio.run(_run(arguments))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
