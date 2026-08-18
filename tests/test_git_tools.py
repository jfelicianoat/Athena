from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from athena.cancellation import CancellationSource
from athena.errors import PermissionDeniedError, ToolValidationError
from athena.events import EventName, InMemoryEventBus, RuntimeEvent
from athena.git_tools import (
    GitCommitTool,
    GitDiffTool,
    GitLogTool,
    GitShowTool,
    GitStatusTool,
    git_read_tools,
)
from athena.models import ModelToolCall
from athena.mutation_tools import workspace_mutation_tools
from athena.permissions import PermissionDecision, PolicyPermissionEngine, RiskTier
from athena.process_tools import BashTool, CommandPolicy, parse_command
from athena.registry import ToolRegistry
from athena.repository_tools import repository_read_tools
from athena.stores import InMemoryToolResultStore
from athena.testing import ScriptedPermissionPrompt
from athena.tool_executor import ToolExecutor
from athena.tools import ToolContext
from athena.workspace import Workspace


def _run_git(root: Path, *arguments: str) -> None:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments], capture_output=True, check=False, text=True
    )
    if completed.returncode != 0:
        pytest.fail(f"git {' '.join(arguments)} failed: {completed.stderr}")


def _repository(root: Path) -> Path:
    _run_git(root, "init", "-b", "main")
    _run_git(root, "config", "user.email", "athena@example.invalid")
    _run_git(root, "config", "user.name", "Athena Test")
    (root / "tracked.txt").write_text("first\nsecond\n", encoding="utf-8")
    _run_git(root, "add", "tracked.txt")
    _run_git(root, "commit", "-m", "initial commit")
    return root


def _context(root: Path) -> ToolContext:
    return ToolContext("session", Workspace.from_path(root), "call-1")


def _runtime(
    root: Path, answers: tuple[PermissionDecision, ...] = ()
) -> tuple[ToolExecutor, Workspace, ScriptedPermissionPrompt, list[RuntimeEvent], ToolRegistry]:
    bus = InMemoryEventBus()
    events: list[RuntimeEvent] = []
    bus.subscribe(events.append)
    registry = ToolRegistry(
        (
            *repository_read_tools(),
            *workspace_mutation_tools(bus),
            *git_read_tools(),
            GitCommitTool(),
            BashTool(event_bus=bus),
        )
    )
    prompt = ScriptedPermissionPrompt(answers)
    executor = ToolExecutor(
        registry, PolicyPermissionEngine(), InMemoryToolResultStore(), bus, prompt=prompt
    )
    return executor, Workspace.from_path(root), prompt, events, registry


def test_git_status_reports_a_dirty_workspace(tmp_path: Path) -> None:
    root = _repository(tmp_path)

    async def scenario() -> None:
        context = _context(root)
        clean = await GitStatusTool().execute(context, {}, CancellationSource().token)
        assert isinstance(clean.output, dict)
        assert clean.output["clean"] is True
        assert clean.output["changed_files"] == 0

        (root / "tracked.txt").write_text("first\nCHANGED\n", encoding="utf-8")
        (root / "untracked.txt").write_text("new", encoding="utf-8")

        dirty = await GitStatusTool().execute(context, {}, CancellationSource().token)
        assert isinstance(dirty.output, dict)
        assert dirty.output["clean"] is False
        assert dirty.output["changed_files"] == 2
        assert dirty.output["branch"] == "main"

    asyncio.run(scenario())


def test_git_diff_shows_the_edit_that_athena_made(tmp_path: Path) -> None:
    root = _repository(tmp_path)

    async def scenario() -> None:
        executor, workspace, _, _, _ = _runtime(root, (PermissionDecision.ALLOW,))
        await executor.execute(
            ModelToolCall(
                "e1",
                "edit_file",
                {"path": "tracked.txt", "old_string": "second", "new_string": "SECOND"},
            ),
            session_id="s",
            workspace=workspace,
            cancellation=CancellationSource().token,
        )

        diff = await GitDiffTool().execute(_context(root), {}, CancellationSource().token)

        assert isinstance(diff.output, dict)
        assert diff.output["empty"] is False
        patch = str(diff.output["diff"])
        assert "-second" in patch
        assert "+SECOND" in patch

    asyncio.run(scenario())


def test_git_log_and_show_read_local_history(tmp_path: Path) -> None:
    root = _repository(tmp_path)

    async def scenario() -> None:
        context = _context(root)
        log = await GitLogTool().execute(context, {"max_entries": 5}, CancellationSource().token)
        assert isinstance(log.output, dict)
        entries = log.output["entries"]
        assert isinstance(entries, list)
        assert len(entries) == 1
        assert "initial commit" in str(entries[0])

        shown = await GitShowTool().execute(
            context, {"revision": "HEAD"}, CancellationSource().token
        )
        assert isinstance(shown.output, dict)
        assert "initial commit" in str(shown.output["content"])

    asyncio.run(scenario())


def test_git_show_rejects_an_option_shaped_revision(tmp_path: Path) -> None:
    root = _repository(tmp_path)

    with pytest.raises(ToolValidationError):
        GitShowTool().validate({"revision": "--upload-pack=evil"})
    assert GitShowTool().validate({"revision": "HEAD~1"})["revision"] == "HEAD~1"
    assert root.exists()


def test_commit_is_always_an_ask_and_a_rejection_records_nothing(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "tracked.txt").write_text("first\nCHANGED\n", encoding="utf-8")

    async def scenario() -> None:
        executor, workspace, prompt, events, _ = _runtime(root, (PermissionDecision.DENY,))

        with pytest.raises(PermissionDeniedError):
            await executor.execute(
                ModelToolCall("c1", "git_commit", {"message": "attempt", "paths": ["tracked.txt"]}),
                session_id="s",
                workspace=workspace,
                cancellation=CancellationSource().token,
            )

        assert len(prompt.requests) == 1
        assert prompt.requests[0].tier is RiskTier.R3_EXTERNAL_OR_IRREVERSIBLE
        resolved = [
            event.payload for event in events if event.name is EventName.PERMISSION_RESOLVED
        ]
        assert resolved[-1]["decision"] == "deny"
        assert resolved[-1]["asked"] is True

        log = await GitLogTool().execute(
            _context(root), {"max_entries": 5}, CancellationSource().token
        )
        assert isinstance(log.output, dict)
        assert len(list(log.output["entries"] or ())) == 1

    asyncio.run(scenario())


def test_commit_records_history_only_after_an_approval(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "tracked.txt").write_text("first\nCHANGED\n", encoding="utf-8")

    async def scenario() -> None:
        executor, workspace, prompt, _, _ = _runtime(root, (PermissionDecision.ALLOW,))

        result = await executor.execute(
            ModelToolCall("c1", "git_commit", {"message": "approved", "paths": ["tracked.txt"]}),
            session_id="s",
            workspace=workspace,
            cancellation=CancellationSource().token,
        )

        assert isinstance(result.output, dict)
        assert result.output["committed"] is True
        assert len(prompt.requests) == 1
        log = await GitLogTool().execute(
            _context(root), {"max_entries": 5}, CancellationSource().token
        )
        assert isinstance(log.output, dict)
        assert any("approved" in str(entry) for entry in log.output["entries"] or ())

    asyncio.run(scenario())


def test_push_is_neither_a_tool_nor_an_allowed_command(tmp_path: Path) -> None:
    root = _repository(tmp_path)

    async def scenario() -> None:
        executor, workspace, prompt, events, registry = _runtime(root, (PermissionDecision.ALLOW,))

        names = registry.names()
        for forbidden in ("git_push", "git_merge", "git_pull", "git_rebase", "deploy"):
            assert forbidden not in names

        for command in ("git push origin main", "git push --force", "git merge other"):
            with pytest.raises(PermissionDeniedError):
                await executor.execute(
                    ModelToolCall(f"b-{command}", "bash", {"command": command}),
                    session_id="s",
                    workspace=workspace,
                    cancellation=CancellationSource().token,
                )

        decisions = [
            event.payload["decision"]
            for event in events
            if event.name is EventName.PERMISSION_RESOLVED
        ]
        assert decisions == ["deny", "deny", "deny"]
        assert prompt.requests == [], "A forbidden action must never reach a human prompt"

    asyncio.run(scenario())


def test_forbidden_git_subcommands_are_classified_as_r4() -> None:
    policy = CommandPolicy()
    for command in (
        "git push",
        "git pull",
        "git fetch",
        "git merge main",
        "git rebase main",
        "git reset --hard",
        "git clean -fd",
    ):
        classification = policy.classify(parse_command(command), ".")
        assert classification.tier is RiskTier.R4_FORBIDDEN, command
