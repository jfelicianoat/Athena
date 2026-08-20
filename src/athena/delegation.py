"""The tool through which an agent asks for a task to be delegated.

Until now a subagent could only be started by Athena's own code. This is how the model
asks — and it asks for a *task*, not for an agent. The distinction is the whole naming
decision: `spawn_agent` would invite the model to think about infrastructure, and
infrastructure is not its business. It states a goal, what would count as done, and which
specialism it thinks fits; the runtime decides everything about how that happens.

Two rules make this safe to expose, and both are enforced rather than requested.

**A child's authority is a subset of its parent's.** Not "usually", not "by convention" —
`narrow` computes the intersection and there is no path that widens it. A parent that
cannot write cannot delegate a task that can, whatever role the model asked for.

**Delegation itself goes through the PermissionEngine.** The risk is not that a subagent
exists; it is what the subagent may do. Asking for a read-only explorer is an R0 question.
Asking for a coder that can run commands is not, and the person is asked.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from athena.errors import ToolValidationError
from athena.permissions import PermissionPolicy, PermissionRequest, RiskLevel, RiskTier
from athena.subagents import (
    DEFAULT_PROFILES,
    SubagentBrief,
    SubagentProfile,
    SubagentRole,
)
from athena.tools import ToolLoadPolicy, ToolResult, ToolSpec
from athena.types import JSONObject, JSONSchema
from athena.workspace import Workspace

DELEGATE_TASK_NAME = "delegate_task"


def narrow(parent: PermissionPolicy, child: PermissionPolicy) -> PermissionPolicy:
    """The authority a child may actually have: the intersection, never the union.

    Written as arithmetic rather than as a check that raises, because the safe answer
    always exists — a child asked for more than its parent has simply gets less. Raising
    would make the caller decide what to do about it, and there is only one right answer.
    """
    return PermissionPolicy(
        allow_workspace_writes=parent.allow_workspace_writes and child.allow_workspace_writes,
        allow_local_execution=parent.allow_local_execution and child.allow_local_execution,
    )


def confine(
    profile: SubagentProfile,
    parent_policy: PermissionPolicy,
    available_tools: frozenset[str],
) -> SubagentProfile:
    """Fit a profile inside its parent's authority and inside what actually exists.

    Both halves matter. The policy narrowing stops a delegate from doing more than its
    parent could; the toolset intersection stops it from being handed a name the parent's
    own registry never had — which is how a delegate would otherwise reach a tool the
    parent was deliberately not given.
    """
    permitted = tuple(name for name in profile.toolsets if name in available_tools)
    if not permitted:
        raise ToolValidationError(
            f"A {profile.role.value} would have no tools it is allowed to use",
            details={"role": profile.role.value},
        )
    return replace(profile, policy=narrow(parent_policy, profile.policy), toolsets=permitted)


@dataclass(frozen=True, slots=True)
class DelegationRequest:
    """What the model asked for, after the runtime has read it properly."""

    goal: str
    role: SubagentRole
    expected_output: str = ""
    acceptance_criteria: tuple[str, ...] = ()
    context_refs: tuple[str, ...] = ()

    def brief(self) -> SubagentBrief:
        constraints = (f"Expected output: {self.expected_output}",) if self.expected_output else ()
        return SubagentBrief(
            objective=self.goal,
            acceptance_criteria=self.acceptance_criteria,
            relevant_files=self.context_refs,
            constraints=constraints,
        )


def permission_request_for(
    profile: SubagentProfile, workspace: Workspace, goal: str
) -> PermissionRequest:
    """What the engine is asked about a delegation.

    The tier follows the *delegate's* authority, not the act of delegating. A read-only
    explorer changes nothing and is R0; one that can write is a workspace write; one that
    can run commands is local execution. Declaring a flat tier for "delegation" would make
    the cheapest and the most dangerous case indistinguishable.
    """
    if profile.policy.allow_local_execution:
        tier = RiskTier.R2_LOCAL_EXECUTION
        risk = RiskLevel.MEDIUM
    elif profile.policy.allow_workspace_writes:
        tier = RiskTier.R1_WORKSPACE_WRITE
        risk = RiskLevel.MEDIUM
    else:
        tier = RiskTier.R0_READ_ONLY
        risk = RiskLevel.LOW
    read_only = not (profile.policy.allow_workspace_writes or profile.policy.allow_local_execution)
    return PermissionRequest(
        tool_name=DELEGATE_TASK_NAME,
        operation="delegate",
        action=f"run a {profile.role.value} on: {goal}",
        workspace=workspace,
        risk=risk,
        tier=tier,
        is_read_only=read_only,
        is_destructive=False,
        reason=f"The agent wants a {profile.role.value} to handle part of the work.",
        possible_effects=(f"A delegate with these tools: {', '.join(profile.toolsets)}",),
        arguments={"role": profile.role.value, "goal": goal},
    )


_SCHEMA: JSONSchema = {
    "type": "object",
    "required": ["goal", "role", "acceptance_criteria"],
    "properties": {
        "goal": {
            "type": "string",
            "description": "One concrete objective, stated the way you would state it to a "
            "colleague who has not seen this conversation.",
        },
        "role": {
            "type": "string",
            "enum": [role.value for role in SubagentRole],
            "description": "explorer reads and reports; coder changes code; verifier runs "
            "the project's checks.",
        },
        "expected_output": {"type": "string"},
        "acceptance_criteria": {
            "type": "array",
            "items": {"type": "string"},
            "description": "How someone else would know this task is done. Required: a "
            "task nobody can check is not delegated.",
        },
        "context_refs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Files the delegate should start from. It does not see this "
            "conversation, so anything it needs must be named here.",
        },
    },
}


def parse_delegation(arguments: JSONObject) -> DelegationRequest:
    """Read the model's request, refusing rather than guessing.

    An unrecognised role is refused instead of defaulted, for the same reason the plan
    parser refuses one: quietly turning an invented specialism into `coder` would hand a
    write-capable toolset to work that was meant to be read-only.
    """
    goal = arguments.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise ToolValidationError("delegate_task needs a goal")
    raw_role = arguments.get("role")
    try:
        role = SubagentRole(raw_role) if isinstance(raw_role, str) else None
    except ValueError as exc:
        raise ToolValidationError(f"Unknown role: {raw_role!r}") from exc
    if role is None:
        raise ToolValidationError("delegate_task needs a role")
    criteria = _strings(arguments.get("acceptance_criteria"))
    if not criteria:
        # The same bar the planner applies. A delegate that cannot be checked will report
        # success on its own word, which is what verification exists to refuse.
        raise ToolValidationError("delegate_task needs at least one acceptance criterion")
    expected = arguments.get("expected_output")
    return DelegationRequest(
        goal=goal.strip(),
        role=role,
        expected_output=expected.strip() if isinstance(expected, str) else "",
        acceptance_criteria=criteria,
        context_refs=_strings(arguments.get("context_refs")),
    )


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


#: What a delegation returns, so a caller knows the shape without running one.
_OUTPUT_SCHEMA: JSONSchema = {
    "type": "object",
    "properties": {
        "role": {"type": "string"},
        "outcome": {"type": "string"},
        "summary": {"type": "string"},
        "files_changed": {"type": "array", "items": {"type": "string"}},
    },
}

DELEGATE_TASK_SPEC = ToolSpec(
    name=DELEGATE_TASK_NAME,
    description=(
        "Hand one self-contained task to a specialist that does not see this "
        "conversation. Use it when a part of the work has its own objective and its own "
        "way of being checked. Do not use it to split work that has a single output."
    ),
    input_schema=_SCHEMA,
    output_schema=_OUTPUT_SCHEMA,
    risk=RiskLevel.MEDIUM,
    #: A delegate's answer is a summary, not a transcript, so it stays small by design.
    max_result_size_chars=8_000,
    load_policy=ToolLoadPolicy.CORE,
    search_hint="delegate subagent explorer coder verifier task",
)


def profile_for_request(
    request: DelegationRequest,
    parent_policy: PermissionPolicy,
    available_tools: frozenset[str],
) -> SubagentProfile:
    """The profile a delegation actually gets, after narrowing."""
    profile = DEFAULT_PROFILES.get(request.role)
    if profile is None:  # pragma: no cover - SubagentRole is closed
        raise ToolValidationError(f"No profile for role: {request.role.value}")
    return confine(profile, parent_policy, available_tools)


def describe_result(role: SubagentRole, summary: str, files: tuple[str, ...]) -> ToolResult:
    """What the parent sees. A summary and what changed, never the child's transcript."""
    lines = [f"{role.value} finished.", summary.strip()]
    if files:
        lines.append("Files changed: " + ", ".join(files))
    return ToolResult(call_id="", output="\n".join(line for line in lines if line))


__all__ = [
    "DELEGATE_TASK_NAME",
    "DELEGATE_TASK_SPEC",
    "DelegationRequest",
    "confine",
    "describe_result",
    "narrow",
    "parse_delegation",
    "permission_request_for",
    "profile_for_request",
]
