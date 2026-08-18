from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path

import pytest

from athena.cancellation import CancellationSource
from athena.context import ContextBuilder, ContextLimits
from athena.errors import WorkspaceBoundaryError
from athena.events import EventName, InMemoryEventBus, RuntimeEvent
from athena.logging import JsonFormatter
from athena.models import ModelToolCall
from athena.permissions import ReadOnlyPermissionEngine
from athena.registry import ToolRegistry
from athena.repository_tools import repository_read_tools
from athena.stores import InMemoryToolResultStore
from athena.tool_executor import ToolExecutor
from athena.workspace import Workspace


def _executor(root: Path) -> tuple[ToolExecutor, Workspace, InMemoryToolResultStore]:
    workspace = Workspace.from_path(root, "workspace")
    registry = ToolRegistry(repository_read_tools())
    store = InMemoryToolResultStore()
    executor = ToolExecutor(registry, ReadOnlyPermissionEngine(), store, InMemoryEventBus())
    return executor, workspace, store


def test_path_outside_workspace_is_rejected(tmp_path: Path) -> None:
    workspace = Workspace.from_path(tmp_path)

    with pytest.raises(WorkspaceBoundaryError):
        workspace.resolve("../outside.txt", must_exist=False)
    with pytest.raises(WorkspaceBoundaryError):
        workspace.resolve(Path(tmp_path.anchor) / "outside.txt", must_exist=False)


def test_workspace_escape_resolves_permission_as_deny(tmp_path: Path) -> None:
    async def scenario() -> None:
        executor, workspace, _ = _executor(tmp_path)
        events: list[RuntimeEvent] = []
        executor.event_bus.subscribe(events.append)

        with pytest.raises(WorkspaceBoundaryError):
            await executor.execute(
                ModelToolCall("escape-1", "read_file", {"path": "../outside.txt"}),
                session_id="session",
                workspace=workspace,
                cancellation=CancellationSource().token,
            )

        decisions = [
            event.payload["decision"]
            for event in events
            if event.name is EventName.PERMISSION_RESOLVED
        ]
        assert decisions == ["deny"]

    asyncio.run(scenario())


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    link = tmp_path / "escape"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError:
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            pytest.fail("Test environment cannot create a symlink or directory junction")
    workspace = Workspace.from_path(tmp_path)

    with pytest.raises(WorkspaceBoundaryError):
        workspace.resolve("escape/secret.txt")


def test_large_tool_result_is_externalized_and_retrievable(tmp_path: Path) -> None:
    async def scenario() -> None:
        content = "x" * 20_000
        (tmp_path / "large.txt").write_text(content, encoding="utf-8")
        executor, workspace, store = _executor(tmp_path)
        source = CancellationSource()

        result = await executor.execute(
            ModelToolCall("large-1", "read_file", {"path": "large.txt"}),
            session_id="session",
            workspace=workspace,
            cancellation=source.token,
        )

        assert result.call_id == "large-1"
        assert result.reference is not None
        assert result.reference.uri.startswith("athena-result://")
        stored = await store.get(result.reference, source.token)
        assert content in stored
        assert isinstance(result.output, dict)
        assert result.output["externalized"] is True
        assert content not in json.dumps(result.output)

    asyncio.run(scenario())


def test_context_builder_resolves_agents_root_to_leaf_and_bounds_status(tmp_path: Path) -> None:
    async def scenario() -> None:
        (tmp_path / "AGENTS.md").write_text("root rule", encoding="utf-8")
        nested = tmp_path / "src" / "feature"
        nested.mkdir(parents=True)
        (tmp_path / "src" / "AGENTS.md").write_text("src rule", encoding="utf-8")
        (nested / "code.py").write_text("pass", encoding="utf-8")
        workspace = Workspace.from_path(tmp_path)
        builder = ContextBuilder(workspace, limits=ContextLimits(git_status_chars=10))

        project = await builder.inspect_project(
            CancellationSource().token,
            ("src/feature/code.py",),
        )

        assert [path for path, _ in project.instructions] == ["AGENTS.md", "src/AGENTS.md"]
        assert [content for _, content in project.instructions] == ["root rule", "src rule"]

    asyncio.run(scenario())


def test_context_builder_collects_bounded_git_context(tmp_path: Path) -> None:
    async def scenario() -> None:
        subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "athena@example.invalid"],
            cwd=tmp_path,
            check=True,
        )
        subprocess.run(["git", "config", "user.name", "Athena Test"], cwd=tmp_path, check=True)
        (tmp_path / "tracked.txt").write_text("tracked", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )
        for index in range(10):
            (tmp_path / f"untracked-{index}.txt").write_text("x", encoding="utf-8")
        builder = ContextBuilder(
            Workspace.from_path(tmp_path),
            limits=ContextLimits(git_status_chars=30, recent_log_entries=2),
        )

        project = await builder.inspect_project(CancellationSource().token)

        assert project.git["branch"] == "main"
        assert project.git["default_branch"] == "main"
        assert project.git["status_truncated"] is True
        assert "[truncated]" in str(project.git["status"])
        assert project.git["recent_log"] == ["initial"] or "initial" in str(
            project.git["recent_log"]
        )

    asyncio.run(scenario())


def test_events_and_logs_redact_secrets() -> None:
    async def event_scenario() -> None:
        bus = InMemoryEventBus()
        received: list[RuntimeEvent] = []
        bus.subscribe(received.append)
        await bus.publish(
            RuntimeEvent(
                EventName.MODEL_FAILED,
                "session",
                {"api_key": "super-secret", "message": "Bearer abc.def.ghi"},
            )
        )

        assert received[0].payload["api_key"] == "[REDACTED]"
        assert received[0].payload["message"] == "[REDACTED]"

    asyncio.run(event_scenario())
    record = logging.LogRecord(
        "athena.test",
        logging.INFO,
        __file__,
        1,
        "token=my-secret-token",
        (),
        None,
    )
    formatted = json.loads(JsonFormatter().format(record))
    assert formatted["message"] == "token=[REDACTED]"
