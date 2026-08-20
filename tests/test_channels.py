"""The channel boundary: what crosses it, what does not, and what it refuses.

The point of these is not that the translation functions return the right strings. It is
that the boundary holds — that the runtime cannot learn what channel it is talking to, that
free text cannot start an agent, that an unlisted account gets nothing, and that a channel
which falls over does not take the runtime with it.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

import athena
from athena.adapters.channel_gateway import (
    ChannelAccessPolicy,
    ChannelGateway,
    ChannelGrant,
    serve_channel,
)
from athena.adapters.service.runs import CapabilityMode, RunOptions, RunRegistry
from athena.cancellation import CancellationToken
from athena.channels import (
    REPORTED_EVENTS,
    ChannelAdapter,
    ChannelCommand,
    ChannelEventSink,
    ChannelIdentity,
    ChannelMessage,
    CommandName,
    ResponseKind,
    parse_command,
    render_event,
)
from athena.events import EventName, InMemoryEventBus, ModelEvent, RuntimeEvent
from athena.models import (
    ModelCapabilities,
    ModelHealth,
    ModelHealthStatus,
    ModelProvider,
    ModelRequest,
    ModelResponse,
)
from athena.planning import PlanBoard, PlanStatus, TaskGraph, TaskNode, describe_plan
from athena.session_store import SqliteSessionStore
from athena.stores import SqliteToolResultStore
from athena.subagents import SubagentRole
from athena.testing import FakeChannelAdapter
from athena.types import JSONValue

#: Packages a runtime must never reach for. If a concrete channel is ever written, it lives
#: outside `athena/` and is handed in; the day one of these appears in the runtime, the
#: boundary has already been lost.
CHANNEL_SDKS = frozenset(
    {
        "aiogram",
        "discord",
        "irc",
        "matrix",
        "nio",
        "pyrogram",
        "slack",
        "slack_bolt",
        "slack_sdk",
        "telegram",
        "telethon",
        "twilio",
    }
)

ALICE = ChannelIdentity("fake", "u-1", "chat-1", "Alice")
MALLORY = ChannelIdentity("fake", "u-9", "chat-9")


class _IdleProvider(ModelProvider):
    """Says there is nothing to do, so a run starts and ends without touching anything."""

    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        del request
        cancellation.raise_if_cancelled()
        return ModelResponse("Nothing to do.", "scripted", "stop")

    async def stream(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> AsyncIterator[ModelEvent]:
        del request, cancellation
        if False:
            yield ModelEvent(EventName.MODEL_COMPLETED, "never")

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(False, True, False)

    async def health(self, cancellation: CancellationToken) -> ModelHealth:
        cancellation.raise_if_cancelled()
        return ModelHealth(ModelHealthStatus.HEALTHY)


def _message(text: str, identity: ChannelIdentity = ALICE) -> ChannelMessage:
    return ChannelMessage(identity, text)


def _registry(tmp_path: Path, bus: InMemoryEventBus) -> RunRegistry:
    return RunRegistry(
        _IdleProvider(),
        bus,
        SqliteSessionStore(tmp_path / "sessions.db"),
        SqliteToolResultStore(tmp_path / "results.db"),
    )


def _policy(root: Path, identities: Sequence[ChannelIdentity] = (ALICE,)) -> ChannelAccessPolicy:
    policy = ChannelAccessPolicy()
    for identity in identities:
        policy.grant(
            identity.key,
            ChannelGrant(
                root,
                RunOptions(writes=CapabilityMode.OFF, execution=CapabilityMode.OFF),
            ),
        )
    return policy


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "notes.md").write_text("nothing to see\n", encoding="utf-8")
    return root


# --- the boundary itself --------------------------------------------------------------


def test_the_runtime_never_imports_a_channel_sdk() -> None:
    """Structural, not aspirational.

    Reading the import statements is the only version of this claim that stays true after
    someone adds a Telegram adapter in a hurry.
    """
    source_root = Path(athena.__file__).parent
    offenders: list[str] = []
    for module in sorted(source_root.rglob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            offenders.extend(
                f"{module.name}: {name}"
                for name in names
                if name.split(".")[0].lower() in CHANNEL_SDKS
            )

    assert offenders == []


def test_the_contracts_depend_on_nothing_but_the_runtime() -> None:
    """`channels` is the boundary, so it must not reach sideways into an adapter.

    If it imported the service adapter, every channel would arrive carrying HTTP, and the
    next transport would find the shape already bent around the first one.
    """
    module = Path(athena.__file__).parent / "channels.py"
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    imported = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("athena")
    }

    assert imported == {"athena.events", "athena.types"}


def test_the_fake_satisfies_both_protocols_without_inheriting_them() -> None:
    # A real adapter wraps a third-party client and cannot subclass anything of Athena's.
    # A fake that only worked by subclassing would be testing a shape nothing real has.
    adapter = FakeChannelAdapter()

    assert isinstance(adapter, ChannelAdapter)
    assert isinstance(adapter, ChannelEventSink)
    assert ChannelAdapter not in FakeChannelAdapter.__mro__


# --- inbound translation ---------------------------------------------------------------


def test_plain_text_is_not_an_objective() -> None:
    # The cost of guessing wrong is an agent running against someone's repository because
    # they said hello.
    command = parse_command(_message("please fix the failing test"))

    assert command.name is CommandName.UNKNOWN
    assert command.objective is None
    assert command.reason is not None
    assert "explicit" in command.reason


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/run fix calc.add", CommandName.START_RUN),
        ("/status", CommandName.RUN_STATUS),
        ("/cancel", CommandName.CANCEL_RUN),
        ("/help", CommandName.HELP),
        ("/start", CommandName.HELP),
        ("/nonsense", CommandName.UNKNOWN),
        ("", CommandName.UNKNOWN),
        ("   ", CommandName.UNKNOWN),
    ],
)
def test_commands_map_to_the_closed_set(text: str, expected: CommandName) -> None:
    assert parse_command(_message(text)).name is expected


def test_a_command_carries_the_identity_that_sent_it() -> None:
    command = parse_command(_message("/run fix calc.add", MALLORY))

    assert command.identity == MALLORY
    assert command.objective == "fix calc.add"


def test_an_objective_that_is_too_long_is_refused_at_the_boundary() -> None:
    # Not the model's problem to discover: the channel is where the size is known.
    command = parse_command(_message("/run " + "x" * 2_001))

    assert command.name is CommandName.UNKNOWN
    assert command.reason is not None
    assert "2000" in command.reason


def test_run_without_an_objective_asks_for_one_rather_than_starting_an_empty_run() -> None:
    assert parse_command(_message("/run   ")).name is CommandName.UNKNOWN


def test_a_command_carries_no_channel_object() -> None:
    """What crosses into the runtime is data, not a client.

    This is what makes "the runtime cannot depend on a channel" true by construction rather
    than by review.
    """
    command = parse_command(_message("/run fix calc.add"))

    assert isinstance(command.identity, ChannelIdentity)
    assert command.objective is None or isinstance(command.objective, str)
    assert command.run_id is None or isinstance(command.run_id, str)


# --- outbound translation --------------------------------------------------------------


def _event(name: EventName, payload: dict[str, JSONValue] | None = None) -> RuntimeEvent:
    return RuntimeEvent(name, "run-1", payload or {})


def test_a_channel_is_not_a_log() -> None:
    # Relaying every event is the failure mode that makes a channel unreadable, so the
    # quiet ones are quiet by policy rather than by luck.
    noisy = [
        EventName.TOOL_STARTED,
        EventName.TOOL_PROGRESS,
        EventName.TOOL_COMPLETED,
        EventName.MODEL_STARTED,
        EventName.MODEL_COMPLETED,
        EventName.FILE_CHANGED,
        EventName.SESSION_PERSISTED,
        EventName.CONTEXT_COMPACTED,
    ]

    assert all(render_event(_event(name), ALICE) is None for name in noisy)


def test_the_end_of_a_run_is_always_reported() -> None:
    for name in (
        EventName.AGENT_COMPLETED,
        EventName.AGENT_FAILED,
        EventName.AGENT_CANCELLED,
    ):
        response = render_event(_event(name, {"error_code": "boom", "message": "it broke"}), ALICE)

        assert response is not None
        assert response.run_id == "run-1"
        assert response.identity == ALICE


def test_an_inconclusive_verification_is_not_dressed_up_as_success() -> None:
    response = render_event(
        _event(EventName.VERIFICATION_COMPLETED, {"status": "inconclusive"}), ALICE
    )

    assert response is not None
    assert response.kind is ResponseKind.ERROR
    assert "nothing was proven" in response.text


def test_a_permission_request_is_reported_as_the_refusal_it_becomes() -> None:
    # Nobody on a channel can answer, so Athena's unattended default applies. Silence here
    # would make a run that refused itself look like a run that did nothing.
    response = render_event(
        _event(EventName.PERMISSION_REQUESTED, {"action": "write calc.py"}), ALICE
    )

    assert response is not None
    assert "write calc.py" in response.text
    assert "refused" in response.text


def test_rendering_uses_the_keys_the_runtime_actually_publishes() -> None:
    # `agent.failed` carries `error_code` and `message`. Reading for a key nobody sends
    # turns the most important message of a run into "no detail given".
    response = render_event(
        _event(
            EventName.AGENT_FAILED,
            {"error_code": "budget_exceeded", "message": "a stated reason"},
        ),
        ALICE,
    )

    assert response is not None
    assert "a stated reason" in response.text


def test_completion_carries_the_evidence_that_earned_it() -> None:
    # A run that says it finished without saying what it proved is the claim Athena refuses
    # to take from the model; a channel must not launder it either.
    response = render_event(
        _event(EventName.AGENT_COMPLETED, {"verification": "All project checks pass."}), ALICE
    )

    assert response is not None
    assert "All project checks pass." in response.text


# --- the gateway -----------------------------------------------------------------------


def test_an_unlisted_account_gets_nothing(tmp_path: Path) -> None:
    """A chat account is discoverable by anyone who finds the bot."""

    async def scenario() -> None:
        bus = InMemoryEventBus()
        registry = _registry(tmp_path, bus)
        adapter = FakeChannelAdapter(inbound=[_message("/run delete everything", MALLORY)])
        gateway = ChannelGateway(adapter, registry, _policy(tmp_path), bus)
        try:
            await gateway.start()
            await gateway.run_forever()
        finally:
            await gateway.stop()
            await registry.shutdown()

        assert registry.live_ids() == ()
        assert adapter.of_kind(ResponseKind.ERROR)
        assert "not authorised" in adapter.texts()[0]

    asyncio.run(scenario())


def test_the_refusal_does_not_say_why(tmp_path: Path) -> None:
    # Two differently worded refusals would be a way to probe the grant table.
    async def scenario() -> None:
        bus = InMemoryEventBus()
        registry = _registry(tmp_path, bus)
        adapter = FakeChannelAdapter(
            inbound=[_message("/run a", MALLORY), _message("/status", MALLORY)]
        )
        gateway = ChannelGateway(adapter, registry, _policy(tmp_path), bus)
        try:
            await gateway.start()
            await gateway.run_forever()
        finally:
            await gateway.stop()
            await registry.shutdown()

        assert len(set(adapter.texts())) == 1

    asyncio.run(scenario())


def test_a_granted_identity_starts_a_run_and_hears_about_it(tmp_path: Path) -> None:
    async def scenario() -> None:
        bus = InMemoryEventBus()
        registry = _registry(tmp_path, bus)
        adapter = FakeChannelAdapter(inbound=[_message("/run take a look around")])
        gateway = ChannelGateway(adapter, registry, _policy(_workspace(tmp_path)), bus)
        try:
            await gateway.start()
            await gateway.run_forever()
            run_id = await gateway.run_for(ALICE)
            assert run_id is not None
            await registry.wait(run_id)
        finally:
            await gateway.stop()
            await registry.shutdown()

        # This run ends FAILED, and that is the runtime being right: the model said there
        # was nothing to do and verification proved nothing, so completion was never
        # earned. What the channel has to get right is that all three facts arrive.
        assert any("Started" in text for text in adapter.texts())
        assert any("nothing was proven" in text for text in adapter.texts())
        assert any(text.startswith("Athena stopped:") for text in adapter.texts())
        assert not any("no detail given" in text for text in adapter.texts())

    asyncio.run(scenario())


def test_events_reach_only_the_identity_that_asked(tmp_path: Path) -> None:
    """A run this gateway did not start has no owner, and is not anybody's business."""

    async def scenario() -> None:
        bus = InMemoryEventBus()
        registry = _registry(tmp_path, bus)
        adapter = FakeChannelAdapter()
        gateway = ChannelGateway(adapter, registry, _policy(tmp_path), bus)
        await gateway.start()
        try:
            await bus.publish(RuntimeEvent(EventName.AGENT_COMPLETED, "someone-elses-run"))
        finally:
            await gateway.stop()
            await registry.shutdown()

        assert adapter.delivered == []

    asyncio.run(scenario())


def test_one_run_at_a_time_per_identity(tmp_path: Path) -> None:
    # Otherwise a chat account is an unbounded way to spend the host's CPU, and no event
    # can be attributed to the run it came from.
    async def scenario() -> None:
        bus = InMemoryEventBus()
        registry = _registry(tmp_path, bus)
        adapter = FakeChannelAdapter(inbound=[_message("/run one"), _message("/run two")])
        gateway = ChannelGateway(adapter, registry, _policy(_workspace(tmp_path)), bus)
        try:
            await gateway.start()
            await gateway.run_forever()
            assert len(registry.live_ids()) == 1
            assert any("already going" in text for text in adapter.texts())
            run_id = await gateway.run_for(ALICE)
            assert run_id is not None
            await registry.wait(run_id)
        finally:
            await gateway.stop()
            await registry.shutdown()

    asyncio.run(scenario())


def test_nobody_can_cancel_a_run_they_do_not_own(tmp_path: Path) -> None:
    async def scenario() -> None:
        bus = InMemoryEventBus()
        registry = _registry(tmp_path, bus)
        adapter = FakeChannelAdapter(inbound=[_message("/run mine")])
        policy = _policy(_workspace(tmp_path), (ALICE, MALLORY))
        gateway = ChannelGateway(adapter, registry, policy, bus)
        try:
            await gateway.start()
            await gateway.run_forever()
            run_id = await gateway.run_for(ALICE)
            assert run_id is not None
            adapter.push(f"/cancel {run_id}", MALLORY)
            await gateway.run_forever()
            await registry.wait(run_id)
        finally:
            await gateway.stop()
            await registry.shutdown()

        assert "You have no run to cancel." in adapter.texts()

    asyncio.run(scenario())


def test_help_needs_no_grant(tmp_path: Path) -> None:
    # Someone who cannot use Athena should still be able to find out that they cannot.
    async def scenario() -> None:
        bus = InMemoryEventBus()
        registry = _registry(tmp_path, bus)
        adapter = FakeChannelAdapter(inbound=[_message("/help", MALLORY)])
        gateway = ChannelGateway(adapter, registry, _policy(tmp_path), bus)
        try:
            await gateway.start()
            await gateway.run_forever()
        finally:
            await gateway.stop()
            await registry.shutdown()

        assert "/run" in adapter.texts()[0]
        assert "Permission requests cannot be answered from here" in adapter.texts()[0]

    asyncio.run(scenario())


def test_a_channel_that_falls_over_does_not_take_the_runtime_with_it(tmp_path: Path) -> None:
    async def scenario() -> None:
        bus = InMemoryEventBus()
        registry = _registry(tmp_path, bus)
        adapter = FakeChannelAdapter(inbound=[_message("/help")], fail_delivery=True)
        gateway = ChannelGateway(adapter, registry, _policy(tmp_path), bus)
        try:
            await gateway.start()
            await gateway.run_forever()
        finally:
            await gateway.stop()
            await registry.shutdown()

        assert adapter.delivered == []
        assert adapter.stopped

    asyncio.run(scenario())


def test_serving_a_channel_closes_it_even_when_the_loop_is_cancelled(tmp_path: Path) -> None:
    class _NeverAnswers(FakeChannelAdapter):
        async def receive(self) -> ChannelMessage | None:
            await asyncio.sleep(3600)
            return None

    async def scenario() -> None:
        bus = InMemoryEventBus()
        registry = _registry(tmp_path, bus)
        adapter = _NeverAnswers()
        task = asyncio.ensure_future(serve_channel(adapter, registry, _policy(tmp_path), bus))
        while not adapter.started:
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await registry.shutdown()

        assert adapter.stopped

    asyncio.run(scenario())


def node(
    task_id: str,
    *,
    depends: Sequence[str] = (),
    role: SubagentRole = SubagentRole.CODER,
    output: str | None = None,
) -> TaskNode:
    return TaskNode(
        id=task_id,
        goal=f"do {task_id}",
        expected_output=output or f"{task_id} exists",
        acceptance_criteria=("it can be checked",),
        dependencies=tuple(depends),
        suggested_role=role,
    )


# ------------------------------------------------------------------- the plan, in a chat


def test_a_channel_hears_about_the_plan_and_not_about_every_task() -> None:
    """Aggregation, stated as a rule.

    A graph of twelve tasks produces twenty-four task events. A channel that relayed all
    of them would stop being read, which is the failure aggregation exists to prevent.
    """
    from athena.planning import PlanBoard, describe_plan

    del PlanBoard, describe_plan  # imported to prove they are reachable from the package

    assert EventName.GRAPH_STARTED in REPORTED_EVENTS
    assert EventName.GRAPH_COMPLETED in REPORTED_EVENTS
    assert EventName.TASK_FAILED in REPORTED_EVENTS, "a failure is worth interrupting for"
    assert EventName.TASK_STARTED not in REPORTED_EVENTS
    assert EventName.TASK_COMPLETED not in REPORTED_EVENTS


def test_the_plan_announces_its_size_so_someone_knows_what_they_are_waiting_for() -> None:
    response = render_event(_event(EventName.GRAPH_STARTED, {"tasks": 4}), ALICE)

    assert response is not None
    assert "4 tareas" in response.text
    assert response.kind is ResponseKind.PROGRESS


def test_a_failing_task_interrupts_while_there_is_still_time_to_look() -> None:
    response = render_event(
        _event(EventName.TASK_FAILED, {"task_id": "T03", "summary": "no compila"}), ALICE
    )

    assert response is not None
    assert "T03" in response.text
    assert "no compila" in response.text
    assert response.kind is ResponseKind.ERROR


def test_a_cancelled_plan_is_not_reported_as_a_failed_one() -> None:
    cancelled = render_event(_event(EventName.GRAPH_CANCELLED), ALICE)
    failed = render_event(_event(EventName.GRAPH_FAILED), ALICE)

    assert cancelled is not None and cancelled.kind is ResponseKind.RESULT
    assert failed is not None and failed.kind is ResponseKind.ERROR


def test_asking_for_the_plan_of_a_run_that_has_none_says_so(tmp_path: Path) -> None:
    """A run that goes straight is not a plan of one step.

    Drawing a single-line list would make a direct run look like a graph nobody bothered
    to decompose.
    """

    async def scenario() -> None:
        bus = InMemoryEventBus()
        registry = _registry(tmp_path, bus)
        adapter = FakeChannelAdapter()
        gateway = ChannelGateway(
            adapter, registry, _policy(_workspace(tmp_path)), bus, board=PlanBoard()
        )
        try:
            await gateway.start()
            await gateway.handle(
                ChannelCommand(CommandName.START_RUN, ALICE, objective="look around")
            )
            await gateway.handle(ChannelCommand(CommandName.RUN_PLAN, ALICE))
            run_id = await gateway.run_for(ALICE)
            assert run_id is not None
            await registry.wait(run_id)
        finally:
            await gateway.stop()
            await registry.shutdown()

        assert any("va directo" in text for text in adapter.texts())

    asyncio.run(scenario())


def test_a_plan_reads_as_a_plan_rather_than_as_a_queue() -> None:
    """Indented by dependency, because a flat list does not say what waited for what."""
    graph = TaskGraph.build(
        [
            node("survey", role=SubagentRole.EXPLORER),
            node("api", depends=["survey"], output="api"),
            node("check", depends=["api"], role=SubagentRole.VERIFIER, output="check"),
        ]
    )

    rendered = describe_plan(graph)
    lines = rendered.splitlines()

    assert "0 de 3" in lines[0]
    assert not lines[1].startswith(" "), "lo que no espera a nadie va al margen"
    assert lines[2].startswith("  ")
    assert lines[3].startswith("    ")
    assert "explorer" in rendered


def test_the_plan_shows_what_is_done_and_what_is_running() -> None:
    graph = TaskGraph.build([node("a", output="a"), node("b", output="b")])
    graph.transition("a", PlanStatus.READY)
    graph.transition("a", PlanStatus.RUNNING)
    graph.transition("a", PlanStatus.COMPLETED)

    rendered = describe_plan(graph)

    assert "1 de 2" in rendered
    assert "✓ a" in rendered
    assert "○ b" in rendered


def test_the_board_does_not_remember_every_plan_ever_run() -> None:
    # A board that grew without bound is a slow leak in a process meant to stay up.
    board = PlanBoard(capacity=2)
    for index in range(4):
        board.record(f"run-{index}", TaskGraph.build([node(f"t{index}")]))

    assert board.plan_for("run-0") is None
    assert board.plan_for("run-3") is not None


def test_the_executor_leaves_its_plan_where_a_channel_can_read_it() -> None:
    """Neither side knows about the other; both know about the board."""
    import ast

    import athena

    module = Path(athena.__file__).parent / "graph_executor.py"
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    imported = {
        item.module or ""
        for item in ast.walk(tree)
        if isinstance(item, ast.ImportFrom) and (item.module or "").startswith("athena")
    }

    assert "athena.channels" not in imported
    assert "athena.adapters.channel_gateway" not in imported
