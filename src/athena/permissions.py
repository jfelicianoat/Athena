"""Deterministic permission contracts and the engine that owns every decision.

The model may request an action; it can never authorize one. Decisions are a pure
function of the request, the declared capability tier, and local policy.
"""

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


class RiskTier(StrEnum):
    """Capability tiers. A tool declares its tier; the engine decides the outcome."""

    R0_READ_ONLY = "r0_read_only"
    R1_WORKSPACE_WRITE = "r1_workspace_write"
    R2_LOCAL_EXECUTION = "r2_local_execution"
    R3_EXTERNAL_OR_IRREVERSIBLE = "r3_external_or_irreversible"
    R4_FORBIDDEN = "r4_forbidden"


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    """Everything an interface needs to render an informed approval prompt."""

    tool_name: str
    operation: str
    workspace: Workspace
    risk: RiskLevel
    tier: RiskTier
    is_read_only: bool
    is_destructive: bool
    action: str = ""
    reason: str = ""
    possible_effects: tuple[str, ...] = ()
    is_concurrency_safe: bool = False
    resources: tuple[str, ...] = ()
    arguments: JSONObject = field(default_factory=dict)


@runtime_checkable
class PermissionEngine(Protocol):
    """Sole authority for ALLOW / ASK / DENY decisions."""

    def decide(self, request: PermissionRequest) -> PermissionDecision: ...


@runtime_checkable
class PermissionPrompt(Protocol):
    """Interface port that resolves an ASK into a single-use ALLOW or DENY.

    Implementations must decide one call at a time. There is deliberately no
    "always allow" memory: a broad standing grant would move the security boundary
    from the engine to whatever the model asked for first.
    """

    async def confirm(self, request: PermissionRequest) -> PermissionDecision: ...


class DenyingPermissionPrompt:
    """Default for non-interactive runs: an unanswered ASK is a DENY."""

    async def confirm(self, request: PermissionRequest) -> PermissionDecision:
        del request
        return PermissionDecision.DENY


@dataclass(frozen=True, slots=True)
class PermissionPolicy:
    """Local policy. Absent an explicit grant, capability tiers escalate to ASK."""

    allow_workspace_writes: bool = False
    allow_local_execution: bool = False


class PolicyPermissionEngine:
    """Maps capability tiers to decisions and refuses self-inconsistent requests."""

    def __init__(self, policy: PermissionPolicy | None = None) -> None:
        self.policy = policy or PermissionPolicy()

    def decide(self, request: PermissionRequest) -> PermissionDecision:
        if request.tier is RiskTier.R4_FORBIDDEN:
            return PermissionDecision.DENY
        if not self._tier_matches_declared_effects(request):
            return PermissionDecision.DENY
        if request.tier is RiskTier.R3_EXTERNAL_OR_IRREVERSIBLE:
            return PermissionDecision.ASK
        if request.is_destructive:
            return PermissionDecision.ASK
        if request.tier is RiskTier.R2_LOCAL_EXECUTION:
            if self.policy.allow_local_execution:
                return PermissionDecision.ALLOW
            return PermissionDecision.ASK
        if request.tier is RiskTier.R1_WORKSPACE_WRITE:
            if self.policy.allow_workspace_writes:
                return PermissionDecision.ALLOW
            return PermissionDecision.ASK
        return PermissionDecision.ALLOW

    @staticmethod
    def _tier_matches_declared_effects(request: PermissionRequest) -> bool:
        """R0 is the only unconditional ALLOW, so a request claiming it must be honest.

        Over-declaring a tier is safe and stays permitted; under-declaring is not.
        """
        if request.tier is RiskTier.R0_READ_ONLY:
            return request.is_read_only and not request.is_destructive
        return True


class ReadOnlyPermissionEngine:
    """Investigation-only policy: allow non-destructive reads, deny everything else."""

    def decide(self, request: PermissionRequest) -> PermissionDecision:
        if request.is_destructive or not request.is_read_only:
            return PermissionDecision.DENY
        return PermissionDecision.ALLOW
