from __future__ import annotations

import asyncio

import pytest

from athena.cancellation import CancellationSource
from athena.errors import CancellationError


def test_cancellation_propagates_once_and_wakes_waiters() -> None:
    async def scenario() -> None:
        source = CancellationSource()
        calls: list[str] = []
        source.token.register(lambda: calls.append("cancelled"))

        waiter = asyncio.create_task(source.token.wait())
        source.cancel()
        source.cancel()
        await asyncio.wait_for(waiter, timeout=1)

        assert source.token.is_cancelled
        assert calls == ["cancelled"]
        with pytest.raises(CancellationError):
            source.token.raise_if_cancelled()

    asyncio.run(scenario())


def test_late_cancellation_callback_runs_immediately() -> None:
    source = CancellationSource()
    source.cancel()
    calls: list[str] = []

    source.token.register(lambda: calls.append("late"))

    assert calls == ["late"]
