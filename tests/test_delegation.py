"""Delegation, and the one rule that has to be true rather than intended.

A subagent is not dangerous because it exists. It is dangerous if it can do something the
agent that asked for it could not — which is how a read-only run acquires a writer by
asking politely. `narrow` makes that arithmetic instead of a promise, and these tests are
the arithmetic being checked in every direction.
"""

from __future__ import annotations

import pytest

from athena.delegation import (
    DELEGATE_TASK_NAME,
    DELEGATE_TASK_SPEC,
    DelegationRequest,
    confine,
    narrow,
    parse_delegation,
    permission_request_for,
    profile_for_request,
)
from athena.errors import ToolValidationError
from athena.permissions import (
    PermissionDecision,
    PermissionPolicy,
    PolicyPermissionEngine,
    RiskTier,
)
from athena.subagents import DEFAULT_PROFILES, SubagentRole
from athena.workspace import Workspace

ALL_TOOLS = frozenset(
    {
        "glob",
        "grep",
        "read_file",
        "read_range",
        "list_directory",
        "git_status",
        "git_diff",
        "git_log",
        "git_show",
        "write_file",
        "edit_file",
        "bash",
    }
)

FULL = PermissionPolicy(allow_workspace_writes=True, allow_local_execution=True)
READ_ONLY = PermissionPolicy()
WRITES_ONLY = PermissionPolicy(allow_workspace_writes=True)


# ------------------------------------------------------- the subset rule, arithmetically


@pytest.mark.parametrize(
    ("parent", "child", "writes", "execution"),
    [
        (FULL, FULL, True, True),
        (FULL, READ_ONLY, False, False),
        (READ_ONLY, FULL, False, False),
        (WRITES_ONLY, FULL, True, False),
        (FULL, WRITES_ONLY, True, False),
        (READ_ONLY, READ_ONLY, False, False),
    ],
    ids=[
        "both-full",
        "parent-full-child-narrow",
        "parent-narrow-child-asks-for-more",
        "parent-writes-child-asks-exec",
        "child-narrower-than-parent",
        "neither",
    ],
)
def test_a_child_never_gets_more_than_its_parent(
    parent: PermissionPolicy, child: PermissionPolicy, writes: bool, execution: bool
) -> None:
    """The intersection, in every combination. There is no path here that widens."""
    result = narrow(parent, child)

    assert result.allow_workspace_writes is writes
    assert result.allow_local_execution is execution


def test_narrowing_is_the_intersection_not_the_union() -> None:
    # Stated as its own case because "union" is the bug this would be if written the
    # obvious wrong way, and it would look correct in the both-full test above.
    result = narrow(READ_ONLY, FULL)

    assert not result.allow_workspace_writes
    assert not result.allow_local_execution


def test_a_read_only_parent_cannot_delegate_a_writer() -> None:
    """The scenario the rule exists for.

    A run that was deliberately given no write capability asks for a coder. It gets a
    coder — with nothing it can write.
    """
    profile = confine(DEFAULT_PROFILES[SubagentRole.CODER], READ_ONLY, ALL_TOOLS)

    assert not profile.policy.allow_workspace_writes
    assert not profile.policy.allow_local_execution
    assert profile.role is SubagentRole.CODER, "the role is honoured; the authority is not"


def test_a_delegate_cannot_reach_a_tool_the_parent_never_had() -> None:
    """Policy narrowing is not enough on its own.

    A profile names tools by string. If the parent's registry never held `bash`, handing
    the child a profile that names it would be reaching around the parent's own limits.
    """
    without_bash = ALL_TOOLS - {"bash"}

    profile = confine(DEFAULT_PROFILES[SubagentRole.CODER], FULL, without_bash)

    assert "bash" not in profile.toolsets
    assert "edit_file" in profile.toolsets, "what does exist is still available"


def test_a_delegate_with_nothing_left_is_refused_rather_than_started() -> None:
    # An agent with an empty toolset would burn a model call to discover it can do
    # nothing, and would report that as its own failure.
    with pytest.raises(ToolValidationError):
        confine(DEFAULT_PROFILES[SubagentRole.EXPLORER], FULL, frozenset({"bash"}))


def test_confining_does_not_mutate_the_shared_profile() -> None:
    # `DEFAULT_PROFILES` is module state. One narrowed delegation must not quietly narrow
    # every future one.
    before = DEFAULT_PROFILES[SubagentRole.CODER].policy.allow_workspace_writes

    confine(DEFAULT_PROFILES[SubagentRole.CODER], READ_ONLY, ALL_TOOLS)

    assert DEFAULT_PROFILES[SubagentRole.CODER].policy.allow_workspace_writes is before


# --------------------------------------------------------------- delegation is permissioned


def test_the_tier_follows_what_the_delegate_may_do(tmp_path: object) -> None:
    """A flat tier for "delegation" would make the cheapest and the worst case look alike."""
    workspace = Workspace.from_path(tmp_path)  # type: ignore[arg-type]

    explorer = permission_request_for(
        confine(DEFAULT_PROFILES[SubagentRole.EXPLORER], FULL, ALL_TOOLS), workspace, "look"
    )
    coder = permission_request_for(
        confine(DEFAULT_PROFILES[SubagentRole.CODER], FULL, ALL_TOOLS), workspace, "fix"
    )

    assert explorer.tier is RiskTier.R0_READ_ONLY
    assert explorer.is_read_only
    assert coder.tier in (RiskTier.R1_WORKSPACE_WRITE, RiskTier.R2_LOCAL_EXECUTION)
    assert not coder.is_read_only


def test_a_narrowed_coder_is_asked_about_as_the_reader_it_became(tmp_path: object) -> None:
    # The question follows the authority, not the label. A coder that cannot write is a
    # read-only delegation and should not prompt as though it were not.
    workspace = Workspace.from_path(tmp_path)  # type: ignore[arg-type]

    request = permission_request_for(
        confine(DEFAULT_PROFILES[SubagentRole.CODER], READ_ONLY, ALL_TOOLS), workspace, "fix"
    )

    assert request.tier is RiskTier.R0_READ_ONLY
    assert request.is_read_only


def test_the_engine_allows_a_reader_and_asks_about_a_writer(tmp_path: object) -> None:
    workspace = Workspace.from_path(tmp_path)  # type: ignore[arg-type]
    engine = PolicyPermissionEngine(PermissionPolicy())

    explorer = permission_request_for(
        confine(DEFAULT_PROFILES[SubagentRole.EXPLORER], FULL, ALL_TOOLS), workspace, "look"
    )
    coder = permission_request_for(
        confine(DEFAULT_PROFILES[SubagentRole.CODER], FULL, ALL_TOOLS), workspace, "fix"
    )

    assert engine.decide(explorer) is PermissionDecision.ALLOW
    assert engine.decide(coder) is PermissionDecision.ASK


def test_the_request_says_what_the_delegate_will_be_able_to_do(tmp_path: object) -> None:
    # Approving blind is not approving. The effect line names the tools.
    workspace = Workspace.from_path(tmp_path)  # type: ignore[arg-type]

    request = permission_request_for(
        confine(DEFAULT_PROFILES[SubagentRole.CODER], FULL, ALL_TOOLS), workspace, "fix add"
    )

    assert "fix add" in request.action
    assert any("edit_file" in effect for effect in request.possible_effects)


# ------------------------------------------------------------------------ reading the ask


def test_a_well_formed_request_is_read() -> None:
    request = parse_delegation(
        {
            "goal": "find where authentication fails",
            "role": "explorer",
            "expected_output": "the failing call path",
            "acceptance_criteria": ["names a file and a function"],
            "context_refs": ["auth.py"],
        }
    )

    assert isinstance(request, DelegationRequest)
    assert request.role is SubagentRole.EXPLORER
    assert request.acceptance_criteria == ("names a file and a function",)
    assert request.context_refs == ("auth.py",)


def test_a_task_nobody_can_check_is_not_delegated() -> None:
    """The same bar the planner applies, for the same reason.

    A delegate without acceptance criteria reports success on its own word, which is what
    verification exists to refuse.
    """
    with pytest.raises(ToolValidationError):
        parse_delegation({"goal": "improve things", "role": "coder"})


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"role": "explorer", "acceptance_criteria": ["x"]},
        {"goal": "   ", "role": "explorer", "acceptance_criteria": ["x"]},
        {"goal": "do it", "acceptance_criteria": ["x"]},
        {"goal": "do it", "role": "architect", "acceptance_criteria": ["x"]},
        {"goal": "do it", "role": "explorer", "acceptance_criteria": []},
    ],
    ids=["empty", "no-goal", "blank-goal", "no-role", "invented-role", "no-criteria"],
)
def test_an_unusable_request_is_refused(arguments: dict[str, object]) -> None:
    with pytest.raises(ToolValidationError):
        parse_delegation(arguments)  # type: ignore[arg-type]


def test_an_invented_role_is_refused_rather_than_defaulted() -> None:
    # Turning an unrecognised specialism into `coder` would hand a write-capable toolset
    # to work the model meant to be read-only.
    with pytest.raises(ToolValidationError) as caught:
        parse_delegation({"goal": "do it", "role": "architect", "acceptance_criteria": ["x"]})

    assert "architect" in str(caught.value)


def test_the_brief_carries_the_task_and_not_the_conversation() -> None:
    request = parse_delegation(
        {
            "goal": "find the bug",
            "role": "explorer",
            "expected_output": "a file and a line",
            "acceptance_criteria": ["names the cause"],
            "context_refs": ["auth.py"],
        }
    )

    brief = request.brief()

    assert brief.objective == "find the bug"
    assert brief.relevant_files == ("auth.py",)
    assert any("a file and a line" in item for item in brief.constraints)
    assert brief.findings == (), "a fresh delegation inherits nothing"


# ------------------------------------------------------------------------------ the spec


def test_the_tool_is_named_for_the_task_not_the_agent() -> None:
    """`spawn_agent` would invite the model to think about infrastructure.

    The unit Athena reasons in is the task; the runtime decides what runs it.
    """
    assert DELEGATE_TASK_NAME == "delegate_task"
    assert DELEGATE_TASK_SPEC.name == DELEGATE_TASK_NAME
    assert "agent" not in DELEGATE_TASK_SPEC.name


def test_the_schema_demands_what_the_runtime_will_demand() -> None:
    required = DELEGATE_TASK_SPEC.input_schema["required"]

    assert isinstance(required, list)
    assert set(required) == {"goal", "role", "acceptance_criteria"}


def test_the_description_tells_the_model_when_not_to_use_it() -> None:
    # A tool that only says what it does gets used for everything.
    assert "single output" in DELEGATE_TASK_SPEC.description


def test_a_profile_comes_back_already_confined() -> None:
    profile = profile_for_request(
        DelegationRequest(goal="fix it", role=SubagentRole.CODER), READ_ONLY, ALL_TOOLS
    )

    assert not profile.policy.allow_workspace_writes
