from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from athena.cancellation import CancellationSource
from athena.errors import PermissionDeniedError
from athena.events import EventName, InMemoryEventBus, RuntimeEvent
from athena.git_tools import GitCommitTool, git_read_tools
from athena.models import ModelToolCall
from athena.mutation_tools import workspace_mutation_tools
from athena.permissions import (
    PermissionDecision,
    PermissionPolicy,
    PermissionRequest,
    PolicyPermissionEngine,
    RiskLevel,
    RiskTier,
)
from athena.process_tools import BashTool
from athena.registry import ToolRegistry
from athena.repository_tools import repository_read_tools
from athena.stores import InMemoryToolResultStore
from athena.testing import ScriptedPermissionPrompt
from athena.tool_executor import ToolExecutor
from athena.workspace import Workspace


def _runtime(
    root: Path,
    *,
    answers: tuple[PermissionDecision, ...] = (),
    policy: PermissionPolicy | None = None,
) -> tuple[ToolExecutor, Workspace, ScriptedPermissionPrompt, list[RuntimeEvent]]:
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
        registry,
        PolicyPermissionEngine(policy),
        InMemoryToolResultStore(),
        bus,
        prompt=prompt,
    )
    return executor, Workspace.from_path(root), prompt, events


def _decisions(events: list[RuntimeEvent]) -> list[str]:
    return [
        str(event.payload["decision"])
        for event in events
        if event.name is EventName.PERMISSION_RESOLVED
    ]


def test_read_only_request_is_allowed_without_asking(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")

    async def scenario() -> None:
        executor, workspace, prompt, events = _runtime(tmp_path)

        result = await executor.execute(
            ModelToolCall("r1", "read_file", {"path": "a.txt"}),
            session_id="s",
            workspace=workspace,
            cancellation=CancellationSource().token,
        )

        assert isinstance(result.output, dict)
        assert result.output["content"] == "hello"
        assert _decisions(events) == ["allow"]
        assert prompt.requests == []

    asyncio.run(scenario())


def test_ask_then_approve_performs_the_write(tmp_path: Path) -> None:
    async def scenario() -> None:
        executor, workspace, prompt, events = _runtime(
            tmp_path, answers=(PermissionDecision.ALLOW,)
        )

        await executor.execute(
            ModelToolCall("w1", "write_file", {"path": "new.txt", "content": "created"}),
            session_id="s",
            workspace=workspace,
            cancellation=CancellationSource().token,
        )

        assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "created"
        assert _decisions(events) == ["allow"]
        assert len(prompt.requests) == 1
        asked = prompt.requests[0]
        assert asked.tier is RiskTier.R1_WORKSPACE_WRITE
        assert asked.action.startswith("create")
        assert asked.reason
        assert asked.possible_effects

    asyncio.run(scenario())


def test_ask_then_reject_leaves_the_workspace_untouched(tmp_path: Path) -> None:
    async def scenario() -> None:
        executor, workspace, prompt, events = _runtime(tmp_path, answers=(PermissionDecision.DENY,))

        with pytest.raises(PermissionDeniedError):
            await executor.execute(
                ModelToolCall("w1", "write_file", {"path": "new.txt", "content": "created"}),
                session_id="s",
                workspace=workspace,
                cancellation=CancellationSource().token,
            )

        assert not (tmp_path / "new.txt").exists()
        assert _decisions(events) == ["deny"]
        assert len(prompt.requests) == 1

    asyncio.run(scenario())


def test_forbidden_command_is_denied_without_reaching_the_interface(tmp_path: Path) -> None:
    async def scenario() -> None:
        executor, workspace, prompt, events = _runtime(
            tmp_path, answers=(PermissionDecision.ALLOW,)
        )

        with pytest.raises(PermissionDeniedError):
            await executor.execute(
                ModelToolCall("b1", "bash", {"command": "curl https://example.com"}),
                session_id="s",
                workspace=workspace,
                cancellation=CancellationSource().token,
            )

        assert _decisions(events) == ["deny"]
        assert prompt.requests == [], "R4 must never be offered to a human for approval"

    asyncio.run(scenario())


def test_granted_policy_allows_writes_without_a_prompt(tmp_path: Path) -> None:
    async def scenario() -> None:
        executor, workspace, prompt, events = _runtime(
            tmp_path, policy=PermissionPolicy(allow_workspace_writes=True)
        )

        await executor.execute(
            ModelToolCall("w1", "write_file", {"path": "new.txt", "content": "created"}),
            session_id="s",
            workspace=workspace,
            cancellation=CancellationSource().token,
        )

        assert _decisions(events) == ["allow"]
        assert prompt.requests == []

    asyncio.run(scenario())


def test_engine_refuses_a_request_that_understates_its_own_effects(tmp_path: Path) -> None:
    """A tool cannot reach the unconditional ALLOW tier while declaring side effects."""
    engine = PolicyPermissionEngine(PermissionPolicy(allow_workspace_writes=True))
    dishonest = PermissionRequest(
        tool_name="sneaky",
        operation="write",
        workspace=Workspace.from_path(tmp_path),
        risk=RiskLevel.LOW,
        tier=RiskTier.R0_READ_ONLY,
        is_read_only=False,
        is_destructive=True,
    )

    assert engine.decide(dishonest) is PermissionDecision.DENY


def test_destructive_requests_escalate_to_ask_even_when_writes_are_granted(
    tmp_path: Path,
) -> None:
    engine = PolicyPermissionEngine(
        PermissionPolicy(allow_workspace_writes=True, allow_local_execution=True)
    )
    request = PermissionRequest(
        tool_name="write_file",
        operation="write_file",
        workspace=Workspace.from_path(tmp_path),
        risk=RiskLevel.HIGH,
        tier=RiskTier.R1_WORKSPACE_WRITE,
        is_read_only=False,
        is_destructive=True,
    )

    assert engine.decide(request) is PermissionDecision.ASK


def test_exhausted_prompt_script_denies_rather_than_approving(tmp_path: Path) -> None:
    async def scenario() -> None:
        executor, workspace, _, events = _runtime(tmp_path, answers=())

        with pytest.raises(PermissionDeniedError):
            await executor.execute(
                ModelToolCall("w1", "write_file", {"path": "new.txt", "content": "x"}),
                session_id="s",
                workspace=workspace,
                cancellation=CancellationSource().token,
            )

        assert _decisions(events) == ["deny"]

    asyncio.run(scenario())
