"""De donde salen las comprobaciones: AGENTS.md, pyproject, package.json.

Se descubren del proyecto en vez de configurarse: un repo que ya declara sus
comandos no tiene que declararlos otra vez para Athena.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Sequence

from athena.errors import AthenaRuntimeError
from athena.permissions import RiskTier
from athena.process_tools import CommandPolicy, parse_command
from athena.verification.contratos import (
    CheckKind,
    PlanSource,
    VerificationCheck,
    VerificationPlan,
)
from athena.workspace import Workspace


class VerificationPlanner:
    """Discovers verification commands. It never invents one.

    Sources, in precedence order:

    1. an explicit configuration passed by the operator;
    2. a `## Verification` section in the workspace `AGENTS.md`;
    3. the project's own configuration (`pyproject.toml`, `package.json`).

    Every candidate is parsed into argv and classified by `CommandPolicy`. Anything that
    is not plain local execution (R2) is discarded, so a malicious or careless
    instruction file cannot turn verification into an escape hatch.
    """

    _SECTION = re.compile(
        r"^##+\s*verification\s*$(.*?)(?=^##+\s|\Z)", re.IGNORECASE | re.MULTILINE | re.DOTALL
    )
    _FENCE = re.compile(r"```[a-zA-Z]*\n(.*?)```", re.DOTALL)

    def __init__(
        self,
        workspace: Workspace,
        *,
        command_policy: CommandPolicy | None = None,
        explicit: Sequence[VerificationCheck] = (),
    ) -> None:
        self.workspace = workspace
        self.command_policy = command_policy or CommandPolicy()
        self.explicit = tuple(explicit)

    def plan(self) -> VerificationPlan:
        if self.explicit:
            checks = self._accepted(self.explicit)
            if checks:
                return VerificationPlan(checks, PlanSource.EXPLICIT)
        from_instructions = self._accepted(self._from_agents_md())
        if from_instructions:
            return VerificationPlan(from_instructions, PlanSource.AGENTS_MD)
        detected = self._accepted(self._from_project_config())
        if detected:
            return VerificationPlan(detected, PlanSource.PROJECT_CONFIG)
        return VerificationPlan((), PlanSource.NONE)

    def _accepted(self, checks: Sequence[VerificationCheck]) -> tuple[VerificationCheck, ...]:
        allowed: list[VerificationCheck] = []
        for check in checks:
            if not check.command:
                continue
            classification = self.command_policy.classify(check.command, ".")
            if classification.tier is RiskTier.R2_LOCAL_EXECUTION:
                allowed.append(check)
        return tuple(allowed)

    def _from_agents_md(self) -> tuple[VerificationCheck, ...]:
        instructions = self.workspace.root / "AGENTS.md"
        if not instructions.is_file():
            return ()
        try:
            text = instructions.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return ()
        section = self._SECTION.search(text)
        if section is None:
            return ()
        body = section.group(1)
        fenced = self._FENCE.search(body)
        lines = (fenced.group(1) if fenced else body).splitlines()
        checks: list[VerificationCheck] = []
        for raw in lines:
            line = raw.strip().lstrip("-").strip()
            if not line or line.startswith("#"):
                continue
            try:
                argv = parse_command(line)
            except AthenaRuntimeError:
                continue
            checks.append(VerificationCheck(line, _infer_kind(argv), argv))
        return tuple(checks)

    def _from_project_config(self) -> tuple[VerificationCheck, ...]:
        checks: list[VerificationCheck] = []
        checks.extend(self._from_pyproject())
        checks.extend(self._from_package_json())
        return tuple(checks)

    def _from_pyproject(self) -> tuple[VerificationCheck, ...]:
        config = self.workspace.root / "pyproject.toml"
        if not config.is_file():
            return ()
        try:
            data = tomllib.loads(config.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            return ()
        tools = data.get("tool", {})
        if not isinstance(tools, dict):
            return ()
        checks: list[VerificationCheck] = []
        if "pytest" in tools:
            checks.append(
                VerificationCheck("pytest", CheckKind.TEST, ("python", "-m", "pytest", "-q"))
            )
        if "ruff" in tools:
            checks.append(
                VerificationCheck("ruff", CheckKind.LINT, ("python", "-m", "ruff", "check", "."))
            )
        if "mypy" in tools:
            checks.append(VerificationCheck("mypy", CheckKind.TYPECHECK, ("python", "-m", "mypy")))
        return tuple(checks)

    def _from_package_json(self) -> tuple[VerificationCheck, ...]:
        config = self.workspace.root / "package.json"
        if not config.is_file():
            return ()
        try:
            data = json.loads(config.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return ()
        scripts = data.get("scripts") if isinstance(data, dict) else None
        if not isinstance(scripts, dict):
            return ()
        mapping = {
            "test": (CheckKind.TEST, ("npm", "test")),
            "lint": (CheckKind.LINT, ("npm", "run", "lint")),
            "build": (CheckKind.BUILD, ("npm", "run", "build")),
        }
        return tuple(
            VerificationCheck(name, kind, command)
            for name, (kind, command) in mapping.items()
            if name in scripts
        )


def _infer_kind(argv: tuple[str, ...]) -> CheckKind:
    joined = " ".join(argv).lower()
    if "pytest" in joined or "test" in joined:
        return CheckKind.TEST
    if "mypy" in joined or "tsc" in joined:
        return CheckKind.TYPECHECK
    if "ruff" in joined or "lint" in joined or "eslint" in joined:
        return CheckKind.LINT
    return CheckKind.BUILD


# --------------------------------------------------------------------------- integrity
