from __future__ import annotations

import asyncio

import pytest

from athena.cancellation import CancellationSource
from athena.errors import ModelStreamingUnsupportedError
from athena.models import ModelMessage, ModelProvider, ModelRequest, ModelResponse, ModelRole
from athena.testing import FakeModelProvider


def test_fake_model_provider_obeys_provider_contract() -> None:
    async def scenario() -> None:
        expected = ModelResponse(content="ok", model="fake-1", finish_reason="stop")
        provider = FakeModelProvider([expected])
        request = ModelRequest(messages=(ModelMessage(ModelRole.USER, "hello"),))
        cancellation = CancellationSource()

        assert isinstance(provider, ModelProvider)
        assert await provider.complete(request, cancellation.token) == expected
        assert provider.requests == [request]
        assert not provider.capabilities().streaming
        assert (await provider.health(cancellation.token)).status == "healthy"

    asyncio.run(scenario())


def test_non_streaming_provider_fails_explicitly() -> None:
    async def scenario() -> None:
        provider = FakeModelProvider([])
        request = ModelRequest(messages=(ModelMessage(ModelRole.USER, "hello"),))
        stream = provider.stream(request, CancellationSource().token)

        with pytest.raises(ModelStreamingUnsupportedError):
            await anext(stream)

    asyncio.run(scenario())
