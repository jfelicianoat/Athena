from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest

from athena.cancellation import CancellationSource
from athena.errors import ToolResultUnavailableError
from athena.memory import WorkingMemory
from athena.session_store import (
    EventCheckpoint,
    InMemorySessionStore,
    SessionRecord,
    SqliteSessionStore,
)
from athena.state import AgentStatus
from athena.stores import SqliteToolResultStore
from athena.tools import ToolResultReference
from athena.working_state import PlanStep, RecordedError


def _memory() -> WorkingMemory:
    return (
        WorkingMemory(objective="Fix calc.add", constraints=("Do not delete tests",))
        .with_plan((PlanStep("Read calc.py"), PlanStep("Fix the operator")), current_step=1)
        .observing(files_examined=("calc.py", "test_calc.py"))
        .modifying(files_modified=("calc.py",))
        .ran("pytest -q")
        .noting(facts=("add subtracted",), decisions=("Restore +",))
        .failing(RecordedError("verification_failure", "1 test failing", "return_evidence"))
        .verified({"status": "failed", "summary": "1 test failing"})
    )


def _record(session_id: str = "s-1", status: AgentStatus = AgentStatus.RUNNING) -> SessionRecord:
    return SessionRecord(
        session_id=session_id,
        workspace_id="ws-1",
        status=status,
        working_memory=_memory(),
        tool_references=(ToolResultReference("key-1", "application/json", 40_000, "abc123"),),
        verification={"status": "failed"},
        checkpoints=(EventCheckpoint("started", {"iteration": 1}),),
    )


def _store(tmp_path: Path) -> SqliteSessionStore:
    return SqliteSessionStore(tmp_path / "athena" / "sessions.db")


# ------------------------------------------------------------------ round trip


def test_a_session_round_trips_through_sqlite(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        original = _record()

        await store.save(original)
        loaded = await store.load("s-1")

        assert loaded is not None
        assert loaded.degraded is False
        assert loaded.working_memory == original.working_memory
        assert loaded.tool_references == original.tool_references
        assert loaded.verification == {"status": "failed"}
        assert [c.name for c in loaded.checkpoints] == ["started"]
        assert loaded.status is AgentStatus.RUNNING

    asyncio.run(scenario())


def test_saving_the_same_session_twice_updates_it(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        await store.save(_record())
        await store.save(_record(status=AgentStatus.COMPLETED))

        sessions = await store.list_sessions()

        assert len(sessions) == 1
        assert sessions[0].status is AgentStatus.COMPLETED

    asyncio.run(scenario())


def test_an_unknown_session_is_absent_not_an_error(tmp_path: Path) -> None:
    async def scenario() -> None:
        assert await _store(tmp_path).load("nope") is None

    asyncio.run(scenario())


# ------------------------------------------------------------------ recovery


def test_a_live_session_becomes_recovery_pending_after_a_restart(tmp_path: Path) -> None:
    """The central rule: an interrupted run is never resurrected as completed."""
    database = tmp_path / "sessions.db"

    async def scenario() -> None:
        first_process = SqliteSessionStore(database)
        await first_process.save(_record("live-1", AgentStatus.RUNNING))
        await first_process.save(_record("live-2", AgentStatus.VERIFYING))
        await first_process.save(_record("live-3", AgentStatus.WAITING_PERMISSION))
        await first_process.save(_record("done-1", AgentStatus.COMPLETED))
        await first_process.save(_record("failed-1", AgentStatus.FAILED))

        # The process dies here. A new one opens the same database.
        restarted = SqliteSessionStore(database)
        interrupted = await restarted.mark_interrupted()

        assert set(interrupted) == {"live-1", "live-2", "live-3"}
        for session_id in interrupted:
            record = await restarted.load(session_id)
            assert record is not None
            assert record.status is AgentStatus.RECOVERY_PENDING
            assert record.resumable
        untouched = await restarted.load("done-1")
        assert untouched is not None
        assert untouched.status is AgentStatus.COMPLETED
        assert not untouched.resumable

    asyncio.run(scenario())


def test_recovery_preserves_everything_needed_to_continue(tmp_path: Path) -> None:
    database = tmp_path / "sessions.db"

    async def scenario() -> None:
        await SqliteSessionStore(database).save(_record("live-1", AgentStatus.RUNNING))

        restarted = SqliteSessionStore(database)
        await restarted.mark_interrupted()
        recovered = await restarted.load("live-1")

        assert recovered is not None
        memory = recovered.working_memory
        assert memory.objective == "Fix calc.add"
        assert memory.constraints == ("Do not delete tests",)
        assert memory.files_modified == ("calc.py",)
        assert memory.decisions == ("Restore +",)
        assert memory.errors[0].code == "verification_failure"
        assert recovered.tool_references[0].store_key == "key-1"

    asyncio.run(scenario())


def test_marking_interrupted_twice_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "sessions.db"

    async def scenario() -> None:
        store = SqliteSessionStore(database)
        await store.save(_record("live-1", AgentStatus.RUNNING))

        first = await store.mark_interrupted()
        second = await store.mark_interrupted()

        assert first == ("live-1",)
        assert second == ()

    asyncio.run(scenario())


def test_the_in_memory_store_has_the_same_recovery_semantics() -> None:
    async def scenario() -> None:
        store = InMemorySessionStore()
        await store.save(_record("live-1", AgentStatus.RUNNING))
        await store.save(_record("done-1", AgentStatus.COMPLETED))

        interrupted = await store.mark_interrupted()

        assert interrupted == ("live-1",)
        recovered = await store.load("live-1")
        assert recovered is not None and recovered.status is AgentStatus.RECOVERY_PENDING

    asyncio.run(scenario())


# ------------------------------------------------------------------ damaged data


def _corrupt(database: Path, session_id: str, column: str, value: str) -> None:
    connection = sqlite3.connect(database)
    connection.execute(
        f"UPDATE sessions SET {column} = ? WHERE session_id = ?", (value, session_id)
    )
    connection.commit()
    connection.close()


def test_a_session_with_unreadable_working_memory_is_degraded_not_lost(
    tmp_path: Path,
) -> None:
    database = tmp_path / "sessions.db"

    async def scenario() -> None:
        store = SqliteSessionStore(database)
        await store.save(_record("s-1", AgentStatus.RUNNING))
        _corrupt(database, "s-1", "working_memory", "{not valid json")

        loaded = await store.load("s-1")

        assert loaded is not None, "a damaged row must not disappear"
        assert loaded.degraded is True
        assert "unrecovered objective" in loaded.working_memory.objective
        assert loaded.working_memory.remaining_work != ()

    asyncio.run(scenario())


def test_an_incomplete_working_memory_is_reconstructed_safely(tmp_path: Path) -> None:
    database = tmp_path / "sessions.db"

    async def scenario() -> None:
        store = SqliteSessionStore(database)
        await store.save(_record("s-1", AgentStatus.RUNNING))
        _corrupt(database, "s-1", "working_memory", '{"facts": ["half a record"]}')

        loaded = await store.load("s-1")

        assert loaded is not None
        assert loaded.degraded is True

    asyncio.run(scenario())


def test_an_unknown_status_is_treated_as_needing_recovery(tmp_path: Path) -> None:
    database = tmp_path / "sessions.db"

    async def scenario() -> None:
        store = SqliteSessionStore(database)
        await store.save(_record("s-1", AgentStatus.RUNNING))
        _corrupt(database, "s-1", "status", "who_knows")

        loaded = await store.load("s-1")

        assert loaded is not None
        assert loaded.status is AgentStatus.RECOVERY_PENDING
        assert loaded.degraded is True

    asyncio.run(scenario())


def test_damaged_checkpoints_degrade_to_an_empty_trail(tmp_path: Path) -> None:
    database = tmp_path / "sessions.db"

    async def scenario() -> None:
        store = SqliteSessionStore(database)
        await store.save(_record("s-1", AgentStatus.RUNNING))
        _corrupt(database, "s-1", "checkpoints", "[[[")

        loaded = await store.load("s-1")

        assert loaded is not None
        assert loaded.checkpoints == ()
        assert loaded.working_memory.objective == "Fix calc.add"

    asyncio.run(scenario())


# ------------------------------------------------------------------ tool results


def test_a_tool_result_reference_survives_a_restart(tmp_path: Path) -> None:
    database = tmp_path / "results.db"
    payload = "PERSISTED-" + "x" * 50_000

    async def scenario() -> None:
        token = CancellationSource().token
        reference = await SqliteToolResultStore(database).put(
            payload, media_type="text/plain", cancellation=token
        )

        # A different process, holding only the reference.
        restored = await SqliteToolResultStore(database).get(reference, token)

        assert restored == payload

    asyncio.run(scenario())


def test_a_reference_past_its_retention_window_fails_loudly(tmp_path: Path) -> None:
    database = tmp_path / "results.db"

    async def scenario() -> None:
        token = CancellationSource().token
        store = SqliteToolResultStore(database, retention_seconds=0.05)
        reference = await store.put("short lived", media_type="text/plain", cancellation=token)
        assert await store.get(reference, token) == "short lived"

        time.sleep(0.1)

        with pytest.raises(ToolResultUnavailableError) as failure:
            await store.get(reference, token)
        assert "expired" in failure.value.message

    asyncio.run(scenario())


def test_a_missing_or_tampered_payload_is_reported_not_guessed(tmp_path: Path) -> None:
    async def scenario() -> None:
        token = CancellationSource().token
        store = SqliteToolResultStore(tmp_path / "results.db")
        real = await store.put("content", media_type="text/plain", cancellation=token)

        missing = ToolResultReference("nope", "text/plain", 7, "abc")
        with pytest.raises(ToolResultUnavailableError):
            await store.get(missing, token)

        tampered = ToolResultReference(real.store_key, "text/plain", 7, "wrong-checksum")
        with pytest.raises(ToolResultUnavailableError):
            await store.get(tampered, token)

    asyncio.run(scenario())


def test_expired_results_can_be_purged(tmp_path: Path) -> None:
    async def scenario() -> None:
        token = CancellationSource().token
        store = SqliteToolResultStore(tmp_path / "results.db", retention_seconds=0.05)
        await store.put("a", media_type="text/plain", cancellation=token)
        await store.put("b", media_type="text/plain", cancellation=token)
        time.sleep(0.1)

        assert await store.purge_expired() == 2
        assert await store.purge_expired() == 0

    asyncio.run(scenario())
