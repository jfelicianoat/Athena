"""A `ModelProvider` that is really several, tried in order.

`RecoveryPolicy` has been able to emit `provider_fallback` since H3 and nothing consumed
it. A recovery directive with no implementation is worse than an absent feature: it reads
like a capability, it appears in the taxonomy, and the run it was supposed to save fails
exactly as if the directive did not exist. This is the consumer.

The router is itself a `ModelProvider`, which is the entire design. `AgentLoop` keeps
talking to the port and never learns there is a router behind it — so nothing above this
file changes, and a deployment with one provider does not pay for the concept.

**This is not a second AI_Broker.** It routes between *providers*, not between models. If
AI_Broker is the primary, it is already choosing models behind its own endpoint, and
duplicating that here would give two components an opinion about the same decision. What
this answers is narrower and unglamorous: the primary endpoint is down, and there is
another one.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from athena.cancellation import CancellationToken
from athena.capabilities import match, requirements_for
from athena.errors import (
    AthenaRuntimeError,
    ModelPermanentError,
    ModelStreamingUnsupportedError,
    ModelTransientError,
)
from athena.events import ModelEvent
from athena.models import (
    ModelCapabilities,
    ModelHealth,
    ModelHealthStatus,
    ModelProvider,
    ModelRequest,
    ModelResponse,
)

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProviderEntry:
    """One provider and the name a person would use for it in a report."""

    name: str
    provider: ModelProvider

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("A provider entry needs a name")


@dataclass
class RouterStats:
    """What the router did, for metrics and for a person reading a failed run."""

    attempts: int = 0
    failures_by_provider: dict[str, int] = field(default_factory=dict)
    served_by: dict[str, int] = field(default_factory=dict)

    def record_failure(self, name: str) -> None:
        self.failures_by_provider[name] = self.failures_by_provider.get(name, 0) + 1

    def record_success(self, name: str) -> None:
        self.served_by[name] = self.served_by.get(name, 0) + 1


class ProviderRegistry:
    """The ordered list of providers a router may use.

    Order is the policy. There is no scoring, no health-based reordering and no learning:
    a router that silently changed which provider it preferred would make two identical
    runs incomparable, which is precisely what the metrics work needs not to happen.
    """

    def __init__(self, primary: ProviderEntry, fallbacks: Sequence[ProviderEntry] = ()) -> None:
        self.primary = primary
        self.fallbacks = tuple(fallbacks)
        names = [entry.name for entry in self.all()]
        if len(set(names)) != len(names):
            raise ValueError("Provider names must be unique")

    def all(self) -> tuple[ProviderEntry, ...]:
        return (self.primary, *self.fallbacks)

    def __len__(self) -> int:
        return 1 + len(self.fallbacks)


class ProviderRouter(ModelProvider):
    """Tries providers in order, and is honest about which failures are worth moving on.

    A transient failure is the primary's own problem and its own retry — moving to a
    fallback on the first timeout would abandon a working endpoint over a blip, and would
    quietly change which model answered. A *permanent* failure is different: the request
    will never succeed there, so the only useful thing left is somewhere else.

    Cancellation is never a reason to fall back. Being stopped is not a provider failing,
    and trying the next one would mean ignoring the person who asked to stop.
    """

    def __init__(self, registry: ProviderRegistry) -> None:
        self.registry = registry
        self.stats = RouterStats()

    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        last: ModelPermanentError | None = None
        needed = requirements_for(
            offers_tools=bool(request.tools),
            needs_schema=request.response_schema is not None,
        )
        for entry in self.registry.all():
            cancellation.raise_if_cancelled()
            # Caer a un proveedor que no da lo que hace falta no es un respaldo, es una
            # degradación con otro nombre: el run continuaría sin la garantía que alguien
            # pidió y fallaría más adelante, atribuido al sitio equivocado.
            gaps = match(entry.name, entry.provider.capabilities(), needed)
            if not gaps.usable:
                self.stats.record_failure(entry.name)
                _logger.warning(
                    "router.provider_rejected provider=%s missing=%s",
                    entry.name,
                    ",".join(gaps.missing_required),
                )
                last = ModelPermanentError(
                    f"{entry.name} does not offer what this request requires",
                    details=gaps.to_json(),
                )
                continue
            self.stats.attempts += 1
            try:
                response = await entry.provider.complete(request, cancellation)
            except ModelPermanentError as error:
                # Permanent means "not here, not ever". The next provider is the only
                # thing left that could help.
                self.stats.record_failure(entry.name)
                _logger.warning("router.provider_permanent_failure provider=%s", entry.name)
                last = error
                continue
            except ModelTransientError:
                # Left to the primary's own retry policy. Falling back on a blip would
                # abandon a working endpoint and change which model answered.
                self.stats.record_failure(entry.name)
                raise
            self.stats.record_success(entry.name)
            if entry is not self.registry.all()[0]:
                # Qué se dejó de usar, qué se usó y por qué: sin eso, un run atendido por
                # el respaldo es indistinguible de uno normal, y la comparación de modelos
                # del informe mezclaría dos cosas distintas.
                _logger.info(
                    "router.fallback_used from=%s to=%s cause=%s",
                    self.registry.all()[0].name,
                    entry.name,
                    "" if last is None else last.code,
                )
            return response
        raise last or ModelPermanentError("No provider is configured")

    async def stream(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> AsyncIterator[ModelEvent]:
        """Streams from the first provider that can stream at all.

        Failing over mid-stream is not attempted, and not because it is hard: a consumer
        that already received half an answer cannot be handed the beginning of a different
        one. The fallback applies to starting a stream, never to rescuing one.
        """
        for entry in self.registry.all():
            cancellation.raise_if_cancelled()
            if not entry.provider.capabilities().streaming:
                continue
            async for event in entry.provider.stream(request, cancellation):
                yield event
            return
        raise ModelStreamingUnsupportedError("No configured provider supports streaming")

    def capabilities(self) -> ModelCapabilities:
        """The intersection, so a caller is never promised something a fallback lacks.

        Advertising the primary's capabilities would mean a run that fell back could
        suddenly lose streaming or tool calls halfway through, which the caller has no way
        to prepare for.
        """
        entries = self.registry.all()
        return ModelCapabilities(
            streaming=all(entry.provider.capabilities().streaming for entry in entries),
            tool_calls=all(entry.provider.capabilities().tool_calls for entry in entries),
            structured_output=all(
                entry.provider.capabilities().structured_output for entry in entries
            ),
        )

    async def health(self, cancellation: CancellationToken) -> ModelHealth:
        """Healthy if anything can serve; degraded if the primary cannot.

        "Degraded" is the useful middle answer: the run will work, and somebody should know
        it is not running on what they configured.
        """
        cancellation.raise_if_cancelled()
        reachable: list[str] = []
        for entry in self.registry.all():
            try:
                health = await entry.provider.health(cancellation)
            except AthenaRuntimeError:
                continue
            if health.status is not ModelHealthStatus.UNAVAILABLE:
                reachable.append(entry.name)
        if not reachable:
            return ModelHealth(ModelHealthStatus.UNAVAILABLE, "No provider is reachable")
        if reachable[0] != self.registry.primary.name:
            return ModelHealth(
                ModelHealthStatus.DEGRADED,
                f"The primary provider is unavailable; serving from {reachable[0]}",
            )
        return ModelHealth(ModelHealthStatus.HEALTHY)


__all__ = ["ProviderEntry", "ProviderRegistry", "ProviderRouter", "RouterStats"]
