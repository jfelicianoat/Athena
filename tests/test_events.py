from __future__ import annotations

import asyncio

from athena.events import AgentEvent, EventName, InMemoryEventBus, RuntimeEvent


def test_event_bus_filters_orders_and_unsubscribes_handlers() -> None:
    async def scenario() -> None:
        bus = InMemoryEventBus()
        received: list[str] = []

        bus.subscribe(lambda event: received.append(f"all:{event.name}"))
        unsubscribe = bus.subscribe(
            lambda event: received.append(f"agent:{event.name}"),
            [EventName.AGENT_STARTED],
        )
        event = AgentEvent(name=EventName.AGENT_STARTED, session_id="s-1")

        await bus.publish(event)
        unsubscribe()
        await bus.publish(event)

        assert received == [
            "all:agent.started",
            "agent:agent.started",
            "all:agent.started",
        ]

    asyncio.run(scenario())


def test_required_event_vocabulary_is_frozen() -> None:
    """The H0 names may never be removed. Adding to them is how the runtime grows.

    Asserting equality would mean no milestone could ever name a new fact — which every
    milestone since H0 has done, `subagent.*` and `recovery.*` among them. What has to hold
    is that nothing a consumer already depends on quietly disappears.
    """
    required = {
        "agent.started",
        "agent.completed",
        "agent.failed",
        "agent.cancelled",
        "model.started",
        "model.completed",
        "model.failed",
        "tool.started",
        "tool.progress",
        "tool.completed",
        "tool.failed",
        "permission.requested",
        "permission.resolved",
        "verification.started",
        "verification.completed",
        "file.changed",
        "process.started",
        "process.completed",
        "process.failed",
        "process.cancelled",
        "verification.check.started",
        "verification.check.completed",
        "verification.failed",
        "recovery.started",
        "recovery.action",
        "recovery.exhausted",
        "context.compacted",
        "session.persisted",
        "session.resumed",
        "subagent.started",
        "subagent.completed",
        "subagent.failed",
        "subagent.cancelled",
    }

    assert required <= {event.value for event in EventName}
    assert issubclass(AgentEvent, RuntimeEvent)
