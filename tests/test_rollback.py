"""Undoing what Athena did, and refusing to undo anything else.

The tests that matter here are the refusals. A rollback that reverted the workspace
wholesale would take a person's uncommitted work with it and give them no way to find out,
so most of what follows is about files the rollback declines to touch.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from athena.checkpoints import CheckpointStore
from athena.rollback import (
    RollbackError,
    RollbackLedger,
    RollbackScope,
    is_worth_checkpointing,
)
from athena.workspace import Workspace


def _workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "notes.md").write_text("a person wrote this\n", encoding="utf-8")
    return Workspace.from_path(root)


def _ledger(tmp_path: Path) -> RollbackLedger:
    return RollbackLedger(CheckpointStore(tmp_path / "checkpoints"))


# ------------------------------------------------------------------- when to bother


def test_a_reader_is_not_checkpointed() -> None:
    """A task that cannot write cannot damage anything.

    Copying the workspace to protect against it would make every run pay for the worst
    case regardless of what the task is allowed to do.
    """
    assert not is_worth_checkpointing(["calc.py"], writes=False)


def test_a_writer_touching_nothing_is_not_checkpointed() -> None:
    assert not is_worth_checkpointing([], writes=True)


def test_a_writer_touching_something_is() -> None:
    assert is_worth_checkpointing(["calc.py"], writes=True)


def test_nothing_to_copy_produces_no_rollback_point(tmp_path: Path) -> None:
    """`None` rather than an empty point.

    An empty rollback point would later read as a rollback that found nothing to undo,
    which is a different and more worrying thing than never having taken one.
    """

    async def scenario() -> None:
        ledger = _ledger(tmp_path)

        point = await ledger.checkpoint("T01", _workspace(tmp_path), [])

        assert point is None
        assert ledger.points() == ()

    asyncio.run(scenario())


# ------------------------------------------------------------------------ undoing


def test_a_task_s_change_can_be_put_back(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = _workspace(tmp_path)
        ledger = _ledger(tmp_path)
        await ledger.checkpoint("T01", workspace, ["calc.py"])
        ledger.record_written("T01", ["calc.py"])
        (workspace.root / "calc.py").write_text("def add(a, b):\n    return 0\n")

        result = await ledger.roll_back(workspace, task_id="T01")

        assert result.restored == ("calc.py",)
        assert "return a + b" in (workspace.root / "calc.py").read_text()

    asyncio.run(scenario())


def test_a_file_this_run_never_wrote_is_left_alone(tmp_path: Path) -> None:
    """The refusal the whole module exists for.

    Reverting a file the agent never touched would discard uncommitted work with no way
    for its owner to find out it happened.
    """

    async def scenario() -> None:
        workspace = _workspace(tmp_path)
        ledger = _ledger(tmp_path)
        await ledger.checkpoint("T01", workspace, ["calc.py", "notes.md"])
        # The agent wrote calc.py. A person edited notes.md while it worked.
        ledger.record_written("T01", ["calc.py"])
        (workspace.root / "calc.py").write_text("broken\n")
        (workspace.root / "notes.md").write_text("a person wrote this, and then more\n")

        result = await ledger.roll_back(workspace, task_id="T01")

        assert result.restored == ("calc.py",)
        assert result.protected == ("notes.md",)
        assert "and then more" in (workspace.root / "notes.md").read_text()

    asyncio.run(scenario())


def test_a_file_the_task_created_is_removed_again(tmp_path: Path) -> None:
    # The checkpoint recorded that it did not exist, so putting the workspace back means
    # taking it away.
    async def scenario() -> None:
        workspace = _workspace(tmp_path)
        ledger = _ledger(tmp_path)
        await ledger.checkpoint("T01", workspace, ["new_module.py"])
        ledger.record_written("T01", ["new_module.py"])
        (workspace.root / "new_module.py").write_text("print('hello')\n")

        await ledger.roll_back(workspace, task_id="T01")

        assert not (workspace.root / "new_module.py").exists()

    asyncio.run(scenario())


def test_undoing_one_task_leaves_its_sibling_alone(tmp_path: Path) -> None:
    """Scopes mirror cancellation's, and for the same reason."""

    async def scenario() -> None:
        workspace = _workspace(tmp_path)
        (workspace.root / "api.py").write_text("original api\n", encoding="utf-8")
        ledger = _ledger(tmp_path)
        await ledger.checkpoint("T01", workspace, ["calc.py"])
        await ledger.checkpoint("T02", workspace, ["api.py"])
        ledger.record_written("T01", ["calc.py"])
        ledger.record_written("T02", ["api.py"])
        (workspace.root / "calc.py").write_text("broken\n")
        (workspace.root / "api.py").write_text("good work\n")

        await ledger.roll_back(workspace, task_id="T01")

        assert "return a + b" in (workspace.root / "calc.py").read_text()
        assert "good work" in (workspace.root / "api.py").read_text(), "the sibling survives"

    asyncio.run(scenario())


def test_a_subgraph_rollback_takes_what_it_covers(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = _workspace(tmp_path)
        (workspace.root / "api.py").write_text("original api\n", encoding="utf-8")
        ledger = _ledger(tmp_path)
        await ledger.checkpoint(
            "T01", workspace, ["calc.py"], scope=RollbackScope.SUBGRAPH, covers=["T02"]
        )
        await ledger.checkpoint(
            "T02", workspace, ["api.py"], scope=RollbackScope.SUBGRAPH, covers=["T02"]
        )
        ledger.record_written("T01", ["calc.py"])
        ledger.record_written("T02", ["api.py"])
        (workspace.root / "calc.py").write_text("broken\n")
        (workspace.root / "api.py").write_text("also broken\n")

        result = await ledger.roll_back(workspace, task_id="T02", scope=RollbackScope.SUBGRAPH)

        assert set(result.restored) == {"calc.py", "api.py"}

    asyncio.run(scenario())


def test_a_run_rollback_undoes_everything_it_wrote(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = _workspace(tmp_path)
        (workspace.root / "api.py").write_text("original api\n", encoding="utf-8")
        ledger = _ledger(tmp_path)
        await ledger.checkpoint("T01", workspace, ["calc.py"])
        await ledger.checkpoint("T02", workspace, ["api.py"])
        ledger.record_written("T01", ["calc.py"])
        ledger.record_written("T02", ["api.py"])
        (workspace.root / "calc.py").write_text("broken\n")
        (workspace.root / "api.py").write_text("also broken\n")

        result = await ledger.roll_back(workspace, scope=RollbackScope.RUN)

        assert set(result.restored) == {"calc.py", "api.py"}
        assert "return a + b" in (workspace.root / "calc.py").read_text()
        assert "original api" in (workspace.root / "api.py").read_text()

    asyncio.run(scenario())


def test_two_tasks_that_touched_one_file_restore_the_older_state(tmp_path: Path) -> None:
    """Newest first, because checkpoints overlap.

    Restoring the older copy last would put back a state that predates work the rollback
    was never asked to undo.
    """

    async def scenario() -> None:
        workspace = _workspace(tmp_path)
        ledger = _ledger(tmp_path)
        await ledger.checkpoint("T01", workspace, ["calc.py"])
        ledger.record_written("T01", ["calc.py"])
        (workspace.root / "calc.py").write_text("first change\n")
        await ledger.checkpoint("T02", workspace, ["calc.py"])
        ledger.record_written("T02", ["calc.py"])
        (workspace.root / "calc.py").write_text("second change\n")

        await ledger.roll_back(workspace, scope=RollbackScope.RUN)

        assert "return a + b" in (workspace.root / "calc.py").read_text()

    asyncio.run(scenario())


# ------------------------------------------------------------------------- edges


def test_rolling_back_a_task_that_was_never_checkpointed_changes_nothing(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        workspace = _workspace(tmp_path)
        ledger = _ledger(tmp_path)

        result = await ledger.roll_back(workspace, task_id="never-ran")

        assert not result.changed_anything
        assert "return a + b" in (workspace.root / "calc.py").read_text()

    asyncio.run(scenario())


def test_a_scoped_rollback_needs_to_know_what_to_undo(tmp_path: Path) -> None:
    # Falling back to "everything" when the caller forgot to say would be the most
    # destructive possible reading of an omission.
    async def scenario() -> None:
        ledger = _ledger(tmp_path)

        with pytest.raises(RollbackError):
            await ledger.roll_back(_workspace(tmp_path), scope=RollbackScope.TASK)

    asyncio.run(scenario())


def test_accepted_work_lets_its_checkpoints_go(tmp_path: Path) -> None:
    # A run that kept every checkpoint would keep a copy of the workspace per task.
    async def scenario() -> None:
        workspace = _workspace(tmp_path)
        ledger = _ledger(tmp_path)
        await ledger.checkpoint("T01", workspace, ["calc.py"])

        await ledger.discard("T01")

        assert ledger.points() == ()

    asyncio.run(scenario())


def test_a_rollback_reports_what_it_declined_to_touch(tmp_path: Path) -> None:
    """Silence about a refusal is how a person ends up believing the workspace is clean."""

    async def scenario() -> None:
        workspace = _workspace(tmp_path)
        ledger = _ledger(tmp_path)
        await ledger.checkpoint("T01", workspace, ["notes.md"])
        (workspace.root / "notes.md").write_text("edited by a person\n")

        result = await ledger.roll_back(workspace, task_id="T01")

        assert result.protected == ("notes.md",)
        assert not result.changed_anything
        assert result.to_json()["protected"] == ["notes.md"]

    asyncio.run(scenario())


def test_attribution_is_recorded_rather_than_inferred() -> None:
    """A diff cannot tell the agent's edit from the person's.

    Guessing wrong in the permissive direction is how a rollback eats somebody's
    afternoon, so what the agent wrote is noted as it happens.
    """
    import ast
    from pathlib import Path as _Path

    import athena

    module = _Path(athena.__file__).parent / "rollback.py"
    source = module.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(module))
    imported = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("athena")
    }

    assert "athena.git_tools" not in imported, "attribution does not come from a diff"
    assert "record_written" in source
