"""Un proveedor que lleva el modelo elegido a cada llamada, venga de donde venga.

El modelo de un run se elegia en un sitio y se aplicaba en otro: el `AgentLoop` lo
ponia en su peticion, pero el planificador, los delegados y los hijos de un grafo se
construian con el proveedor del proceso y pedian `model=None`. Un run que eligio
`chosen-model` hacia tres llamadas y ninguna llevaba ese modelo (auditoria A06).

Envolver el proveedor es la forma de que no dependa de que cada constructor se acuerde:
todo lo que infiere pasa por aqui, asi que todo lleva el modelo.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace

from athena.cancellation import CancellationToken
from athena.events import ModelEvent
from athena.models import (
    ModelCapabilities,
    ModelHealth,
    ModelProvider,
    ModelRequest,
    ModelResponse,
)


class PinnedModelProvider(ModelProvider):
    """Pone `model` en toda peticion que no lo traiga ya."""

    def __init__(self, inner: ModelProvider, model: str) -> None:
        self.inner = inner
        self.model = model

    def _pin(self, request: ModelRequest) -> ModelRequest:
        if request.model:
            return request
        return replace(request, model=self.model)

    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        return await self.inner.complete(self._pin(request), cancellation)

    def stream(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> AsyncIterator[ModelEvent]:
        return self.inner.stream(self._pin(request), cancellation)

    def capabilities(self) -> ModelCapabilities:
        return self.inner.capabilities()

    async def health(self, cancellation: CancellationToken) -> ModelHealth:
        return await self.inner.health(cancellation)

    def __getattr__(self, name: str) -> object:
        if name == "inner":
            raise AttributeError(name)
        # Lo que el proveedor de dentro exponga ademas (su nombre, su router) sigue
        # visible: fijar el modelo no debe esconder quien contesta.
        return getattr(self.inner, name)


def pinned(provider: ModelProvider, model: str) -> ModelProvider:
    """El proveedor con el modelo fijado, o el mismo si no se eligio ninguno."""
    return PinnedModelProvider(provider, model) if model else provider


__all__ = ["PinnedModelProvider", "pinned"]
