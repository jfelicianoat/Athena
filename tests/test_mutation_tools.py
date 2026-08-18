from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from athena.cancellation import CancellationSource
from athena.errors import ToolValidationError, WorkspaceBoundaryError
from athena.events import EventName, InMemoryEventBus, RuntimeEvent
from athena.mutation_tools import EditFileTool, WriteFileTool
from athena.tools import ToolContext
from athena.workspace import Workspace


def _context(root: Path, bus: InMemoryEventBus) -> tuple[ToolContext, list[RuntimeEvent]]:
    events: list[RuntimeEvent] = []
    bus.subscribe(events.append)
    return ToolContext("session", Workspace.from_path(root), "call-1"), events


def test_edit_produces_the_expected_diff_and_file_changed_event(tmp_path: Path) -> None:
    target = tmp_path / "module.py"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    async def scenario() -> None:
        bus = InMemoryEventBus()
        context, events = _context(tmp_path, bus)
        tool = EditFileTool(bus)

        result = await tool.execute(
            context,
            {"path": "module.py", "old_string": "beta", "new_string": "delta"},
            CancellationSource().token,
        )

        assert target.read_text(encoding="utf-8") == "alpha\ndelta\ngamma\n"
        assert isinstance(result.output, dict)
        diff = result.output["diff"]
        assert isinstance(diff, str)
        assert "-beta" in diff
        assert "+delta" in diff
        assert "a/module.py" in diff and "b/module.py" in diff
        assert result.output["replacements"] == 1
        changed = [event for event in events if event.name is EventName.FILE_CHANGED]
        assert len(changed) == 1
        assert changed[0].payload["path"] == "module.py"
        assert changed[0].payload["change"] == "modified"
        assert changed[0].correlation_id == "call-1"

    asyncio.run(scenario())


def test_edit_refuses_an_ambiguous_match_without_writing(tmp_path: Path) -> None:
    target = tmp_path / "module.py"
    original = "x = 1\nx = 1\n"
    target.write_text(original, encoding="utf-8")

    async def scenario() -> None:
        bus = InMemoryEventBus()
        context, _ = _context(tmp_path, bus)

        with pytest.raises(ToolValidationError):
            await EditFileTool(bus).execute(
                context,
                {"path": "module.py", "old_string": "x = 1", "new_string": "x = 2"},
                CancellationSource().token,
            )

        assert target.read_text(encoding="utf-8") == original

    asyncio.run(scenario())


def test_write_refuses_to_overwrite_without_an_explicit_flag(tmp_path: Path) -> None:
    target = tmp_path / "keep.txt"
    target.write_text("important", encoding="utf-8")

    async def scenario() -> None:
        bus = InMemoryEventBus()
        context, _ = _context(tmp_path, bus)

        with pytest.raises(ToolValidationError):
            await WriteFileTool(bus).execute(
                context,
                {"path": "keep.txt", "content": "oops"},
                CancellationSource().token,
            )

        assert target.read_text(encoding="utf-8") == "important"

    asyncio.run(scenario())


def test_write_validation_rejects_an_accidentally_empty_payload(tmp_path: Path) -> None:
    with pytest.raises(ToolValidationError):
        WriteFileTool().validate({"path": "a.txt", "content": ""})

    assert (
        WriteFileTool().validate({"path": "a.txt", "content": "", "allow_empty": True})[
            "allow_empty"
        ]
        is True
    )


def test_a_truncating_rewrite_is_reported_as_destructive(tmp_path: Path) -> None:
    target = tmp_path / "big.py"
    target.write_text("line\n" * 400, encoding="utf-8")
    context = ToolContext("session", Workspace.from_path(tmp_path), "call-1")

    request = WriteFileTool().permission(
        context, {"path": "big.py", "content": "line\n", "overwrite": True}
    )

    assert request.is_destructive is True
    assert any("Discards" in effect for effect in request.possible_effects)


def test_path_traversal_write_is_rejected(tmp_path: Path) -> None:
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("original", encoding="utf-8")

    async def scenario() -> None:
        bus = InMemoryEventBus()
        context, _ = _context(workspace_root, bus)

        with pytest.raises(WorkspaceBoundaryError):
            await WriteFileTool(bus).execute(
                context,
                {"path": "../outside.txt", "content": "hijacked", "overwrite": True},
                CancellationSource().token,
            )

        assert outside.read_text(encoding="utf-8") == "original"

    asyncio.run(scenario())


def test_absolute_path_outside_the_workspace_is_rejected(tmp_path: Path) -> None:
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("original", encoding="utf-8")

    async def scenario() -> None:
        bus = InMemoryEventBus()
        context, _ = _context(workspace_root, bus)

        with pytest.raises(WorkspaceBoundaryError):
            await WriteFileTool(bus).execute(
                context,
                {"path": str(outside), "content": "hijacked", "overwrite": True},
                CancellationSource().token,
            )

        assert outside.read_text(encoding="utf-8") == "original"

    asyncio.run(scenario())


def test_symlink_escape_write_is_rejected(tmp_path: Path) -> None:
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("original", encoding="utf-8")
    link = workspace_root / "escape"
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

    async def scenario() -> None:
        bus = InMemoryEventBus()
        context, _ = _context(workspace_root, bus)

        with pytest.raises(WorkspaceBoundaryError):
            await WriteFileTool(bus).execute(
                context,
                {"path": "escape/secret.txt", "content": "hijacked", "overwrite": True},
                CancellationSource().token,
            )

        assert secret.read_text(encoding="utf-8") == "original"

    asyncio.run(scenario())


def test_write_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    async def scenario() -> None:
        bus = InMemoryEventBus()
        context, _ = _context(tmp_path, bus)

        await WriteFileTool(bus).execute(
            context,
            {"path": "created.txt", "content": "content"},
            CancellationSource().token,
        )

        leftovers = [item.name for item in tmp_path.iterdir() if "athena-tmp" in item.name]
        assert leftovers == []
        assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "content"

    asyncio.run(scenario())
