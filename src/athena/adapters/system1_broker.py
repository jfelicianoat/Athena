"""Synchronous System-1 endpoint using the existing authenticated Broker transport."""

from __future__ import annotations

import time
from collections.abc import Callable

from athena.adapters.ai_broker import AiBrokerModelProvider, _authentication_error
from athena.cancellation import CancellationToken
from athena.system1 import Judgment, JudgmentRequest

#: How long a broker that reported System-1 switched off is believed. The operator can
#: turn the service on without restarting Athena; asking again costs one public GET.
UNAVAILABLE_RECHECK_SECONDS = 60.0


class AiBrokerSystem1Client(AiBrokerModelProvider):
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: float = 75.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(base_url, token, request_timeout_seconds=timeout_seconds)
        self._available: bool | None = None
        self._checked_at = 0.0
        self._clock = clock

    async def initialize(self, cancellation: CancellationToken) -> bool:
        stale = (
            self._available is False
            and self._clock() - self._checked_at >= UNAVAILABLE_RECHECK_SECONDS
        )
        if self._available is None or stale:
            status, capabilities = await self._call(
                "GET",
                "/api/v1/capabilities",
                None,
                cancellation,
            )
            if status != 200:
                return False
            self._available = capabilities.get("system1_judgments") is True
            self._checked_at = self._clock()
        return self._available is True

    async def judge(
        self,
        request: JudgmentRequest,
        cancellation: CancellationToken,
    ) -> Judgment:
        cancellation.raise_if_cancelled()
        known = self._available
        if not await self.initialize(cancellation):
            if known is None and self._available is None:
                return Judgment(reason_code="CAPABILITIES_UNAVAILABLE")
            return Judgment(reason_code="SYSTEM1_UNAVAILABLE")
        status, payload = await self._call(
            "POST",
            "/api/v1/system1/judge",
            request.to_json(),
            cancellation,
        )
        if status != 200:
            problem = _authentication_error(status, payload)
            if problem is not None:
                return Judgment(reason_code=str(problem.details["broker_code"]))
            return Judgment(reason_code=f"HTTP_{status}")
        return Judgment.from_json(payload, request)
