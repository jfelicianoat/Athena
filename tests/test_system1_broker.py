from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from athena.adapters.system1_broker import AiBrokerSystem1Client
from athena.cancellation import CancellationSource
from athena.events import EventName, InMemoryEventBus, RuntimeEvent
from athena.system1 import JudgmentRequest, System1, System1Config
from athena.types import JSONObject, JSONValue
from athena.verification import VerificationEvidence, VerificationResult, VerificationStatus


@contextmanager
def broker(
    payload: JSONObject,
    *,
    enabled: bool | None = True,
    evaluation: bool | None = None,
    status: int = 200,
) -> Iterator[tuple[str, list[tuple[str, str, JSONObject | None]]]]:
    calls: list[tuple[str, str, JSONObject | None]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            calls.append((self.path, self.headers.get("X-Admin-Token", ""), None))
            capabilities: dict[str, JSONValue] = {
                "presets": {"single": ["fast"]},
                "future_field": {},
            }
            if enabled is not None:
                capabilities["system1_judgments"] = enabled
            if evaluation is not None:
                capabilities["system1_evaluation"] = evaluation
            self.reply(200, capabilities)

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, self.headers.get("X-Admin-Token", ""), body))
            self.reply(status, payload)

        def reply(self, code: int, value: JSONObject) -> None:
            data = json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_system1_uses_authenticated_sync_contract_and_caches_capabilities() -> None:
    payload: JSONObject = {
        "use_case": "goal_completion",
        "accepted": True,
        "decision": False,
        "confidence": 0.99,
        "provider": "laya_mcp",
        "fallback_used": True,
        "reason_code": "PROVIDER_UNAVAILABLE",
        "future_response_field": ["ignored"],
    }

    async def scenario(base_url: str) -> None:
        client = AiBrokerSystem1Client(base_url, "fixture-token")
        request = JudgmentRequest("goal_completion", {"goal": "fix"}, "All parts done?")
        for _ in range(2):
            result = await client.judge(request, CancellationSource().token)
            assert result.accepted and result.decision is False
            assert result.provider == "laya_mcp" and result.fallback_used

    with broker(payload) as (url, calls):
        asyncio.run(scenario(url))
    assert [call[0] for call in calls] == [
        "/api/v1/capabilities",
        "/api/v1/system1/judge",
        "/api/v1/system1/judge",
    ]
    assert all(token == "fixture-token" for _, token, _ in calls)
    body = calls[1][2]
    assert body is not None
    assert set(body) == {"use_case", "input", "decision_type", "instructions", "cloud_allowed"}
    assert body["cloud_allowed"] is False


@pytest.mark.parametrize("enabled", [None, False])
@pytest.mark.parametrize("evaluation", [None, False, True])
def test_old_or_disabled_broker_keeps_existing_flow(
    enabled: bool | None, evaluation: bool | None
) -> None:
    with broker({}, enabled=enabled, evaluation=evaluation) as (url, calls):
        client = AiBrokerSystem1Client(url, "token")
        result = asyncio.run(
            client.judge(
                JudgmentRequest("x", {"goal": "fix"}, "done?"),
                CancellationSource().token,
            )
        )
        assert not result.accepted
        assert result.reason_code == "SYSTEM1_UNAVAILABLE"
        assert len(calls) == 1


@pytest.mark.parametrize("evaluation", [None, False, True])
def test_operational_judgments_do_not_depend_on_evaluation_capability(
    evaluation: bool | None,
) -> None:
    with broker(
        {"use_case": "goal_completion", "accepted": True, "decision": True, "confidence": 0.99},
        evaluation=evaluation,
    ) as (url, calls):
        result = asyncio.run(
            AiBrokerSystem1Client(url, "token").judge(
                JudgmentRequest("goal_completion", {"goal": "fix"}, "done?"),
                CancellationSource().token,
            )
        )
        assert result.accepted and result.decision is True
        assert len(calls) == 2
        body = calls[-1][2]
        assert body is not None and "target" not in body


@pytest.mark.parametrize(
    "decision,confidence,use_case",
    [
        ("true", 0.99, "x"),
        (1, 0.99, "x"),
        (True, float("nan"), "x"),
        (True, 2.0, "x"),
        (True, True, "x"),
        (True, 0.99, "different"),
    ],
)
def test_accepted_invalid_output_is_rejected(
    decision: str | bool | int,
    confidence: float | bool,
    use_case: str,
) -> None:
    with broker(
        {"use_case": use_case, "accepted": True, "decision": decision, "confidence": confidence}
    ) as (url, _):
        result = asyncio.run(
            AiBrokerSystem1Client(url, "token").judge(
                JudgmentRequest("x", {"goal": "fix"}, "done?"),
                CancellationSource().token,
            )
        )
        assert not result.accepted and result.reason_code == "INVALID_OUTPUT"


@pytest.mark.parametrize("status", [403, 404, 422, 429, 503])
def test_http_errors_fall_back_without_task_polling(status: int) -> None:
    with broker({}, status=status) as (url, calls):
        system = System1(
            AiBrokerSystem1Client(url, "token"),
            System1Config(goal_completion=True, shadow_mode=False),
            InMemoryEventBus(),
        )
        result = asyncio.run(
            system.completed({"goal": "fix"}, CancellationSource().token, session_id="run")
        )
        assert result is None
        assert len(calls) == 2


@pytest.mark.parametrize(
    "status,code,shape",
    [
        (403, "ADMIN_AUTH_REQUIRED", "error"),
        (503, "ADMIN_AUTH_BACKEND_UNAVAILABLE", "error"),
        (403, "ADMIN_AUTH_REQUIRED", "detail"),
        (503, "ADMIN_AUTH_BACKEND_UNAVAILABLE", "detail"),
    ],
)
def test_system1_preserves_authentication_reason_in_fallback_events(
    status: int, code: str, shape: str
) -> None:
    body: JSONObject = {"error": {"code": code}} if shape == "error" else {"detail": code}
    with broker(body, status=status) as (url, calls):
        events: list[RuntimeEvent] = []
        event_bus = InMemoryEventBus()
        event_bus.subscribe(events.append)
        system = System1(
            AiBrokerSystem1Client(url, "token"),
            System1Config(goal_completion=True, shadow_mode=False),
            event_bus,
        )
        assert (
            asyncio.run(
                system.completed({"goal": "fix"}, CancellationSource().token, session_id="r")
            )
            is None
        )
        event = next(item for item in events if item.name is EventName.SYSTEM1_JUDGED)
        assert event.payload["fallback"] is True
        assert event.payload["reason_code"] == code
        assert len(calls) == 2


@pytest.mark.parametrize(
    "decision,expected", [(0.0, True), (2.0, True), (0.5, False), (3, False), (True, False)]
)
def test_score_is_zero_based_ordinal_not_probability(
    decision: float | bool, expected: bool
) -> None:
    with broker({"use_case": "x", "accepted": True, "decision": decision, "confidence": 0.99}) as (
        url,
        _,
    ):
        result = asyncio.run(
            AiBrokerSystem1Client(url, "token").judge(
                JudgmentRequest(
                    "x", {"text": "hint"}, "relevance", "score", ("low", "mid", "high")
                ),
                CancellationSource().token,
            )
        )
        assert result.accepted is expected


@pytest.mark.parametrize(
    "reason",
    [
        "UNKNOWN_USE_CASE",
        "UNKNOWN_THRESHOLD_PROFILE",
        "INPUT_TOO_LARGE",
        "LOW_CONFIDENCE",
        "INSUFFICIENT_MARGIN",
        "MODEL_CAPABILITY_MISMATCH",
        "SELF_REPORTED_SCORE",
        "FUTURE_REASON",
    ],
)
def test_rejected_judgment_returns_to_legacy_without_retry(reason: str) -> None:
    with broker(
        {
            "use_case": "goal_completion",
            "accepted": False,
            "decision": None,
            "confidence": None,
            "reason_code": reason,
            "fallback_used": True,
        }
    ) as (url, calls):
        system = System1(
            AiBrokerSystem1Client(url, "token"),
            System1Config(goal_completion=True, shadow_mode=False),
            InMemoryEventBus(),
        )
        assert (
            asyncio.run(
                system.completed({"goal": "fix"}, CancellationSource().token, session_id="r")
            )
            is None
        )
        assert len(calls) == 2


@pytest.mark.parametrize(
    "reason,score_source",
    [("LOW_CONFIDENCE", "native"), ("SELF_REPORTED_SCORE", "self_reported")],
)
@pytest.mark.parametrize("use_case", ["goal_completion", "agora_review_gate", "ranking"])
def test_raw_attempt_scores_cannot_replace_an_accepted_top_level_decision(
    use_case: str, reason: str, score_source: str
) -> None:
    decision = 0 if use_case == "ranking" else True
    with broker(
        {
            "use_case": use_case,
            "accepted": False,
            "decision": None,
            "confidence": None,
            "reason_code": reason,
            "fallback_used": True,
            "attempts": [
                {
                    "provider": "ollama_system1",
                    "model": "nimble:latest" if score_source == "native" else "teacher",
                    "decision": decision,
                    "confidence": 1.0,
                    "score_source": score_source,
                    "reason_code": reason,
                    "alternatives": [
                        {"value": 1, "confidence": 0.0},
                        {"value": 2, "confidence": 0.0},
                    ]
                    if use_case == "ranking"
                    else [{"value": False, "confidence": 0.0}],
                }
            ],
        }
    ) as (url, calls):
        events: list[RuntimeEvent] = []
        event_bus = InMemoryEventBus()
        event_bus.subscribe(events.append)
        system = System1(
            AiBrokerSystem1Client(url, "fixture"),
            System1Config(
                goal_completion=True,
                reviewer_gate=True,
                context_filtering=True,
                shadow_mode=False,
            ),
            event_bus,
        )
        cancellation = CancellationSource().token
        if use_case == "goal_completion":
            assert (
                asyncio.run(system.completed({"goal": "fix"}, cancellation, session_id="r")) is None
            )
        elif use_case == "agora_review_gate":
            assert not asyncio.run(
                system.skip_reviewer(
                    {"goal": "fix", "output": "done"},
                    cancellation,
                    session_id="r",
                    verification=VerificationResult(
                        VerificationStatus.PASSED,
                        (VerificationEvidence("tests", "Checks pass", metadata={"passed": True}),),
                        "Project checks pass",
                    ),
                )
            )
        else:
            candidates = ("context that must remain", "additional notes")
            assert (
                asyncio.run(system.filter_context("fix", candidates, cancellation, session_id="r"))
                == candidates
            )
        judgment_event = next(event for event in events if event.name is EventName.SYSTEM1_JUDGED)
        assert judgment_event.payload["reason_code"] == reason
        assert judgment_event.payload["fallback"] is True
        assert judgment_event.payload["decision"] is None
        assert len(calls) == 2
        body = calls[-1][2]
        assert body is not None and "target" not in body


def test_a_disabled_broker_is_asked_again_after_the_recheck_interval() -> None:
    now = [0.0]
    switched_on = [False]
    accepted_payload: JSONObject = {
        "use_case": "goal_completion",
        "accepted": True,
        "decision": True,
        "confidence": 0.99,
    }

    class Client(AiBrokerSystem1Client):
        async def _call(self, method, path, body, cancellation):  # type: ignore[no-untyped-def]
            calls.append(path)
            if path == "/api/v1/capabilities":
                return 200, {"system1_judgments": switched_on[0]}
            return 200, accepted_payload

    calls: list[str] = []
    client = Client("http://broker", "token", clock=lambda: now[0])
    request = JudgmentRequest("goal_completion", {"goal": "fix"}, "done?")
    token = CancellationSource().token
    assert asyncio.run(client.judge(request, token)).reason_code == "SYSTEM1_UNAVAILABLE"
    now[0] = 30.0
    switched_on[0] = True
    # Still within the interval: no new discovery, no judgment.
    assert asyncio.run(client.judge(request, token)).reason_code == "SYSTEM1_UNAVAILABLE"
    assert calls == ["/api/v1/capabilities"]
    now[0] = 61.0
    assert asyncio.run(client.judge(request, token)).accepted
    now[0] = 10_000.0
    # Once on, it stays known: one discovery per transition, not per judgment.
    assert asyncio.run(client.judge(request, token)).accepted
    assert calls == [
        "/api/v1/capabilities",
        "/api/v1/capabilities",
        "/api/v1/system1/judge",
        "/api/v1/system1/judge",
    ]
