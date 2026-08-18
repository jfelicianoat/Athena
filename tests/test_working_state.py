from __future__ import annotations

import json

import pytest

from athena.errors import ToolValidationError
from athena.working_state import PlanStep, RecordedError, StepStatus, WorkingState


def _state() -> WorkingState:
    return WorkingState(objective="Fix the failing test", constraints=("Do not touch tests",))


def test_working_state_requires_an_objective() -> None:
    with pytest.raises(ToolValidationError):
        WorkingState(objective="   ")


def test_the_runtime_validates_the_current_step_against_the_plan() -> None:
    plan = (PlanStep("Read calc.py"), PlanStep("Fix the bug"))

    advanced = _state().with_plan(plan, current_step=1)
    assert advanced.current_step == 1

    with pytest.raises(ToolValidationError):
        _state().with_plan(plan, current_step=5)
    with pytest.raises(ToolValidationError):
        _state().with_plan((), current_step=0)


def test_updates_are_recorded_without_duplicating_paths() -> None:
    state = (
        _state()
        .observing(files_examined=("calc.py", "calc.py", "test_calc.py"))
        .modifying(files_modified=("calc.py",))
        .ran("pytest -q")
        .noting(facts=("add subtracted",), decisions=("Fix the operator",))
        .failing(RecordedError("tool_validation_error", "bad input", "inform_model"))
    )

    assert state.files_examined == ("calc.py", "test_calc.py")
    assert state.files_modified == ("calc.py",)
    assert state.commands_run == ("pytest -q",)
    assert state.facts == ("add subtracted",)
    assert state.errors[0].recovery_action == "inform_model"


def test_state_is_serialisable_and_summarised_without_the_transcript() -> None:
    state = (
        _state()
        .with_plan((PlanStep("Fix", StepStatus.IN_PROGRESS),), current_step=0)
        .verified({"status": "passed", "summary": "All project checks pass."})
    )

    payload = json.loads(state.summary())

    assert payload["objective"] == "Fix the failing test"
    assert payload["constraints"] == ["Do not touch tests"]
    assert payload["verification"]["status"] == "passed"
    assert "0. [in_progress] Fix" in payload["plan"]
    assert set(state.to_json()) == {
        "objective",
        "constraints",
        "current_plan",
        "current_step",
        "facts",
        "files_examined",
        "files_modified",
        "commands_run",
        "decisions",
        "errors",
        "verification",
        "remaining_work",
    }


def test_long_lists_stay_bounded() -> None:
    state = _state()
    for index in range(150):
        state = state.ran(f"command-{index}")

    assert len(state.commands_run) == 100
    assert state.commands_run[-1] == "command-149"
