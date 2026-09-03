"""Integridad del cambio: que el diff no incluya cosas que nadie autorizo.

Un agente que borra tests para que pasen los tests ha cumplido la letra del
encargo y no su intencion. Esto es lo que lo detecta.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IntegrityAuthorization:
    """Explicit permission to do the things that would otherwise look like cheating."""

    allow_test_removal: bool = False
    allow_test_skipping: bool = False
    allow_assertion_removal: bool = False
    allow_lint_suppression: bool = False


@dataclass(frozen=True, slots=True)
class IntegrityFinding:
    kind: str
    detail: str
    lines: tuple[str, ...]


class ChangeIntegrityPolicy:
    """Refuses a green verdict obtained by weakening what does the verifying."""

    _REMOVED_TEST = re.compile(r"^-\s*(?:async\s+)?(?:def\s+test_|it\(|test\()")
    _ADDED_TEST = re.compile(r"^\+\s*(?:async\s+)?(?:def\s+test_|it\(|test\()")
    _ADDED_SKIP = re.compile(
        r"^\+.*(pytest\.mark\.skip|pytest\.mark\.xfail|unittest\.skip|@skip\b"
        r"|\.skip\(|\.only\(|xit\(|xdescribe\()"
    )
    _REMOVED_ASSERT = re.compile(r"^-\s*(assert\b|expect\(|self\.assert)")
    _ADDED_ASSERT = re.compile(r"^\+\s*(assert\b|expect\(|self\.assert)")
    _ADDED_SUPPRESSION = re.compile(
        r"^\+.*(#\s*noqa|#\s*type:\s*ignore|--exit-zero|ignore_errors\s*=\s*true"
        r"|--no-verify|eslint-disable|# ruff: noqa)"
    )

    def __init__(self, authorization: IntegrityAuthorization | None = None) -> None:
        self.authorization = authorization or IntegrityAuthorization()

    def inspect(self, diff: str) -> tuple[IntegrityFinding, ...]:
        removed_tests: list[str] = []
        added_tests = 0
        added_skips: list[str] = []
        removed_assertions = 0
        added_assertions = 0
        removed_assertion_lines: list[str] = []
        suppressions: list[str] = []
        for line in diff.splitlines():
            if line.startswith("---") or line.startswith("+++"):
                continue
            if self._REMOVED_TEST.match(line):
                removed_tests.append(line.strip())
            if self._ADDED_TEST.match(line):
                added_tests += 1
            if self._ADDED_SKIP.match(line):
                added_skips.append(line.strip())
            if self._REMOVED_ASSERT.match(line):
                removed_assertions += 1
                removed_assertion_lines.append(line.strip())
            if self._ADDED_ASSERT.match(line):
                added_assertions += 1
            if self._ADDED_SUPPRESSION.match(line):
                suppressions.append(line.strip())

        findings: list[IntegrityFinding] = []
        # Net counting, so renaming or restructuring a test is not mistaken for deleting it.
        net_removed_tests = len(removed_tests) - added_tests
        if net_removed_tests > 0 and not self.authorization.allow_test_removal:
            findings.append(
                IntegrityFinding(
                    "test_removed",
                    f"{net_removed_tests} test definition(s) were deleted",
                    tuple(removed_tests[:10]),
                )
            )
        if added_skips and not self.authorization.allow_test_skipping:
            findings.append(
                IntegrityFinding(
                    "test_skipped",
                    f"{len(added_skips)} test(s) were skipped or narrowed",
                    tuple(added_skips[:10]),
                )
            )
        if removed_assertions > added_assertions and not self.authorization.allow_assertion_removal:
            findings.append(
                IntegrityFinding(
                    "assertions_weakened",
                    f"{removed_assertions - added_assertions} assertion(s) were removed",
                    tuple(removed_assertion_lines[:10]),
                )
            )
        if suppressions and not self.authorization.allow_lint_suppression:
            findings.append(
                IntegrityFinding(
                    "checks_suppressed",
                    f"{len(suppressions)} lint or type suppression(s) were added",
                    tuple(suppressions[:10]),
                )
            )
        return tuple(findings)


# --------------------------------------------------------------------------- policies
