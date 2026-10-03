"""Synchronous System-1 endpoint using the existing authenticated Broker transport."""

from __future__ import annotations

from athena.adapters.ai_broker import AiBrokerModelProvider
from athena.cancellation import CancellationToken
from athena.system1 import Judgment, JudgmentRequest


class AiBrokerSystem1Client(AiBrokerModelProvider):
    def __init__(self, base_url: str, token: str, *, timeout_seconds: float = 75.0) -> None:
        super().__init__(base_url, token, request_timeout_seconds=timeout_seconds)
        self._available: bool | None = None

    async def initialize(self, cancellation: CancellationToken) -> bool:
        if self._available is None:
            status, capabilities = await self._call(
                "GET",
                "/api/v1/capabilities",
                None,
                cancellation,
            )
            if status != 200:
                return False
            self._available = capabilities.get("system1_judgments") is True
        return self._available

    async def judge(
        self,
        request: JudgmentRequest,
        cancellation: CancellationToken,
    ) -> Judgment:
        cancellation.raise_if_cancelled()
        if (
            self._available is None
            and not await self.initialize(cancellation)
            and self._available is None
        ):
            return Judgment(reason_code="CAPABILITIES_UNAVAILABLE")
        if not self._available:
            return Judgment(reason_code="SYSTEM1_UNAVAILABLE")
        status, payload = await self._call(
            "POST",
            "/api/v1/system1/judge",
            request.to_json(),
            cancellation,
        )
        if status != 200:
            return Judgment(reason_code=f"HTTP_{status}")
        return Judgment.from_json(payload, request)
