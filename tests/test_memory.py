from __future__ import annotations

import pytest

from athena.memory import (
    ContextWindowManager,
    ConversationContext,
    MicroCompaction,
    WorkingMemory,
)
from athena.models import ModelMessage, ModelRole
from athena.working_state import PlanStep, RecordedError, StepStatus


def _tool(name: str, content: str) -> ModelMessage:
    return ModelMessage(ModelRole.TOOL, content, name=name, tool_call_id=f"{name}-1")


def _memory() -> WorkingMemory:
    return (
        WorkingMemory(objective="Fix the failing test", constraints=("Do not touch tests",))
        .with_plan((PlanStep("Read", StepStatus.DONE), PlanStep("Fix")), current_step=1)
        .observing(files_examined=("calc.py",))
        .modifying(files_modified=("calc.py",))
        .ran("pytest -q")
        .noting(
            facts=("add subtracted",),
            decisions=("Restore the operator",),
            remaining_work=("Re-run the suite",),
        )
        .failing(RecordedError("tool_validation_error", "bad input", "inform_model"))
        .verified({"status": "failed", "summary": "1 test failing"})
    )


def test_working_memory_survives_a_round_trip() -> None:
    original = _memory()

    restored = WorkingMemory.from_json(original.to_json())

    assert restored == original


def test_round_trip_tolerates_missing_optional_fields() -> None:
    restored = WorkingMemory.from_json({"objective": "Only the objective survived"})

    assert restored.objective == "Only the objective survived"
    assert restored.current_plan == ()
    assert restored.current_step is None
    assert restored.files_modified == ()


def test_round_trip_refuses_a_payload_with_no_objective() -> None:
    from athena.errors import ToolValidationError

    with pytest.raises(ToolValidationError):
        WorkingMemory.from_json({"facts": ["orphaned"]})


def test_round_trip_drops_a_step_index_that_no_longer_has_a_plan() -> None:
    restored = WorkingMemory.from_json({"objective": "x", "current_step": 7})

    assert restored.current_step is None


# ------------------------------------------------------------------ compaction


def test_compaction_keeps_the_recent_turns_verbatim() -> None:
    messages = tuple(ModelMessage(ModelRole.ASSISTANT, f"turn {index}") for index in range(20))
    context = ConversationContext(messages)

    compacted, report = MicroCompaction(keep_recent=4).compact(context)

    assert compacted.messages[-4:] == messages[-4:]
    assert report.messages_before == 20


def test_compaction_reduces_an_externalized_result_to_its_reference() -> None:
    payload = (
        '{"summary": "' + "x" * 300 + '", "externalized": true, '
        '"reference_uri": "athena-result://abc-123"}'
    )
    context = ConversationContext(
        (_tool("read_file", payload), *[ModelMessage(ModelRole.USER, "next")] * 6)
    )

    compacted, report = MicroCompaction(keep_recent=6, stub_chars=40).compact(context)

    assert report.dropped_externalized == 1
    assert "athena-result://abc-123" in compacted.messages[0].content
    assert len(compacted.messages[0].content) < len(payload)


def test_compaction_drops_repeated_identical_tool_output() -> None:
    repeated = _tool("git_status", "clean")
    context = ConversationContext(
        (repeated, repeated, repeated, *[ModelMessage(ModelRole.USER, "go")] * 3)
    )

    compacted, report = MicroCompaction(keep_recent=3).compact(context)

    assert report.dropped_duplicates == 2
    assert sum(1 for message in compacted.messages if message.name == "git_status") == 1


def test_compaction_truncates_an_oversized_message() -> None:
    context = ConversationContext(
        (ModelMessage(ModelRole.ASSISTANT, "y" * 9_000), *[ModelMessage(ModelRole.USER, "x")] * 3)
    )

    compacted, report = MicroCompaction(keep_recent=3, max_message_chars=100).compact(context)

    assert report.truncated == 1
    assert "message truncated" in compacted.messages[0].content


def test_compaction_never_loses_what_working_memory_holds() -> None:
    """Compaction is safe precisely because the durable facts are not in the transcript."""
    memory = _memory()
    context = ConversationContext(
        tuple(ModelMessage(ModelRole.ASSISTANT, "chatter " * 500) for _ in range(30))
    )

    compacted, _ = MicroCompaction(keep_recent=3, max_message_chars=50).compact(context, memory)

    assert compacted.size_chars < context.size_chars
    assert memory.objective == "Fix the failing test"
    assert memory.files_modified == ("calc.py",)
    assert memory.decisions == ("Restore the operator",)
    assert memory.errors[0].code == "tool_validation_error"
    assert memory.verification["status"] == "failed"
    assert memory.remaining_work == ("Re-run the suite",)


# ------------------------------------------------------------------ window manager


def test_a_small_context_is_passed_through_untouched() -> None:
    context = ConversationContext((ModelMessage(ModelRole.USER, "short"),))

    selected, report = ContextWindowManager(max_context_chars=1_000).select(context)

    assert selected is context
    assert report is None


def test_a_large_session_is_compacted_rather_than_concatenated() -> None:
    messages = tuple(
        _tool("read_file", f'{{"reference_uri": "athena-result://{index}", "body": "{"z" * 900}"}}')
        for index in range(60)
    )
    context = ConversationContext(messages)
    manager = ContextWindowManager(
        max_context_chars=8_000, compaction=MicroCompaction(keep_recent=4, stub_chars=60)
    )

    selected, report = manager.select(context, _memory())

    assert report is not None
    assert selected.size_chars <= 8_000
    assert len(selected) < len(context)


def test_the_window_falls_back_to_recent_turns_when_compaction_is_not_enough() -> None:
    messages = tuple(ModelMessage(ModelRole.ASSISTANT, "w" * 2_000) for _ in range(40))
    manager = ContextWindowManager(
        max_context_chars=5_000,
        compaction=MicroCompaction(keep_recent=2, max_message_chars=1_500),
    )

    selected, report = manager.select(ConversationContext(messages))

    assert report is not None
    assert len(selected) == 2
    assert "window trimmed" in " ".join(report.reasons)


def test_nothing_is_ever_remembered_automatically() -> None:
    """What H4 actually decided, checked as a rule rather than as an absence.

    The original form of this test asserted that no concrete store existed at all, which
    was true and is no longer: `SqliteProjectMemory` shipped in P5. The decision it was
    protecting survives intact — a store exists, and there is still no path by which a
    conclusion becomes a remembered fact without someone asking for it.
    """
    import inspect

    import athena
    from athena.project_memory import SqliteProjectMemory, VerificationState

    assert hasattr(athena, "ProjectMemory"), "the interface is still the contract"

    # `propose` is the only way in, and it hard-codes the weakest standing. An agent that
    # could write `VERIFIED` would be grading its own homework.
    assert not hasattr(SqliteProjectMemory, "remember")
    source = inspect.getsource(SqliteProjectMemory.propose)
    assert "verification_state" not in source, "propose cannot be argued into a higher state"

    signature = inspect.signature(SqliteProjectMemory.propose)
    assert "confidence" not in signature.parameters, "the writer does not grade itself"
    assert VerificationState.PROPOSED.value == "proposed"
