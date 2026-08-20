"""Planning: when not to, and what a plan has to survive before it counts as one.

Most of these are refusals. The layer earns its place by saying no — no to decomposing a
goal that has one output, no to a graph with a loop in it, no to a level of hierarchy that
records no decision — and every one of those noes is a thing a model will produce given the
chance.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence

import pytest

from athena.cancellation import CancellationSource
from athena.errors import AthenaRuntimeError
from athena.events import EventName, ModelEvent
from athena.models import (
    ModelCapabilities,
    ModelHealth,
    ModelHealthStatus,
    ModelProvider,
    ModelRequest,
    ModelResponse,
)
from athena.planning import (
    CyclicPlanError,
    DecompositionPolicy,
    DecompositionSignals,
    DuplicateTaskIdError,
    InvalidTransitionError,
    NonAtomicTaskError,
    PlanLimitExceededError,
    Planner,
    PlanningLimits,
    PlanParseError,
    PlanStatus,
    RedundantTaskError,
    TaskGraph,
    TaskNode,
    UnknownDependencyError,
    parse_plan,
)
from athena.subagents import SubagentRole
from athena.types import JSONValue


def node(
    task_id: str,
    *,
    parent: str | None = None,
    depends: Sequence[str] = (),
    output: str | None = None,
    criteria: Sequence[str] = ("it can be checked",),
    role: SubagentRole = SubagentRole.CODER,
) -> TaskNode:
    return TaskNode(
        id=task_id,
        goal=f"do {task_id}",
        expected_output=output or f"{task_id} exists",
        parent_id=parent,
        acceptance_criteria=tuple(criteria),
        dependencies=tuple(depends),
        suggested_role=role,
    )


class _ScriptedProvider(ModelProvider):
    """Returns whatever it was told to. The planner must not trust it any more than a real
    provider, which is the only reason this is enough to test with."""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest, cancellation: object) -> ModelResponse:
        del cancellation
        self.requests.append(request)
        return ModelResponse(self.answer, "scripted", "stop")

    async def stream(
        self, request: ModelRequest, cancellation: object
    ) -> AsyncIterator[ModelEvent]:
        del request, cancellation
        if False:
            yield ModelEvent(EventName.MODEL_COMPLETED, "never")

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(False, True, True)

    async def health(self, cancellation: object) -> ModelHealth:
        del cancellation
        return ModelHealth(ModelHealthStatus.HEALTHY)


# --------------------------------------------------------------- should we plan at all


def test_a_simple_goal_is_not_decomposed() -> None:
    """The common case, and the one a planning layer most often gets wrong.

    One output means one thing to verify. A graph would add hand-offs between steps that
    were never independent and buy no assurance at all.
    """
    signals = DecompositionSignals(independently_verifiable_outputs=1, subsystems_touched=1)

    decision = DecompositionPolicy().assess(signals)

    assert decision.decompose is False
    assert "AgentLoop" in decision.explanation


def test_one_output_is_not_decomposed_however_risky_it_looks() -> None:
    # Risk is a reason to verify harder, not a reason to split. Splitting one output into
    # three tasks produces three ways to be told it went fine and still one thing to check.
    signals = DecompositionSignals(
        independently_verifiable_outputs=1,
        high_implementation_risk=True,
        subsystems_touched=4,
        distinct_roles_required=3,
        parallelisable_investigation=True,
    )

    assert DecompositionPolicy().assess(signals).decompose is False


def test_one_weak_signal_is_not_enough() -> None:
    signals = DecompositionSignals(independently_verifiable_outputs=2)

    decision = DecompositionPolicy().assess(signals)

    assert decision.decompose is False
    assert decision.reasons == ("multiple independently verifiable outputs",)


def test_a_complex_goal_is_decomposed_and_says_why() -> None:
    signals = DecompositionSignals(
        independently_verifiable_outputs=3,
        has_meaningful_dependencies=True,
        subsystems_touched=2,
        distinct_roles_required=2,
    )

    decision = DecompositionPolicy().assess(signals)

    assert decision.decompose is True
    assert len(decision.reasons) == 4
    assert "multiple independently verifiable outputs" in decision.reasons


def test_the_policy_is_configurable_without_being_vague() -> None:
    # A deployment may want a lower bar. What it may not have is a bar nobody can state.
    signals = DecompositionSignals(independently_verifiable_outputs=2)

    assert DecompositionPolicy(minimum_criteria=1).assess(signals).decompose is True


# --------------------------------------------------------------------- graph validation


def test_a_valid_plan_becomes_a_dag_in_dependency_order() -> None:
    graph = TaskGraph.build(
        [
            node("survey", role=SubagentRole.EXPLORER),
            node("api", depends=["survey"]),
            node("storage", depends=["survey"]),
            node("check", depends=["api", "storage"], role=SubagentRole.VERIFIER),
        ]
    )

    order = [task.id for task in graph.topological_order()]

    assert order.index("survey") < order.index("api")
    assert order.index("api") < order.index("check")
    assert order.index("storage") < order.index("check")
    assert len(graph) == 4


def test_a_cycle_from_the_model_is_rejected_and_names_the_loop() -> None:
    """The failure a model produces most readily, and the one that would hang a runner."""
    with pytest.raises(CyclicPlanError) as caught:
        TaskGraph.build([node("a", depends=["b"]), node("b", depends=["a"])])

    looping = caught.value.details["tasks"]
    assert isinstance(looping, list)
    assert set(looping) == {"a", "b"}


def test_a_task_that_depends_on_itself_is_a_cycle() -> None:
    with pytest.raises(CyclicPlanError):
        TaskGraph.build([node("a", depends=["a"])])


def test_a_dependency_on_a_task_that_is_not_in_the_plan_is_rejected() -> None:
    # A model that forgets to emit a task it referenced leaves a plan that can never run.
    with pytest.raises(UnknownDependencyError):
        TaskGraph.build([node("a", depends=["imagined"])])


def test_duplicate_ids_are_rejected_before_anything_else() -> None:
    # Reported as a duplicate rather than as a strange dependency failure downstream.
    with pytest.raises(DuplicateTaskIdError):
        TaskGraph.build([node("a"), node("a")])


def test_a_parent_outside_the_plan_is_rejected() -> None:
    with pytest.raises(UnknownDependencyError):
        TaskGraph.build([node("child", parent="ghost")])


def test_a_leaf_nobody_can_check_is_not_atomic() -> None:
    """ADR-012 in the planning layer.

    A task with no acceptance criteria will be reported finished on the model's word, which
    is the single thing verification exists to refuse.
    """
    with pytest.raises(NonAtomicTaskError) as caught:
        TaskGraph.build([node("a", criteria=())])

    missing = caught.value.details["missing"]
    assert isinstance(missing, list)
    assert "acceptance_criteria" in missing


def test_an_interior_node_need_not_be_atomic() -> None:
    # A parent is a heading. Demanding criteria of it would make every plan one level deep.
    graph = TaskGraph.build(
        [
            TaskNode(id="epic", goal="the whole thing", expected_output="done"),
            node("a", parent="epic"),
            node("b", parent="epic"),
        ]
    )

    assert len(graph) == 3


# ------------------------------------------------------------------------ microtasks


def test_a_parent_with_one_child_is_collapsed() -> None:
    """A single child records no decision: nothing was divided.

    Left in, these accumulate — every replan adds another — until the graph is mostly
    structure and hardly any work.
    """
    graph = TaskGraph.build(
        [
            node("setup"),
            TaskNode(
                id="wrapper",
                goal="the middle",
                expected_output="middle",
                dependencies=("setup",),
            ),
            node("real", parent="wrapper"),
        ]
    )

    assert [task.id for task in graph.nodes] == ["setup", "real"]
    # The child stands where the parent stood, so what depended on the parent still holds.
    assert graph.get("real").dependencies == ("setup",)
    assert graph.get("real").parent_id is None


def test_collapsing_is_repeated_until_nothing_is_left_to_collapse() -> None:
    graph = TaskGraph.build(
        [
            TaskNode(id="a", goal="a", expected_output="a"),
            TaskNode(id="b", goal="b", expected_output="b", parent_id="a"),
            node("c", parent="b"),
        ]
    )

    assert [task.id for task in graph.nodes] == ["c"]


def test_two_siblings_promising_the_same_output_are_one_task_written_twice() -> None:
    with pytest.raises(RedundantTaskError):
        TaskGraph.build(
            [
                TaskNode(id="epic", goal="epic", expected_output="epic"),
                node("a", parent="epic", output="the migration runs"),
                node("b", parent="epic", output="The Migration Runs"),
                node("c", parent="epic", output="something else"),
            ]
        )


# ---------------------------------------------------------------------- recursion limits


def test_a_plan_larger_than_the_limit_is_rejected() -> None:
    with pytest.raises(PlanLimitExceededError):
        TaskGraph.build([node(f"t{index}") for index in range(5)], PlanningLimits(max_tasks=4))


def test_a_plan_deeper_than_the_limit_is_rejected() -> None:
    # Two children at each level, so nothing collapses and the depth is real. A chain of
    # single children would be folded away first, which is the collapse doing its job
    # rather than the limit doing its job.
    with pytest.raises(PlanLimitExceededError):
        TaskGraph.build(
            [
                TaskNode(id="root", goal="root", expected_output="root"),
                TaskNode(id="l", goal="l", expected_output="l", parent_id="root"),
                TaskNode(id="r", goal="r", expected_output="r", parent_id="root"),
                node("l1", parent="l", output="l1"),
                node("l2", parent="l", output="l2"),
                node("r1", parent="r", output="r1"),
                node("r2", parent="r", output="r2"),
            ],
            PlanningLimits(max_depth=2, max_children=8),
        )


def test_a_chain_of_single_children_is_collapsed_rather_than_hitting_the_depth_limit() -> None:
    # The two rules meet here, and the collapse is the one that should win: a plan that is
    # deep only because nothing was actually divided is not too deep, it is over-nested.
    graph = TaskGraph.build(
        [
            TaskNode(id="a", goal="a", expected_output="a"),
            TaskNode(id="b", goal="b", expected_output="b", parent_id="a"),
            node("c1", parent="b", output="c1"),
            node("c2", parent="b", output="c2"),
        ],
        PlanningLimits(max_depth=2),
    )

    assert graph.depth_of("c1") == 1
    assert "a" not in graph


def test_too_many_children_is_rejected() -> None:
    with pytest.raises(PlanLimitExceededError):
        TaskGraph.build(
            [
                TaskNode(id="epic", goal="epic", expected_output="epic"),
                *(node(f"c{index}", parent="epic") for index in range(4)),
            ],
            PlanningLimits(max_children=3),
        )


def test_limits_must_be_positive() -> None:
    with pytest.raises(ValueError):
        PlanningLimits(max_depth=0)


# ------------------------------------------------------------------------- fan-out / in


def test_fan_out_makes_several_tasks_ready_at_once() -> None:
    graph = TaskGraph.build(
        [
            node("survey", role=SubagentRole.EXPLORER),
            node("api", depends=["survey"]),
            node("storage", depends=["survey"]),
            node("docs", depends=["survey"]),
        ]
    )
    graph.transition("survey", PlanStatus.READY)
    graph.transition("survey", PlanStatus.RUNNING)
    graph.transition("survey", PlanStatus.COMPLETED)

    ready = {task.id for task in graph.ready()}

    assert ready == {"api", "storage", "docs"}


def test_fan_in_waits_for_every_dependency() -> None:
    graph = TaskGraph.build(
        [
            node("api"),
            node("storage"),
            node("check", depends=["api", "storage"], role=SubagentRole.VERIFIER),
        ]
    )
    for task_id in ("api",):
        graph.transition(task_id, PlanStatus.READY)
        graph.transition(task_id, PlanStatus.RUNNING)
        graph.transition(task_id, PlanStatus.COMPLETED)

    assert "check" not in {task.id for task in graph.ready()}

    graph.transition("storage", PlanStatus.READY)
    graph.transition("storage", PlanStatus.RUNNING)
    graph.transition("storage", PlanStatus.COMPLETED)

    assert "check" in {task.id for task in graph.ready()}


# ---------------------------------------------------------------------- state transitions


def test_a_task_cannot_be_completed_without_running() -> None:
    """The transition worth protecting.

    "Completed" is a claim that something was done and checked. A path into it that skips
    doing the work is a path to a plan that lies.
    """
    graph = TaskGraph.build([node("a")])

    with pytest.raises(InvalidTransitionError):
        graph.transition("a", PlanStatus.COMPLETED)


def test_an_attempt_is_counted_on_starting_not_on_finishing() -> None:
    # A task that crashes still spent an attempt. Counting only successes would let a
    # failing task loop forever under a budget it never appears to touch.
    graph = TaskGraph.build([node("a")])
    graph.transition("a", PlanStatus.READY)
    graph.transition("a", PlanStatus.RUNNING)
    graph.transition("a", PlanStatus.FAILED)

    assert graph.get("a").attempts == 1
    assert graph.total_attempts() == 1


def test_a_failure_blocks_what_was_waiting_on_it_transitively() -> None:
    graph = TaskGraph.build([node("a"), node("b", depends=["a"]), node("c", depends=["b"])])
    graph.transition("a", PlanStatus.READY)
    graph.transition("a", PlanStatus.RUNNING)
    graph.transition("a", PlanStatus.FAILED)

    assert graph.get("b").status is PlanStatus.BLOCKED
    assert graph.get("c").status is PlanStatus.BLOCKED
    assert graph.ready() == ()


def test_the_attempt_budget_is_enforced() -> None:
    graph = TaskGraph.build([node("a")], PlanningLimits(max_total_attempts=1))
    graph.transition("a", PlanStatus.READY)
    graph.transition("a", PlanStatus.RUNNING)
    graph.transition("a", PlanStatus.FAILED)
    graph.transition("a", PlanStatus.PENDING)
    graph.transition("a", PlanStatus.READY)

    with pytest.raises(PlanLimitExceededError):
        graph.transition("a", PlanStatus.RUNNING)


# ------------------------------------------------------------------------- replanning


def _diamond() -> TaskGraph:
    return TaskGraph.build(
        [
            node("survey", role=SubagentRole.EXPLORER),
            node("api", depends=["survey"]),
            node("storage", depends=["survey"]),
            node("check", depends=["api", "storage"], role=SubagentRole.VERIFIER),
        ]
    )


def test_a_verification_failure_affects_only_what_came_after_it() -> None:
    """The reason to plan as a graph rather than a list.

    What needs rethinking is the task that failed and the work that was going to consume
    its output — not the sibling that succeeded on a different subsystem, and not the
    dependency that produced exactly what it promised.
    """
    graph = _diamond()

    affected = graph.affected_subgraph("api")

    assert set(affected) == {"api", "check"}
    assert "storage" not in affected, "a sibling that succeeded is not replanned"
    assert "survey" not in affected, "a dependency that held is not replanned"


def test_replanning_replaces_the_subgraph_and_keeps_the_rest() -> None:
    graph = _diamond()
    for task_id in ("survey", "storage"):
        graph.transition(task_id, PlanStatus.READY)
        graph.transition(task_id, PlanStatus.RUNNING)
        graph.transition(task_id, PlanStatus.COMPLETED, verification={"status": "passed"})

    replanned = graph.replan_from(
        "api",
        [
            node("api", depends=["survey"], output="a smaller api"),
            node("check", depends=["api", "storage"], role=SubagentRole.VERIFIER),
        ],
    )

    assert replanned.get("storage").status is PlanStatus.COMPLETED
    assert replanned.get("storage").verification == {"status": "passed"}
    assert replanned.get("api").expected_output == "a smaller api"
    assert replanned.get("api").status is PlanStatus.PENDING


def test_a_replan_may_not_reach_outside_the_affected_subgraph() -> None:
    # Rewriting a completed task would discard evidence that was already produced.
    graph = _diamond()

    with pytest.raises(AthenaRuntimeError) as caught:
        graph.replan_from("api", [node("api"), node("survey", output="rewritten")])

    unexpected = caught.value.details["unexpected"]
    assert isinstance(unexpected, list)
    assert "survey" in unexpected


def test_a_replacement_never_inherits_a_result_it_did_not_earn() -> None:
    graph = _diamond()
    graph.transition("survey", PlanStatus.READY)
    graph.transition("survey", PlanStatus.RUNNING)
    graph.transition("survey", PlanStatus.COMPLETED)

    replanned = graph.replan_from(
        "api",
        [
            TaskNode(
                id="api",
                goal="do api",
                expected_output="api",
                dependencies=("survey",),
                acceptance_criteria=("checkable",),
                status=PlanStatus.COMPLETED,
                verification={"status": "passed"},
            ),
            node("check", depends=["api", "storage"], role=SubagentRole.VERIFIER),
        ],
    )

    assert replanned.get("api").status is PlanStatus.PENDING
    assert replanned.get("api").verification is None


def test_a_replan_is_validated_like_any_other_plan() -> None:
    graph = _diamond()

    with pytest.raises(CyclicPlanError):
        graph.replan_from(
            "api",
            [
                node("api", depends=["check"]),
                node("check", depends=["api"], role=SubagentRole.VERIFIER),
            ],
        )


# ----------------------------------------------------------------- model output is data


def _plan_document(tasks: list[dict[str, JSONValue]]) -> str:
    return json.dumps({"tasks": tasks})


def test_a_model_plan_goes_through_exactly_the_same_validation() -> None:
    """There is no unchecked path to a graph object.

    A parser that built a graph its own way would be a second definition of "valid", and
    the model's plan is precisely the one that would find the difference.
    """
    document = _plan_document(
        [
            {"id": "a", "goal": "one", "expected_output": "x", "acceptance_criteria": ["c"]},
            {
                "id": "b",
                "goal": "two",
                "expected_output": "y",
                "acceptance_criteria": ["c"],
                "dependencies": ["a"],
            },
        ]
    )

    graph = parse_plan(document)

    assert [task.id for task in graph.topological_order()] == ["a", "b"]


def test_a_model_cycle_is_rejected_at_the_same_gate() -> None:
    document = _plan_document(
        [
            {
                "id": "a",
                "goal": "one",
                "expected_output": "x",
                "acceptance_criteria": ["c"],
                "dependencies": ["b"],
            },
            {
                "id": "b",
                "goal": "two",
                "expected_output": "y",
                "acceptance_criteria": ["c"],
                "dependencies": ["a"],
            },
        ]
    )

    with pytest.raises(CyclicPlanError):
        parse_plan(document)


def test_a_fenced_answer_is_still_read() -> None:
    # Models fence JSON whatever they are told. Refusing that would be refusing on style.
    document = (
        "```json\n"
        + _plan_document(
            [{"id": "a", "goal": "one", "expected_output": "x", "acceptance_criteria": ["c"]}]
        )
        + "\n```"
    )

    assert len(parse_plan(document)) == 1


@pytest.mark.parametrize(
    "document",
    ["not json at all", "[]", '{"tasks": "several"}', '{"tasks": [{"id": "a"}]}', "{}"],
    ids=["prose", "an-array", "tasks-not-a-list", "a-task-missing-everything", "no-tasks"],
)
def test_an_unusable_answer_is_refused_rather_than_repaired(document: str) -> None:
    with pytest.raises(PlanParseError):
        parse_plan(document)


def test_an_invented_role_is_refused_rather_than_defaulted() -> None:
    """Quietly turning an unrecognised specialism into "coder" would hand a write-capable
    toolset to a task the plan meant to be read-only."""
    document = _plan_document(
        [
            {
                "id": "a",
                "goal": "one",
                "expected_output": "x",
                "acceptance_criteria": ["c"],
                "suggested_role": "architect",
            }
        ]
    )

    with pytest.raises(PlanParseError):
        parse_plan(document)


def test_a_task_gets_its_role_s_toolset_when_it_names_none() -> None:
    document = _plan_document(
        [
            {
                "id": "a",
                "goal": "look",
                "expected_output": "x",
                "acceptance_criteria": ["c"],
                "suggested_role": "explorer",
            }
        ]
    )

    task = parse_plan(document).get("a")

    assert task.suggested_role is SubagentRole.EXPLORER
    assert "read_file" in task.toolsets
    assert not any(name.startswith("write") for name in task.toolsets)


# ------------------------------------------------------------------------- the planner


def test_the_planner_does_not_call_the_model_for_a_simple_goal() -> None:
    """The decision comes first, and costs nothing.

    Asking a model whether something needs a plan is already most of the cost of planning
    it, and the answer is usually no.
    """

    async def scenario() -> None:
        provider = _ScriptedProvider("{}")
        planner = Planner(provider)

        graph = await planner.plan(
            "fix the failing test in calc.py",
            DecompositionSignals(independently_verifiable_outputs=1),
            CancellationSource().token,
        )

        assert graph is None
        assert provider.requests == [], "no model call for a goal that needs no plan"

    asyncio.run(scenario())


def test_the_planner_validates_what_the_model_returns() -> None:
    async def scenario() -> None:
        provider = _ScriptedProvider(
            _plan_document(
                [
                    {
                        "id": "a",
                        "goal": "one",
                        "expected_output": "x",
                        "acceptance_criteria": ["c"],
                        "dependencies": ["b"],
                    },
                    {
                        "id": "b",
                        "goal": "two",
                        "expected_output": "y",
                        "acceptance_criteria": ["c"],
                        "dependencies": ["a"],
                    },
                ]
            )
        )
        planner = Planner(provider)

        with pytest.raises(CyclicPlanError):
            await planner.plan(
                "rework the storage layer",
                DecompositionSignals(
                    independently_verifiable_outputs=3,
                    has_meaningful_dependencies=True,
                    subsystems_touched=2,
                ),
                CancellationSource().token,
            )

    asyncio.run(scenario())


def test_the_planner_tells_the_model_the_limits_it_will_be_held_to() -> None:
    # Enforcement alone produces a rejected plan and a wasted call. Telling it first
    # usually produces an acceptable one. Neither replaces the other.
    async def scenario() -> None:
        provider = _ScriptedProvider(
            _plan_document(
                [{"id": "a", "goal": "one", "expected_output": "x", "acceptance_criteria": ["c"]}]
            )
        )
        planner = Planner(provider, limits=PlanningLimits(max_tasks=5, max_depth=2))

        await planner.plan(
            "rework the storage layer",
            DecompositionSignals(
                independently_verifiable_outputs=3,
                has_meaningful_dependencies=True,
                subsystems_touched=2,
            ),
            CancellationSource().token,
        )

        brief = provider.requests[0].messages[-1].content
        assert "at most 5 tasks" in brief
        assert "no deeper than 2" in brief

    asyncio.run(scenario())


def test_the_planner_stops_when_cancelled() -> None:
    async def scenario() -> None:
        provider = _ScriptedProvider("{}")
        planner = Planner(provider)
        source = CancellationSource()
        source.cancel()

        with pytest.raises(AthenaRuntimeError):
            await planner.plan(
                "rework the storage layer",
                DecompositionSignals(
                    independently_verifiable_outputs=3,
                    has_meaningful_dependencies=True,
                    subsystems_touched=2,
                ),
                source.token,
            )

        assert provider.requests == []

    asyncio.run(scenario())


def test_planning_names_no_concrete_provider() -> None:
    """The rule the whole runtime is built on, checked here rather than assumed."""
    import ast
    from pathlib import Path

    import athena

    module = Path(athena.__file__).parent / "planning.py"
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    imported = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("athena")
    }

    assert "athena.adapters.openai_compatible" not in imported
    assert "athena.models" in imported, "it depends on the port, and only the port"
