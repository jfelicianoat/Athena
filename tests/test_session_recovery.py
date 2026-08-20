from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from athena.agent_loop import AgentLoop, AgentLoopConfig, AgentRunStatus
from athena.cancellation import CancellationSource, CancellationToken
from athena.context import ContextBuilder
from athena.events import EventName, InMemoryEventBus, ModelEvent, RuntimeEvent
from athena.models import (
    ModelCapabilities,
    ModelHealth,
    ModelHealthStatus,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ModelToolCall,
)
from athena.mutation_tools import workspace_mutation_tools
from athena.permissions import PermissionDecision, PermissionPolicy, PolicyPermissionEngine
from athena.registry import ToolRegistry
from athena.repository_tools import repository_read_tools
from athena.session_store import SessionStoreError, SqliteSessionStore
from athena.state import AgentStatus
from athena.stores import SqliteToolResultStore
from athena.testing import ScriptedPermissionPrompt
from athena.tool_executor import ToolExecutor
from athena.verification import (
    CommandVerificationPolicy,
    VerificationPlanner,
    VerificationStatus,
)
from athena.workspace import Workspace

BROKEN = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
TEST = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def _git(root: Path, *arguments: str) -> None:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments], capture_output=True, check=False, text=True
    )
    if completed.returncode != 0:
        pytest.fail(f"git {' '.join(arguments)} failed: {completed.stderr}")


def _sandbox(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    command = f'"{sys.executable}" -m pytest -q'
    (root / "calc.py").write_text(BROKEN, encoding="utf-8")
    (root / "test_calc.py").write_text(TEST, encoding="utf-8")
    (root / "AGENTS.md").write_text(
        f"# Sandbox\n\n## Verification\n\n```\n{command}\n```\n", encoding="utf-8"
    )
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "athena@example.invalid")
    _git(root, "config", "user.name", "Athena Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    return root


class _EditThenHang(ModelProvider):
    """Does real work, then stops responding — the shape of a process that dies."""

    def __init__(self) -> None:
        self.edited = asyncio.Event()
        self.calls = 0

    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        del request
        cancellation.raise_if_cancelled()
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                "",
                "scripted",
                "tool_calls",
                tool_calls=(
                    ModelToolCall(
                        "fix-1",
                        "edit_file",
                        {"path": "calc.py", "old_string": "a - b", "new_string": "a + b"},
                    ),
                ),
            )
        self.edited.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def stream(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> AsyncIterator[ModelEvent]:
        del request, cancellation
        if False:
            yield ModelEvent(EventName.MODEL_COMPLETED, "never")

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(False, True, False)

    async def health(self, cancellation: CancellationToken) -> ModelHealth:
        cancellation.raise_if_cancelled()
        return ModelHealth(ModelHealthStatus.HEALTHY)


class _RecordingProvider(ModelProvider):
    """Answers immediately and records exactly what context it was given."""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.requests: list[ModelRequest] = []

    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        cancellation.raise_if_cancelled()
        self.requests.append(request)
        return ModelResponse(self.answer, "scripted", "stop")

    async def stream(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> AsyncIterator[ModelEvent]:
        del request, cancellation
        if False:
            yield ModelEvent(EventName.MODEL_COMPLETED, "never")

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(False, True, False)

    async def health(self, cancellation: CancellationToken) -> ModelHealth:
        cancellation.raise_if_cancelled()
        return ModelHealth(ModelHealthStatus.HEALTHY)


def _build(
    root: Path, provider: ModelProvider, database: Path, results: Path
) -> tuple[AgentLoop, Workspace, list[RuntimeEvent], SqliteSessionStore]:
    bus = InMemoryEventBus()
    events: list[RuntimeEvent] = []
    bus.subscribe(events.append)
    workspace = Workspace.from_path(root)
    registry = ToolRegistry((*repository_read_tools(), *workspace_mutation_tools(bus)))
    executor = ToolExecutor(
        registry,
        PolicyPermissionEngine(PermissionPolicy(allow_workspace_writes=True)),
        SqliteToolResultStore(results),
        bus,
        prompt=ScriptedPermissionPrompt((PermissionDecision.ALLOW,) * 4),
    )
    store = SqliteSessionStore(database)
    loop = AgentLoop(
        provider,
        registry,
        executor,
        ContextBuilder(workspace),
        bus,
        verification=CommandVerificationPolicy(VerificationPlanner(workspace), event_bus=bus),
        session_store=store,
        config=AgentLoopConfig(max_iterations=8, session_timeout_seconds=600.0),
    )
    return loop, workspace, events, store


def test_an_interrupted_session_resumes_from_stored_state_alone(tmp_path: Path) -> None:
    """The acceptance case: continue after a crash, with no transcript and no CLI."""
    root = _sandbox(tmp_path / "repo")
    database = tmp_path / "sessions.db"
    results = tmp_path / "results.db"

    async def scenario() -> None:
        # --- process 1: does the work, then dies mid-run -------------------
        provider = _EditThenHang()
        loop, workspace, events, _ = _build(root, provider, database, results)
        task = asyncio.create_task(loop.run("Fix calc.add", workspace, CancellationSource().token))
        await asyncio.wait_for(provider.edited.wait(), timeout=60)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert (root / "calc.py").read_text(encoding="utf-8") == FIXED
        session_id = next(
            event.session_id for event in events if event.name is EventName.AGENT_STARTED
        )

        # --- process 2: a cold start on the same database ------------------
        resumed_provider = _RecordingProvider("calc.add was already repaired.")
        resumed_loop, workspace, resumed_events, store = _build(
            root, resumed_provider, database, results
        )
        interrupted = await store.mark_interrupted()
        assert session_id in interrupted

        pending = await store.load(session_id)
        assert pending is not None
        assert pending.status is AgentStatus.RECOVERY_PENDING
        assert pending.working_memory.files_modified == ("calc.py",)

        result = await resumed_loop.resume(session_id, workspace, CancellationSource().token)

        # The run finished, and it finished because verification said so.
        assert result.status is AgentRunStatus.COMPLETED
        assert result.verification is not None
        assert result.verification.status is VerificationStatus.PASSED

        # It continued the same session, not a new one.
        assert result.session.session_id == session_id
        assert any(e.name is EventName.SESSION_RESUMED for e in resumed_events)

        # And it did so without replaying a single message of the old conversation.
        first_request = resumed_provider.requests[0]
        assert [message.role for message in first_request.messages] == [
            ModelRole.SYSTEM,
            ModelRole.USER,
        ]
        system = first_request.messages[0].content
        assert "calc.py" in system, "working memory must carry the state the transcript held"
        assert "fix-1" not in system, "no tool call from the previous process is replayed"

        stored = await store.load(session_id)
        assert stored is not None
        assert stored.status is AgentStatus.COMPLETED

    asyncio.run(scenario())


def test_resume_refuses_a_session_that_is_not_pending_recovery(tmp_path: Path) -> None:
    root = _sandbox(tmp_path / "repo")
    database = tmp_path / "sessions.db"
    results = tmp_path / "results.db"

    async def scenario() -> None:
        provider = _RecordingProvider("done")
        loop, workspace, events, _ = _build(root, provider, database, results)
        await loop.run("Do nothing", workspace, CancellationSource().token)
        session_id = next(
            event.session_id for event in events if event.name is EventName.AGENT_STARTED
        )

        with pytest.raises(SessionStoreError):
            await loop.resume(session_id, workspace, CancellationSource().token)

    asyncio.run(scenario())


def test_resume_reports_an_unknown_session(tmp_path: Path) -> None:
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        loop, workspace, _, _ = _build(
            root, _RecordingProvider("done"), tmp_path / "s.db", tmp_path / "r.db"
        )

        with pytest.raises(SessionStoreError):
            await loop.resume("does-not-exist", workspace, CancellationSource().token)

    asyncio.run(scenario())


def test_a_loop_without_a_store_cannot_resume(tmp_path: Path) -> None:
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        bus = InMemoryEventBus()
        workspace = Workspace.from_path(root)
        registry = ToolRegistry(repository_read_tools())
        loop = AgentLoop(
            _RecordingProvider("done"),
            registry,
            ToolExecutor(
                registry,
                PolicyPermissionEngine(),
                SqliteToolResultStore(tmp_path / "r.db"),
                bus,
            ),
            ContextBuilder(workspace),
            bus,
        )

        with pytest.raises(SessionStoreError):
            await loop.resume("any", workspace, CancellationSource().token)

    asyncio.run(scenario())


def test_a_completed_run_is_persisted_with_its_evidence(tmp_path: Path) -> None:
    root = _sandbox(tmp_path / "repo")
    (root / "calc.py").write_text(FIXED, encoding="utf-8")
    database = tmp_path / "sessions.db"

    async def scenario() -> None:
        provider = _RecordingProvider("Nothing to do; the suite is green.")
        loop, workspace, events, store = _build(root, provider, database, tmp_path / "r.db")

        result = await loop.run("Check the suite", workspace, CancellationSource().token)
        session_id = result.session.session_id

        assert result.status is AgentRunStatus.COMPLETED
        stored = await store.load(session_id)
        assert stored is not None
        assert stored.status is AgentStatus.COMPLETED
        assert stored.verification.get("status") == "passed"
        assert next(checkpoint.name for checkpoint in stored.checkpoints) == "started"
        assert any(e.name is EventName.SESSION_PERSISTED for e in events)

    asyncio.run(scenario())
