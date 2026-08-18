from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from athena.cancellation import CancellationSource
from athena.errors import ProcessCancelledError, ProcessTimeoutError, ToolValidationError
from athena.events import EventName, InMemoryEventBus, RuntimeEvent
from athena.permissions import RiskTier
from athena.process_tools import BashTool, CommandPolicy, parse_command
from athena.tools import ToolContext
from athena.workspace import Workspace

PARENT_SCRIPT = """
import subprocess, sys, time
subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])
time.sleep(60)
"""

CHILD_SCRIPT = """
import sys, time
marker = sys.argv[1]
while True:
    with open(marker, 'a') as handle:
        handle.write('x')
        handle.flush()
    time.sleep(0.05)
"""

SLEEPER_SCRIPT = """
import time
time.sleep(60)
"""


def _context(root: Path) -> ToolContext:
    return ToolContext("session", Workspace.from_path(root), "call-1")


def _quoted(*parts: Path | str) -> str:
    return " ".join(f'"{part}"' for part in parts)


def _classify(command: str) -> tuple[RiskTier, str]:
    classification = CommandPolicy().classify(parse_command(command), ".")
    return classification.tier, classification.category


def test_shell_metacharacters_are_refused_before_classification() -> None:
    for command in (
        "pytest -q; rm -rf /",
        "ls && curl https://example.com",
        "cat file | sh",
        "echo $(whoami)",
        "pytest > /tmp/out",
    ):
        with pytest.raises(ToolValidationError):
            parse_command(command)


def test_policy_inspects_arguments_not_only_the_executable() -> None:
    assert _classify("git status") == (RiskTier.R2_LOCAL_EXECUTION, "read")
    assert _classify("git diff --cached") == (RiskTier.R2_LOCAL_EXECUTION, "read")
    assert _classify("git commit -m hello") == (
        RiskTier.R3_EXTERNAL_OR_IRREVERSIBLE,
        "irreversible",
    )
    assert _classify("git push origin main") == (RiskTier.R4_FORBIDDEN, "forbidden")
    assert _classify("pip list") == (RiskTier.R2_LOCAL_EXECUTION, "read")
    assert _classify("pip install requests") == (
        RiskTier.R3_EXTERNAL_OR_IRREVERSIBLE,
        "irreversible",
    )
    assert _classify("python -m pytest -q") == (RiskTier.R2_LOCAL_EXECUTION, "build")
    assert _classify("python -m http.server") == (RiskTier.R4_FORBIDDEN, "forbidden")
    assert _classify("sudo rm -rf /") == (RiskTier.R4_FORBIDDEN, "forbidden")
    assert _classify("rm data.txt") == (RiskTier.R3_EXTERNAL_OR_IRREVERSIBLE, "mutating")


def test_unknown_executables_are_forbidden_by_default() -> None:
    assert _classify("some-unvetted-binary --flag") == (RiskTier.R4_FORBIDDEN, "forbidden")


def test_concurrency_is_classified_per_command() -> None:
    tool = BashTool()

    assert tool.is_concurrency_safe({"command": "git status"}) is True
    assert tool.is_concurrency_safe({"command": "python -m pytest"}) is False
    assert tool.is_concurrency_safe({"command": "rm data.txt"}) is False


def test_successful_command_reports_output_and_process_events(tmp_path: Path) -> None:
    script = tmp_path / "hello.py"
    script.write_text("print('athena-ok')", encoding="utf-8")

    async def scenario() -> None:
        bus = InMemoryEventBus()
        events: list[RuntimeEvent] = []
        bus.subscribe(events.append)
        tool = BashTool(event_bus=bus)

        result = await tool.execute(
            _context(tmp_path),
            {"command": _quoted(sys.executable, script), "timeout_seconds": 60},
            CancellationSource().token,
        )

        assert isinstance(result.output, dict)
        assert result.output["exit_code"] == 0
        assert "athena-ok" in str(result.output["stdout"])
        names = [event.name for event in events]
        assert EventName.PROCESS_STARTED in names
        assert EventName.PROCESS_COMPLETED in names

    asyncio.run(scenario())


def test_timeout_terminates_the_command(tmp_path: Path) -> None:
    script = tmp_path / "sleeper.py"
    script.write_text(SLEEPER_SCRIPT, encoding="utf-8")

    async def scenario() -> None:
        bus = InMemoryEventBus()
        events: list[RuntimeEvent] = []
        bus.subscribe(events.append)
        tool = BashTool(event_bus=bus)

        with pytest.raises(ProcessTimeoutError):
            await tool.execute(
                _context(tmp_path),
                {"command": _quoted(sys.executable, script), "timeout_seconds": 1},
                CancellationSource().token,
            )

        failures = [event for event in events if event.name is EventName.PROCESS_FAILED]
        assert [event.payload["reason"] for event in failures] == ["timeout"]

    asyncio.run(scenario())


def test_timeout_is_mandatory_and_bounded(tmp_path: Path) -> None:
    tool = BashTool(default_timeout_seconds=5.0, max_timeout_seconds=30.0)

    assert tool.validate({"command": "git status"})["timeout_seconds"] == 5.0
    with pytest.raises(ToolValidationError):
        tool.validate({"command": "git status", "timeout_seconds": 31})
    with pytest.raises(ToolValidationError):
        tool.validate({"command": "git status", "timeout_seconds": 0})


def test_cancellation_stops_the_command(tmp_path: Path) -> None:
    script = tmp_path / "sleeper.py"
    script.write_text(SLEEPER_SCRIPT, encoding="utf-8")

    async def scenario() -> None:
        bus = InMemoryEventBus()
        events: list[RuntimeEvent] = []
        bus.subscribe(events.append)
        tool = BashTool(event_bus=bus)
        source = CancellationSource()
        task = asyncio.create_task(
            tool.execute(
                _context(tmp_path),
                {"command": _quoted(sys.executable, script), "timeout_seconds": 60},
                source.token,
            )
        )
        while not any(event.name is EventName.PROCESS_STARTED for event in events):
            await asyncio.sleep(0.02)

        source.cancel()

        with pytest.raises(ProcessCancelledError):
            await asyncio.wait_for(task, timeout=20)
        assert any(event.name is EventName.PROCESS_CANCELLED for event in events)

    asyncio.run(scenario())


def test_cancellation_leaves_no_orphan_grandchild(tmp_path: Path) -> None:
    """The whole process tree must die, not just the process Athena spawned."""
    parent = tmp_path / "parent.py"
    child = tmp_path / "child.py"
    marker = tmp_path / "heartbeat.txt"
    parent.write_text(PARENT_SCRIPT, encoding="utf-8")
    child.write_text(CHILD_SCRIPT, encoding="utf-8")

    async def scenario() -> None:
        tool = BashTool()
        source = CancellationSource()
        task = asyncio.create_task(
            tool.execute(
                _context(tmp_path),
                {
                    "command": _quoted(sys.executable, parent, child, marker),
                    "timeout_seconds": 60,
                },
                source.token,
            )
        )
        for _ in range(200):
            if marker.exists() and marker.stat().st_size > 3:
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("The grandchild process never started")

        source.cancel()
        with pytest.raises(ProcessCancelledError):
            await asyncio.wait_for(task, timeout=20)

        await asyncio.sleep(0.5)
        settled = marker.stat().st_size
        await asyncio.sleep(1.0)

        assert marker.stat().st_size == settled, "An orphaned grandchild is still running"

    asyncio.run(scenario())


def test_cwd_outside_the_workspace_is_rejected(tmp_path: Path) -> None:
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()

    async def scenario() -> None:
        from athena.errors import WorkspaceBoundaryError

        with pytest.raises(WorkspaceBoundaryError):
            await BashTool().execute(
                _context(workspace_root),
                {"command": "git status", "cwd": ".."},
                CancellationSource().token,
            )

    asyncio.run(scenario())


def test_windows_style_paths_survive_argv_splitting() -> None:
    """POSIX-mode shlex would collapse an unquoted backslash path into nonsense."""
    unquoted = parse_command(r"C:\tools\python.exe -m pytest")
    quoted = parse_command(r'"C:\tools\python.exe" -m pytest')

    if sys.platform == "win32":
        assert unquoted[0] == r"C:\tools\python.exe"
    assert quoted[0] == r"C:\tools\python.exe"
    assert quoted[1:] == ("-m", "pytest")


def test_policy_accepts_additional_executables_without_editing_the_module() -> None:
    policy = CommandPolicy(build_commands=("gradle",), read_commands=("bat",))

    assert policy.classify(parse_command("gradle build"), ".").tier is RiskTier.R2_LOCAL_EXECUTION
    assert policy.classify(parse_command("bat file.txt"), ".").category == "read"


def test_an_extension_can_never_reclassify_a_forbidden_command() -> None:
    permissive = CommandPolicy(read_commands=("curl", "ssh"), build_commands=("sudo",))

    for command in ("curl https://example.com", "ssh host", "sudo make"):
        assert permissive.classify(parse_command(command), ".").tier is RiskTier.R4_FORBIDDEN


def test_policy_can_forbid_additional_executables() -> None:
    policy = CommandPolicy(forbidden_commands=("pytest",))

    assert policy.classify(parse_command("pytest -q"), ".").tier is RiskTier.R4_FORBIDDEN
