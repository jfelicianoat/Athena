"""Deterministic permission contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from athena.types import JSONObject
from athena.workspace import Workspace


class PermissionDecision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    tool_name: str
    operation: str
    workspace: Workspace
    risk: RiskLevel
    is_read_only: bool
    is_destructive: bool
    resources: tuple[str, ...] = ()
    arguments: JSONObject = field(default_factory=dict)
    rationale: str | None = None


@runtime_checkable
class PermissionEngine(Protocol):
    """Sole authority for ALLOW / ASK / DENY decisions."""

    def decide(self, request: PermissionRequest) -> PermissionDecision: ...


class ReadOnlyPermissionEngine:
    """Default H1 policy: allow only non-destructive reads inside the workspace."""

    def decide(self, request: PermissionRequest) -> PermissionDecision:
        if request.is_destructive or not request.is_read_only:
            return PermissionDecision.DENY
        return PermissionDecision.ALLOW
