"""Deriving the decomposition evidence instead of being handed it.

The scout's value is not that it fills in six numbers — anything can do that. It is that it
says which of the six it actually established and which it left alone, because a default
presented as a measurement is worse than an admitted gap: the policy weighs both the same
and only one of them is true.

So most of these tests are about the second list.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from athena.planning import DecompositionPolicy, DecompositionSignals
from athena.scouting import RepositoryScout, merge
from athena.workspace import Workspace


def _repo(
    tmp_path: Path, *, with_checks: bool = True, files: dict[str, str] | None = None
) -> Workspace:
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    for name, body in (files or {"calc.py": "def add(a, b):\n    return a + b\n"}).items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    if with_checks:
        command = f'"{sys.executable}" -m pytest -q'
        (root / "AGENTS.md").write_text(
            f"# Sandbox\n\n## Verification\n\n```\n{command}\n```\n", encoding="utf-8"
        )
    return Workspace.from_path(root)


# --------------------------------------------------------------- what it actually measures


def test_paths_named_in_the_objective_are_resolved(tmp_path: Path) -> None:
    """A path either exists in the repository or it does not.

    That is checkable in a way that "this sounds ambitious" is not, which is why it is the
    only thing the objective is read for.
    """
    workspace = _repo(tmp_path, files={"calc.py": "x\n", "api.py": "y\n"})

    scouted = RepositoryScout().scout(workspace, "fix add in calc.py and the handler in api.py")

    assert set(scouted.resolved_paths) == {"calc.py", "api.py"}


def test_a_path_that_names_nothing_is_prose(tmp_path: Path) -> None:
    """Otherwise a wordy objective would look like a wide one."""
    workspace = _repo(tmp_path)

    scouted = RepositoryScout().scout(
        workspace, "improve the payment.service and the billing.engine"
    )

    assert scouted.resolved_paths == ()


def test_files_in_different_directories_count_as_different_subsystems(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path, files={"api/handler.py": "a\n", "storage/db.py": "b\n"})

    scouted = RepositoryScout().scout(workspace, "change api/handler.py and storage/db.py")

    assert scouted.signals.subsystems_touched == 2
    assert "subsystems_touched" in scouted.established


def test_files_in_one_directory_are_one_subsystem(tmp_path: Path) -> None:
    workspace = _repo(tmp_path, files={"api/handler.py": "a\n", "api/routes.py": "b\n"})

    scouted = RepositoryScout().scout(workspace, "change api/handler.py and api/routes.py")

    assert scouted.signals.subsystems_touched == 1


def test_a_large_file_is_recognised_as_risky(tmp_path: Path) -> None:
    workspace = _repo(tmp_path, files={"huge.py": "x = 1\n" * 900})

    scouted = RepositoryScout().scout(workspace, "refactor huge.py")

    assert scouted.signals.high_implementation_risk
    assert "high_implementation_risk" in scouted.established
    assert any("900 lines" in note for note in scouted.notes)


def test_a_small_file_is_measured_as_not_risky(tmp_path: Path) -> None:
    # Measured, not assumed: the difference is that this one appears in `established`.
    workspace = _repo(tmp_path)

    scouted = RepositoryScout().scout(workspace, "fix calc.py")

    assert not scouted.signals.high_implementation_risk
    assert "high_implementation_risk" in scouted.established


def test_the_number_of_checks_bounds_what_can_be_proved_separately(
    tmp_path: Path,
) -> None:
    """One check means one thing to prove, whatever else the objective mentions."""
    workspace = _repo(
        tmp_path, files={"api/handler.py": "a\n", "storage/db.py": "b\n"}, with_checks=True
    )

    scouted = RepositoryScout().scout(workspace, "change api/handler.py and storage/db.py")

    assert scouted.signals.subsystems_touched == 2
    assert scouted.signals.independently_verifiable_outputs == 1, "one check, one proof"


# --------------------------------------------------------------- what it refuses to invent


def test_signals_it_cannot_establish_are_named_rather_than_defaulted(
    tmp_path: Path,
) -> None:
    """The design.

    A default presented as a measurement is worse than an admitted gap, because the policy
    weighs both the same and only one of them is true.
    """
    workspace = _repo(tmp_path)

    scouted = RepositoryScout().scout(workspace, "fix calc.py")

    assert "has_meaningful_dependencies" in scouted.assumed
    assert "distinct_roles_required" in scouted.assumed
    assert not scouted.is_complete
    assert set(scouted.established) & set(scouted.assumed) == set()


def test_a_project_with_no_checks_admits_it_cannot_count_outputs(tmp_path: Path) -> None:
    """The gate the policy weighs most, so guessing it would decide the whole question."""
    workspace = _repo(tmp_path, with_checks=False)

    scouted = RepositoryScout().scout(workspace, "fix calc.py")

    assert "independently_verifiable_outputs" in scouted.assumed
    assert scouted.signals.independently_verifiable_outputs == 1
    assert any("no verification commands" in note for note in scouted.notes)


def test_risk_is_unknown_when_nothing_was_named(tmp_path: Path) -> None:
    """Returning `False` would assert safety on no evidence.

    Risk is a property of what is being changed; with nothing named there is nothing to
    measure.
    """
    workspace = _repo(tmp_path)

    scouted = RepositoryScout().scout(workspace, "make the project better somehow")

    assert "high_implementation_risk" in scouted.assumed
    assert not scouted.signals.high_implementation_risk


def test_the_explanation_says_what_was_and_was_not_measured(tmp_path: Path) -> None:
    workspace = _repo(tmp_path)

    explanation = RepositoryScout().scout(workspace, "fix calc.py").explain()

    assert "Established from the repository" in explanation
    assert "Not established" in explanation
    assert "has_meaningful_dependencies" in explanation


def test_it_asks_no_model(tmp_path: Path) -> None:
    """A repository is evidence and a sentence is not."""
    import ast

    import athena

    del tmp_path
    module = Path(athena.__file__).parent / "scouting.py"
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    imported = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("athena")
    }

    assert "athena.models" not in imported
    assert not any(name.startswith("athena.adapters") for name in imported)


# ------------------------------------------------------------------- filling in the gaps


def test_a_caller_can_supply_what_the_repository_cannot_show(tmp_path: Path) -> None:
    """The direction is the point.

    A caller knows that two outputs genuinely depend on each other. It does not know better
    than the filesystem how many directories exist.
    """
    workspace = _repo(tmp_path, files={"api/h.py": "a\n", "storage/d.py": "b\n"})
    scouted = RepositoryScout().scout(workspace, "change api/h.py and storage/d.py")

    combined = merge(
        scouted,
        DecompositionSignals(
            has_meaningful_dependencies=True,
            distinct_roles_required=2,
            subsystems_touched=99,
        ),
    )

    assert combined.has_meaningful_dependencies, "the caller knew this"
    assert combined.distinct_roles_required == 2
    assert combined.subsystems_touched == 2, "and the filesystem knew this"


def test_merging_changes_nothing_when_the_caller_adds_nothing(tmp_path: Path) -> None:
    workspace = _repo(tmp_path)
    scouted = RepositoryScout().scout(workspace, "fix calc.py")

    combined = merge(scouted, DecompositionSignals())

    assert combined == scouted.signals


# ------------------------------------------------------------------ through to a decision


def test_a_one_file_goal_in_a_checked_project_is_not_decomposed(tmp_path: Path) -> None:
    """The whole point of measuring: this used to be answered by whoever was asked."""
    workspace = _repo(tmp_path)
    scouted = RepositoryScout().scout(workspace, "fix the failing test in calc.py")

    decision = DecompositionPolicy().assess(scouted.signals)

    assert not decision.decompose
    assert "AgentLoop" in decision.explanation


def test_a_goal_across_subsystems_with_several_checks_is(tmp_path: Path) -> None:
    workspace = _repo(
        tmp_path,
        files={"api/h.py": "a\n", "storage/d.py": "b\n"},
        with_checks=False,
    )
    # Both commands in one block: that is how the planner reads them, and counting them
    # any other way here would measure something a run would never execute.
    (workspace.root / "AGENTS.md").write_text(
        "# Sandbox\n\n## Verification\n\n```\npython -m pytest -q\npython -m ruff check .\n```\n",
        encoding="utf-8",
    )
    scouted = RepositoryScout().scout(workspace, "rework api/h.py and storage/d.py together")

    combined = merge(scouted, DecompositionSignals(has_meaningful_dependencies=True))
    decision = DecompositionPolicy().assess(combined)

    assert scouted.signals.independently_verifiable_outputs >= 2
    assert decision.decompose
    assert "multiple independently verifiable outputs" in decision.reasons


def test_scouting_a_repository_it_cannot_read_does_not_raise(tmp_path: Path) -> None:
    # A workspace with nothing in it is a legitimate state, not an error.
    root = tmp_path / "empty"
    root.mkdir()
    workspace = Workspace.from_path(root)

    scouted = RepositoryScout().scout(workspace, "do something")

    assert scouted.signals.subsystems_touched == 1
    assert not scouted.is_complete


@pytest.mark.parametrize(
    "objective",
    ["", "   ", "https://example.com/calc.py", "see calc.py.bak"],
    ids=["empty", "blank", "a-url", "a-file-that-does-not-exist"],
)
def test_an_objective_with_no_usable_paths_is_handled(objective: str, tmp_path: Path) -> None:
    workspace = _repo(tmp_path)

    scouted = RepositoryScout().scout(workspace, objective)

    assert scouted.resolved_paths == ()
