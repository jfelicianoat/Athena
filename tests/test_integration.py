"""Putting isolated writers back together, and finding out when they disagree.

Overlap is not conflict. Two tasks editing opposite ends of one file agree perfectly, and a
runtime that treated "they both touched it" as a problem would serialise work that never
needed serialising. So the interesting cases here are the two that look alike from outside:
a shared file that merges cleanly, and one that does not.

Everything runs against real git. A fake would agree with whatever this module assumed
about what constitutes a conflict, which is precisely the judgement being delegated.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from athena.cancellation import CancellationSource, CancellationToken
from athena.integration import (
    Integrator,
    PatchOutcome,
    revert_applied,
)
from athena.isolation import (
    IntegrationCandidate,
    IsolationMode,
    WorkspaceIsolationPolicy,
    collect_diff,
)
from athena.state import SessionState
from athena.subagents import SubagentRole
from athena.verification import (
    VerificationEvidence,
    VerificationResult,
    VerificationStatus,
)
from athena.workspace import Workspace

ORIGINAL = "\n".join(
    [
        "def add(a, b):",
        "    return a + b",
        "",
        "",
        "def subtract(a, b):",
        "    return a - b",
        "",
        "",
        "def multiply(a, b):",
        "    return a * b",
        "",
    ]
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=root, check=True, capture_output=True, timeout=60)


def _repository(tmp_path: Path) -> Workspace:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "calc.py").write_text(ORIGINAL, encoding="utf-8")
    (root / "api.py").write_text("def handler():\n    return None\n", encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "athena@example.invalid")
    _git(root, "config", "user.name", "Athena Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    return Workspace.from_path(root)


class _FixedVerification:
    def __init__(self, status: VerificationStatus) -> None:
        self.status = status
        self.calls = 0

    async def verify(
        self, state: SessionState, workspace: Workspace, cancellation: CancellationToken
    ) -> VerificationResult:
        del state, workspace
        cancellation.raise_if_cancelled()
        self.calls += 1
        return VerificationResult(
            status=self.status,
            summary=f"stubbed {self.status.value}",
            evidence=(VerificationEvidence("stub", "a stated result"),),
        )


async def _work(
    policy: WorkspaceIsolationPolicy,
    workspace: Workspace,
    task_id: str,
    edit: tuple[str, str, str],
    token: CancellationToken,
) -> IntegrationCandidate:
    """Give a task its own checkout, change one file in it, and read the diff out."""
    filename, before, after = edit
    isolated = await policy.acquire(task_id, SubagentRole.CODER, workspace, token)
    target = isolated.workspace.root / filename
    target.write_text(target.read_text(encoding="utf-8").replace(before, after), encoding="utf-8")
    candidate = await collect_diff(isolated, task_id, token)
    assert candidate is not None, "the edit produced a diff"
    return candidate


# ------------------------------------------------------------------ what actually merges


def test_two_tasks_on_different_files_both_land(tmp_path: Path) -> None:
    """The case parallelism exists for, and the one it must not complicate."""

    async def scenario() -> None:
        workspace = _repository(tmp_path)
        policy = WorkspaceIsolationPolicy(IsolationMode.PER_WRITER)
        token = CancellationSource().token
        first = await _work(
            policy, workspace, "T01", ("calc.py", "return a * b", "return a * b * 1"), token
        )
        second = await _work(
            policy, workspace, "T02", ("api.py", "return None", 'return "ok"'), token
        )

        result = await Integrator().integrate(workspace, [first, second], token)
        await policy.release_all(token)

        assert set(result.applied) == {"T01", "T02"}
        assert result.conflicted == ()
        assert "return a * b * 1" in (workspace.root / "calc.py").read_text()
        assert 'return "ok"' in (workspace.root / "api.py").read_text()

    asyncio.run(scenario())


def test_one_file_two_tasks_that_agree_still_merges(tmp_path: Path) -> None:
    """Overlap is not conflict.

    Both tasks touched `calc.py` at opposite ends. Treating that as a problem would
    serialise work that never needed serialising, which is the bottleneck the whole
    isolation layer was built to remove.
    """

    async def scenario() -> None:
        workspace = _repository(tmp_path)
        policy = WorkspaceIsolationPolicy(IsolationMode.PER_WRITER)
        token = CancellationSource().token
        top = await _work(
            policy, workspace, "T01", ("calc.py", "return a + b", "return b + a"), token
        )
        bottom = await _work(
            policy, workspace, "T02", ("calc.py", "return a * b", "return b * a"), token
        )

        result = await Integrator().integrate(workspace, [top, bottom], token)
        await policy.release_all(token)

        assert set(result.applied) == {"T01", "T02"}
        merged = (workspace.root / "calc.py").read_text()
        assert "return b + a" in merged
        assert "return b * a" in merged
        assert result.overlaps == {"calc.py": ["T01", "T02"]}, "reported even though it merged"

    asyncio.run(scenario())


def test_the_same_line_twice_is_a_conflict_and_git_says_so(tmp_path: Path) -> None:
    """The judgement that is delegated, exercised.

    Both tasks rewrote the same line differently. Nothing in this module reads the diffs to
    work that out — git does, and the answer is its exit code.
    """

    async def scenario() -> None:
        workspace = _repository(tmp_path)
        policy = WorkspaceIsolationPolicy(IsolationMode.PER_WRITER)
        token = CancellationSource().token
        first = await _work(
            policy, workspace, "T01", ("calc.py", "return a + b", "return int(a + b)"), token
        )
        second = await _work(
            policy, workspace, "T02", ("calc.py", "return a + b", "return float(a + b)"), token
        )

        result = await Integrator().integrate(workspace, [first, second], token)
        await policy.release_all(token)

        assert result.applied == ("T01",)
        assert result.conflicted == ("T02",)
        assert not result.succeeded

    asyncio.run(scenario())


def test_a_conflicting_patch_leaves_the_workspace_where_it_found_it(tmp_path: Path) -> None:
    """Checked before applied.

    A workspace half-integrated and then found to conflict is worse than one nobody
    touched, because the person now has to work out which half.
    """

    async def scenario() -> None:
        workspace = _repository(tmp_path)
        policy = WorkspaceIsolationPolicy(IsolationMode.PER_WRITER)
        token = CancellationSource().token
        # A patch built against a file that has since changed underneath it.
        stale = await _work(
            policy, workspace, "T01", ("calc.py", "return a + b", "return a + b + 1"), token
        )
        (workspace.root / "calc.py").write_text(
            ORIGINAL.replace("def add(a, b):\n    return a + b", "def add(x, y):\n    return x"),
            encoding="utf-8",
        )
        before = (workspace.root / "calc.py").read_text()

        result = await Integrator().integrate(workspace, [stale], token)
        await policy.release_all(token)

        assert result.conflicted == ("T01",)
        assert (workspace.root / "calc.py").read_text() == before, "untouched"

    asyncio.run(scenario())


def test_a_task_that_changed_nothing_is_not_a_failure(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = _repository(tmp_path)

        result = await Integrator().integrate(
            workspace,
            [IntegrationCandidate("T01", "athena/T01", "", ())],
            CancellationSource().token,
        )

        assert result.patches[0].outcome is PatchOutcome.EMPTY
        assert result.conflicted == ()

    asyncio.run(scenario())


# ------------------------------------------------------------- applying is not succeeding


def test_every_patch_applying_is_not_the_same_as_it_working(tmp_path: Path) -> None:
    """The distinction this module exists to make.

    Two changes that each apply cleanly can still be wrong together, and a runtime that
    stopped at "no conflict" would be asserting something git never claimed.
    """

    async def scenario() -> None:
        workspace = _repository(tmp_path)
        policy = WorkspaceIsolationPolicy(IsolationMode.PER_WRITER)
        token = CancellationSource().token
        first = await _work(
            policy, workspace, "T01", ("calc.py", "return a * b", "return a * b * 1"), token
        )
        checks = _FixedVerification(VerificationStatus.FAILED)

        result = await Integrator(checks).integrate(workspace, [first], token)
        await policy.release_all(token)

        assert result.applied == ("T01",)
        assert not result.succeeded, "applied, and the result does not work"
        assert checks.calls == 1

    asyncio.run(scenario())


def test_a_verified_integration_succeeds(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = _repository(tmp_path)
        policy = WorkspaceIsolationPolicy(IsolationMode.PER_WRITER)
        token = CancellationSource().token
        first = await _work(
            policy, workspace, "T01", ("api.py", "return None", 'return "ok"'), token
        )

        result = await Integrator(_FixedVerification(VerificationStatus.PASSED)).integrate(
            workspace, [first], token
        )
        await policy.release_all(token)

        assert result.succeeded

    asyncio.run(scenario())


def test_a_half_integrated_workspace_is_not_verified(tmp_path: Path) -> None:
    """Verifying an incomplete assembly would measure something nobody asked for.

    Worse, it would attribute the failure to whichever task happened to apply.
    """

    async def scenario() -> None:
        workspace = _repository(tmp_path)
        policy = WorkspaceIsolationPolicy(IsolationMode.PER_WRITER)
        token = CancellationSource().token
        first = await _work(
            policy, workspace, "T01", ("calc.py", "return a + b", "return int(a + b)"), token
        )
        second = await _work(
            policy, workspace, "T02", ("calc.py", "return a + b", "return float(a + b)"), token
        )
        checks = _FixedVerification(VerificationStatus.PASSED)

        result = await Integrator(checks).integrate(workspace, [first, second], token)
        await policy.release_all(token)

        assert result.conflicted == ("T02",)
        assert checks.calls == 0
        assert not result.succeeded

    asyncio.run(scenario())


def test_nothing_applied_means_nothing_to_verify(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = _repository(tmp_path)
        checks = _FixedVerification(VerificationStatus.PASSED)

        result = await Integrator(checks).integrate(
            workspace,
            [IntegrationCandidate("T01", "athena/T01", "", ())],
            CancellationSource().token,
        )

        assert checks.calls == 0
        assert not result.succeeded

    asyncio.run(scenario())


# -------------------------------------------------------------------------- undoing it


def test_an_integration_that_did_not_work_can_be_taken_back_out(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = _repository(tmp_path)
        policy = WorkspaceIsolationPolicy(IsolationMode.PER_WRITER)
        token = CancellationSource().token
        candidate = await _work(
            policy, workspace, "T01", ("api.py", "return None", 'return "ok"'), token
        )
        result = await Integrator().integrate(workspace, [candidate], token)
        assert 'return "ok"' in (workspace.root / "api.py").read_text()

        reverted = await revert_applied(workspace, result, [candidate], token)
        await policy.release_all(token)

        assert reverted == ("api.py",)
        assert "return None" in (workspace.root / "api.py").read_text()

    asyncio.run(scenario())


def test_reverting_without_the_original_text_does_nothing_rather_than_pretending(
    tmp_path: Path,
) -> None:
    """A revert that quietly did nothing would be worse than one that refuses.

    `IntegrationResult` does not carry patch bodies on purpose — they are large and it ends
    up in events — so the caller has to supply them.
    """

    async def scenario() -> None:
        workspace = _repository(tmp_path)
        policy = WorkspaceIsolationPolicy(IsolationMode.PER_WRITER)
        token = CancellationSource().token
        candidate = await _work(
            policy, workspace, "T01", ("api.py", "return None", 'return "ok"'), token
        )
        result = await Integrator().integrate(workspace, [candidate], token)

        reverted = await revert_applied(workspace, result, [], token)
        await policy.release_all(token)

        assert reverted == ()
        assert 'return "ok"' in (workspace.root / "api.py").read_text()

    asyncio.run(scenario())


# ------------------------------------------------------------------------ what it is not


def test_the_integrator_decides_nothing_about_what_should_happen_next() -> None:
    """It reports. Dropping a task, replanning it or asking a person belongs to the graph."""
    import athena.integration as integration

    exported = set(integration.__all__)

    assert not any("policy" in name.lower() for name in exported)
    assert not any("retry" in name.lower() for name in exported)
    assert "IntegrationResult" in exported


def test_conflict_is_git_s_answer_and_not_a_diff_read_here() -> None:
    import ast
    from pathlib import Path as _Path

    import athena

    module = _Path(athena.__file__).parent / "integration.py"
    source = module.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(module))
    imported = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("athena")
    }

    assert "athena.models" not in imported, "no model decides whether things conflict"
    assert "--3way" in source, "git does the three-way merge"
    assert "--check" in source, "and it is asked before anything is touched"
