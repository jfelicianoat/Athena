"""Minimal development CLI that assembles and observes the runtime."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from athena.adapters import OpenAICompatibleModelProvider
from athena.agent_loop import AgentLoop, AgentLoopConfig, AgentRunStatus
from athena.cancellation import CancellationSource
from athena.context import ContextBuilder
from athena.events import InMemoryEventBus, RuntimeEvent
from athena.permissions import ReadOnlyPermissionEngine
from athena.registry import ToolRegistry
from athena.repository_tools import repository_read_tools
from athena.stores import InMemoryToolResultStore
from athena.tool_executor import ToolExecutor
from athena.workspace import Workspace


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Investigate a repository with Athena H1")
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--objective", "-o")
    parser.add_argument(
        "--base-url",
        default=os.getenv("ATHENA_BASE_URL", "http://localhost:1234/v1"),
    )
    parser.add_argument("--model", default=os.getenv("ATHENA_MODEL", "local-model"))
    parser.add_argument("--max-iterations", type=int, default=12)
    parser.add_argument("--timeout", type=float, default=120.0)
    return parser


async def _run(arguments: argparse.Namespace) -> int:
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
            ),
            file=sys.stderr,
        )

    event_bus.subscribe(print_event)
    registry = ToolRegistry(repository_read_tools())
    store = InMemoryToolResultStore()
    executor = ToolExecutor(registry, ReadOnlyPermissionEngine(), store, event_bus)
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
    result = await loop.run(objective, workspace, CancellationSource().token)
    if result.answer:
        print(result.answer)
    return 0 if result.status is AgentRunStatus.COMPLETED else 1


def main() -> int:
    arguments = _parser().parse_args()
    try:
        return asyncio.run(_run(arguments))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
