"""The consumer that `provider_fallback` never had.

The directive has existed since H3 and nothing acted on it, which meant a run that could
have been saved failed exactly as though the option were not there. These tests are mostly
about *when not* to fall back — a router that moves on too eagerly abandons a working
endpoint and quietly changes which model answered.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from athena.cancellation import CancellationSource, CancellationToken
from athena.errors import (
    CancellationError,
    ModelPermanentError,
    ModelStreamingUnsupportedError,
    ModelTransientError,
)
from athena.events import EventName, ModelEvent
from athena.models import (
    ModelCapabilities,
    ModelHealth,
    ModelHealthStatus,
    ModelProvider,
    ModelRequest,
    ModelResponse,
)
from athena.provider_router import ProviderEntry, ProviderRegistry, ProviderRouter
from athena.recovery import RecoveryAction, RecoveryPolicy


class _Provider(ModelProvider):
    """Answers, or fails in a stated way. Counts what it was asked."""

    def __init__(
        self,
        answer: str = "ok",
        *,
        fails_with: Exception | None = None,
        streaming: bool = False,
        tool_calls: bool = True,
        health: ModelHealthStatus = ModelHealthStatus.HEALTHY,
    ) -> None:
        self.answer = answer
        self.fails_with = fails_with
        self.streaming = streaming
        self.tool_calls = tool_calls
        self._health = health
        self.calls = 0

    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        del request
        cancellation.raise_if_cancelled()
        self.calls += 1
        if self.fails_with is not None:
            raise self.fails_with
        return ModelResponse(self.answer, self.answer, "stop")

    async def stream(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> AsyncIterator[ModelEvent]:
        del request, cancellation
        self.calls += 1
        if self.fails_with is not None:
            raise self.fails_with
        yield ModelEvent(EventName.MODEL_COMPLETED, self.answer)

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(self.streaming, self.tool_calls, False)

    async def health(self, cancellation: CancellationToken) -> ModelHealth:
        cancellation.raise_if_cancelled()
        if self._health is ModelHealthStatus.UNAVAILABLE:
            raise ModelTransientError("unreachable")
        return ModelHealth(self._health)


def _router(primary: ModelProvider, *fallbacks: ModelProvider) -> ProviderRouter:
    return ProviderRouter(
        ProviderRegistry(
            ProviderEntry("primary", primary),
            [ProviderEntry(f"fallback{index}", item) for index, item in enumerate(fallbacks)],
        )
    )


REQUEST = ModelRequest(messages=())


# --------------------------------------------------------------------- the directive lives


def test_the_recovery_directive_now_has_something_to_mean() -> None:
    """A directive nothing consumes is worse than an absent feature.

    It reads like a capability and behaves like nothing at all.
    """
    with_fallback = RecoveryPolicy(provider_fallback=True)
    without = RecoveryPolicy()
    error = ModelPermanentError("the endpoint is gone")

    assert with_fallback.decide(error).action is RecoveryAction.LIMITED_RETRY
    assert without.decide(error).action is RecoveryAction.ABORT


def test_a_permanent_failure_moves_to_the_next_provider() -> None:
    async def scenario() -> None:
        primary = _Provider(fails_with=ModelPermanentError("gone"))
        secondary = _Provider("second")
        router = _router(primary, secondary)

        response = await router.complete(REQUEST, CancellationSource().token)

        assert response.content == "second"
        assert primary.calls == 1
        assert secondary.calls == 1
        assert router.stats.failures_by_provider == {"primary": 1}
        assert router.stats.served_by == {"fallback0": 1}

    asyncio.run(scenario())


def test_a_transient_failure_is_the_primary_s_own_business() -> None:
    """Falling back on a blip abandons a working endpoint and changes which model answered.

    The primary already has a retry policy for this; the router staying out of the way is
    what lets that policy work.
    """

    async def scenario() -> None:
        primary = _Provider(fails_with=ModelTransientError("busy"))
        secondary = _Provider("second")
        router = _router(primary, secondary)

        with pytest.raises(ModelTransientError):
            await router.complete(REQUEST, CancellationSource().token)

        assert secondary.calls == 0, "a blip is not a reason to change providers"

    asyncio.run(scenario())


def test_being_stopped_is_never_a_reason_to_try_another_provider() -> None:
    # Trying the next one would mean ignoring the person who asked to stop.
    async def scenario() -> None:
        primary = _Provider()
        secondary = _Provider("second")
        router = _router(primary, secondary)
        source = CancellationSource()
        source.cancel()

        with pytest.raises(CancellationError):
            await router.complete(REQUEST, source.token)

        assert primary.calls == 0
        assert secondary.calls == 0

    asyncio.run(scenario())


def test_the_first_working_provider_wins_and_the_rest_are_untouched() -> None:
    async def scenario() -> None:
        primary = _Provider("first")
        secondary = _Provider("second")
        router = _router(primary, secondary)

        response = await router.complete(REQUEST, CancellationSource().token)

        assert response.content == "first"
        assert secondary.calls == 0

    asyncio.run(scenario())


def test_it_walks_the_whole_list_before_giving_up() -> None:
    async def scenario() -> None:
        first = _Provider(fails_with=ModelPermanentError("gone"))
        second = _Provider(fails_with=ModelPermanentError("also gone"))
        third = _Provider("third")
        router = _router(first, second, third)

        response = await router.complete(REQUEST, CancellationSource().token)

        assert response.content == "third"
        assert router.stats.attempts == 3

    asyncio.run(scenario())


def test_when_everything_is_gone_it_reports_the_last_real_failure() -> None:
    # Not a fresh error of its own. The caller wants to know what the providers said.
    async def scenario() -> None:
        first = _Provider(fails_with=ModelPermanentError("first is gone"))
        second = _Provider(fails_with=ModelPermanentError("second is gone"))
        router = _router(first, second)

        with pytest.raises(ModelPermanentError) as caught:
            await router.complete(REQUEST, CancellationSource().token)

        assert "second is gone" in str(caught.value)

    asyncio.run(scenario())


# ------------------------------------------------------------------------- it is a provider


def test_the_router_is_itself_a_model_provider() -> None:
    """The whole design. `AgentLoop` talks to the port and never learns there is a router."""
    router = _router(_Provider())

    assert isinstance(router, ModelProvider)


def test_capabilities_are_the_intersection_not_the_primary_s() -> None:
    """A run that fell back must not lose a capability halfway through.

    Advertising the primary's would promise something a fallback cannot deliver, and the
    caller has no way to prepare for that.
    """
    router = _router(
        _Provider(streaming=True, tool_calls=True), _Provider(streaming=False, tool_calls=True)
    )

    capabilities = router.capabilities()

    assert capabilities.streaming is False
    assert capabilities.tool_calls is True


def test_streaming_uses_the_first_provider_that_can() -> None:
    async def scenario() -> None:
        router = _router(_Provider(streaming=False), _Provider("streamer", streaming=True))

        events = [event async for event in router.stream(REQUEST, CancellationSource().token)]

        assert [event.session_id for event in events] == ["streamer"]

    asyncio.run(scenario())


def test_nothing_that_streams_is_said_plainly() -> None:
    async def scenario() -> None:
        router = _router(_Provider(streaming=False))

        with pytest.raises(ModelStreamingUnsupportedError):
            [event async for event in router.stream(REQUEST, CancellationSource().token)]

    asyncio.run(scenario())


# ------------------------------------------------------------------------------- health


def test_health_is_degraded_when_the_primary_is_down_but_work_can_continue() -> None:
    """The useful middle answer.

    The run will work, and somebody should know it is not running on what they configured.
    """

    async def scenario() -> None:
        router = _router(_Provider(health=ModelHealthStatus.UNAVAILABLE), _Provider("second"))

        health = await router.health(CancellationSource().token)

        assert health.status is ModelHealthStatus.DEGRADED
        assert health.detail is not None
        assert "fallback0" in health.detail

    asyncio.run(scenario())


def test_health_is_healthy_when_the_primary_answers() -> None:
    async def scenario() -> None:
        router = _router(_Provider(), _Provider("second"))

        health = await router.health(CancellationSource().token)

        assert health.status is ModelHealthStatus.HEALTHY

    asyncio.run(scenario())


def test_health_is_unavailable_only_when_nothing_can_serve() -> None:
    async def scenario() -> None:
        router = _router(
            _Provider(health=ModelHealthStatus.UNAVAILABLE),
            _Provider(health=ModelHealthStatus.UNAVAILABLE),
        )

        health = await router.health(CancellationSource().token)

        assert health.status is ModelHealthStatus.UNAVAILABLE

    asyncio.run(scenario())


# ------------------------------------------------------------------------- the registry


def test_provider_names_must_be_distinct() -> None:
    # The names end up in metrics and in failure reports; two providers called the same
    # thing would make both unreadable.
    with pytest.raises(ValueError):
        ProviderRegistry(ProviderEntry("same", _Provider()), [ProviderEntry("same", _Provider())])


def test_a_nameless_provider_is_refused() -> None:
    with pytest.raises(ValueError):
        ProviderEntry("  ", _Provider())


def test_a_single_provider_deployment_pays_nothing_for_the_concept() -> None:
    async def scenario() -> None:
        only = _Provider("only")
        router = ProviderRouter(ProviderRegistry(ProviderEntry("only", only)))

        response = await router.complete(REQUEST, CancellationSource().token)

        assert response.content == "only"
        assert len(router.registry) == 1

    asyncio.run(scenario())


def test_the_router_does_not_route_between_models() -> None:
    """It is not a second AI_Broker.

    If AI_Broker is the primary it is already choosing models behind its endpoint, and
    duplicating that here would give two components an opinion about one decision.
    """
    import ast
    from pathlib import Path

    import athena

    module = Path(athena.__file__).parent / "provider_router.py"
    source = module.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(module))
    imported = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("athena")
    }

    assert not any(name.startswith("athena.adapters") for name in imported)
    assert "model" not in ProviderEntry.__dataclass_fields__, "it selects providers, not models"
