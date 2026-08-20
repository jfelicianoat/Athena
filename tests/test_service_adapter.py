from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from athena.adapters.service import (
    ApprovalRegistry,
    AthenaService,
    CapabilityMode,
    RemotePermissionPrompt,
    RunOptions,
    RunRegistry,
    ServiceConfig,
)
from athena.adapters.service.approvals import (
    ApprovalAbandonedError,
    PendingApproval,
)
from athena.adapters.service.projections import session_to_json
from athena.cancellation import CancellationToken
from athena.events import EventName, InMemoryEventBus, ModelEvent, RuntimeEvent
from athena.models import (
    ModelCapabilities,
    ModelHealth,
    ModelHealthStatus,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
)
from athena.permissions import PermissionDecision, PermissionRequest, RiskLevel, RiskTier
from athena.session_store import SqliteSessionStore
from athena.state import AgentStatus
from athena.stores import SqliteToolResultStore
from athena.types import JSONObject
from athena.workspace import Workspace

CALC_BROKEN = "def add(a, b):\n    return a - b\n"
CALC_FIXED = "def add(a, b):\n    return a + b\n"
CALC_TEST = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def _git(root: Path, *arguments: str) -> None:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments], capture_output=True, check=False, text=True
    )
    if completed.returncode != 0:
        pytest.fail(f"git {' '.join(arguments)} failed: {completed.stderr}")


def _sandbox(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    command = f'"{sys.executable}" -m pytest -q'
    (root / "calc.py").write_text(CALC_BROKEN, encoding="utf-8")
    (root / "test_calc.py").write_text(CALC_TEST, encoding="utf-8")
    (root / "AGENTS.md").write_text(
        f"# Sandbox\n\n## Verification\n\n```\n{command}\n```\n", encoding="utf-8"
    )
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "athena@example.invalid")
    _git(root, "config", "user.name", "Athena Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    return root


class _ScriptedProvider(ModelProvider):
    def __init__(self, responses: Sequence[ModelResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[ModelRequest] = []
        self.started = asyncio.Event()

    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        cancellation.raise_if_cancelled()
        self.requests.append(request)
        self.started.set()
        if not self._responses:
            return ModelResponse("Nothing left to do.", "scripted", "stop")
        return self._responses.pop(0)

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


def _call(call_id: str, name: str, arguments: JSONObject) -> ModelResponse:
    return ModelResponse(
        "", "scripted", "tool_calls", tool_calls=(ModelToolCall(call_id, name, arguments),)
    )


def _registry(tmp_path: Path, provider: ModelProvider) -> RunRegistry:
    return RunRegistry(
        provider,
        InMemoryEventBus(),
        SqliteSessionStore(tmp_path / "sessions.db"),
        SqliteToolResultStore(tmp_path / "results.db"),
    )


def _permission(workspace: Workspace) -> PermissionRequest:
    return PermissionRequest(
        tool_name="write_file",
        operation="write_file",
        action="create notes.md",
        workspace=workspace,
        risk=RiskLevel.MEDIUM,
        tier=RiskTier.R1_WORKSPACE_WRITE,
        is_read_only=False,
        is_destructive=False,
        reason="The agent wants to write a file.",
        possible_effects=("Creates notes.md",),
    )


# ------------------------------------------------------------------ approvals


def test_no_client_attached_denies_immediately(tmp_path: Path) -> None:
    """The unattended case: nobody is going to answer, so do not wait to find out."""
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        prompt = RemotePermissionPrompt(
            ApprovalRegistry(),
            "run-1",
            lambda pending: pytest.fail("must not publish when nobody is attached"),
            lambda: False,
        )

        decision = await asyncio.wait_for(
            prompt.confirm(_permission(Workspace.from_path(root))), timeout=2
        )

        assert decision is PermissionDecision.DENY

    asyncio.run(scenario())


def test_an_undelivered_request_denies_after_the_short_window(tmp_path: Path) -> None:
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        published: list[PendingApproval] = []
        prompt = RemotePermissionPrompt(
            ApprovalRegistry(),
            "run-1",
            published.append,
            lambda: True,
            delivery_timeout_seconds=0.2,
            approval_timeout_seconds=30.0,
        )

        decision = await asyncio.wait_for(
            prompt.confirm(_permission(Workspace.from_path(root))), timeout=5
        )

        assert decision is PermissionDecision.DENY
        assert len(published) == 1, "the request was published even though nobody looked"

    asyncio.run(scenario())


def test_the_human_clock_only_starts_on_acknowledgement(tmp_path: Path) -> None:
    """A slow link delays the question; it must not eat the time to answer it."""
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        registry = ApprovalRegistry()
        published: list[PendingApproval] = []
        prompt = RemotePermissionPrompt(
            registry,
            "run-1",
            published.append,
            lambda: True,
            delivery_timeout_seconds=0.3,
            approval_timeout_seconds=5.0,
        )
        task = asyncio.create_task(prompt.confirm(_permission(Workspace.from_path(root))))
        await asyncio.sleep(0.05)

        # Acknowledge inside the delivery window, then answer well after it would have
        # expired. The delivery clock must have been replaced by the human one.
        registry.acknowledge(published[0].request_id, 5.0)
        await asyncio.sleep(0.5)
        registry.resolve(published[0].request_id, PermissionDecision.ALLOW)

        assert await asyncio.wait_for(task, timeout=5) is PermissionDecision.ALLOW

    asyncio.run(scenario())


def test_an_acknowledged_request_still_denies_when_nobody_answers(tmp_path: Path) -> None:
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        registry = ApprovalRegistry()
        published: list[PendingApproval] = []
        prompt = RemotePermissionPrompt(
            registry,
            "run-1",
            published.append,
            lambda: True,
            delivery_timeout_seconds=0.2,
            approval_timeout_seconds=0.3,
        )
        task = asyncio.create_task(prompt.confirm(_permission(Workspace.from_path(root))))
        await asyncio.sleep(0.05)
        registry.acknowledge(published[0].request_id, 0.3)

        assert await asyncio.wait_for(task, timeout=5) is PermissionDecision.DENY

    asyncio.run(scenario())


def test_an_answer_is_single_use(tmp_path: Path) -> None:
    root = _sandbox(tmp_path / "repo")
    registry = ApprovalRegistry()

    async def scenario() -> None:
        published: list[PendingApproval] = []
        prompt = RemotePermissionPrompt(
            registry, "run-1", published.append, lambda: True, delivery_timeout_seconds=5.0
        )
        task = asyncio.create_task(prompt.confirm(_permission(Workspace.from_path(root))))
        await asyncio.sleep(0.05)
        request_id = published[0].request_id

        assert registry.resolve(request_id, PermissionDecision.ALLOW) is not None
        # A replayed POST must not approve a second action.
        assert registry.resolve(request_id, PermissionDecision.ALLOW) is None
        assert await asyncio.wait_for(task, timeout=5) is PermissionDecision.ALLOW

    asyncio.run(scenario())


def test_a_cancelled_run_withdraws_its_questions(tmp_path: Path) -> None:
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        registry = ApprovalRegistry()
        published: list[PendingApproval] = []
        prompt = RemotePermissionPrompt(
            registry, "run-1", published.append, lambda: True, delivery_timeout_seconds=5.0
        )
        task = asyncio.create_task(prompt.confirm(_permission(Workspace.from_path(root))))
        await asyncio.sleep(0.05)

        registry.cancel_run("run-1")

        assert await asyncio.wait_for(task, timeout=5) is PermissionDecision.DENY
        # A late answer arriving after cancellation is refused, not applied.
        assert registry.resolve(published[0].request_id, PermissionDecision.ALLOW) is None

    asyncio.run(scenario())


def test_repeated_silence_abandons_the_run(tmp_path: Path) -> None:
    """Burning a whole budget denying itself is waste; stop instead."""
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        prompt = RemotePermissionPrompt(
            ApprovalRegistry(),
            "run-1",
            lambda pending: None,
            lambda: True,
            delivery_timeout_seconds=0.05,
            approval_timeout_seconds=0.05,
            max_consecutive_timeouts=2,
        )
        request = _permission(Workspace.from_path(root))

        assert await prompt.confirm(request) is PermissionDecision.DENY
        with pytest.raises(ApprovalAbandonedError):
            await prompt.confirm(request)

    asyncio.run(scenario())


# ------------------------------------------------------------------ registry


def test_a_run_is_addressable_before_it_emits_anything(tmp_path: Path) -> None:
    """The service names the run, so an intent can arrive on its first millisecond."""
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        provider = _ScriptedProvider([ModelResponse("Nothing to do.", "scripted", "stop")])
        registry = _registry(tmp_path, provider)

        run_id = await registry.start("Look", Workspace.from_path(root))

        # Addressable straight away: the snapshot exists before the run finishes.
        assert run_id in registry.live_ids()
        snapshot = await registry.snapshot(run_id)
        assert snapshot is not None
        assert snapshot.session_id == run_id
        await registry.shutdown()

    asyncio.run(scenario())


def test_capability_modes_decide_which_tools_exist(tmp_path: Path) -> None:
    async def scenario() -> None:
        registry = _registry(tmp_path, _ScriptedProvider([]))
        bus = registry.event_bus

        read_only = registry.tools_for(RunOptions(CapabilityMode.OFF, CapabilityMode.OFF), bus)
        asking = registry.tools_for(RunOptions(), bus)

        read_names = {tool.spec.name for tool in read_only}
        ask_names = {tool.spec.name for tool in asking}
        assert {"write_file", "edit_file", "bash"}.isdisjoint(read_names)
        assert {"write_file", "edit_file", "bash", "git_commit"} <= ask_names

    asyncio.run(scenario())


def test_defaults_ask_rather_than_allow() -> None:
    options = RunOptions.from_json({})

    assert options.writes is CapabilityMode.ASK
    assert options.execution is CapabilityMode.ASK


def test_only_the_controlling_client_may_send_intents(tmp_path: Path) -> None:
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        provider = _ScriptedProvider([ModelResponse("Done.", "scripted", "stop")])
        registry = _registry(tmp_path, provider)
        run_id = await registry.start("Look", Workspace.from_path(root))

        controller = registry.subscribe(run_id, control=True)
        observer = registry.subscribe(run_id, control=True)

        assert controller.controls is True
        assert observer.controls is False
        assert registry.controls(run_id, controller.subscriber_id)
        assert not registry.controls(run_id, observer.subscriber_id)
        await registry.shutdown()

    asyncio.run(scenario())


def test_a_slow_subscriber_is_dropped_not_the_runtime(tmp_path: Path) -> None:
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        registry = _registry(tmp_path, _ScriptedProvider([]))
        run_id = await registry.start("Look", Workspace.from_path(root))
        subscriber = registry.subscribe(run_id)
        from athena.events import RuntimeEvent

        for index in range(600):
            registry._fan_out(RuntimeEvent(EventName.TOOL_STARTED, run_id, {"i": index}))

        assert subscriber.dropped > 0, "backpressure must discard, not grow without bound"
        await registry.shutdown()

    asyncio.run(scenario())


# ------------------------------------------------------------------ http surface


def _config(**extra: object) -> ServiceConfig:
    return ServiceConfig(port=0, token="test-token", **extra)  # type: ignore[arg-type]


async def _request(
    host: str,
    port: int,
    method: str,
    path: str,
    *,
    token: str | None = "test-token",
    body: JSONObject | None = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, str]:
    reader, writer = await asyncio.open_connection(host, port)
    payload = json.dumps(body).encode("utf-8") if body is not None else b""
    lines = [f"{method} {path} HTTP/1.1", f"Host: {host}:{port}"]
    for name, value in (headers or {}).items():
        lines.append(f"{name}: {value}")
    if token:
        lines.append(f"Authorization: Bearer {token}")
    if payload:
        lines.append("Content-Type: application/json")
    lines.append(f"Content-Length: {len(payload)}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + payload)
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head, _, response_body = raw.partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n")[0].split(b" ")[1])
    return status, response_body.decode("utf-8")


def test_health_needs_no_token_and_everything_else_does(tmp_path: Path) -> None:
    async def scenario() -> None:
        registry = _registry(tmp_path, _ScriptedProvider([]))
        service = AthenaService(registry, _config())
        host, port = await service.start()
        try:
            status, body = await _request(host, port, "GET", "/v1/health", token=None)
            assert status == 200
            assert json.loads(body)["status"] == "ok"

            status, _ = await _request(host, port, "GET", "/v1/runs", token=None)
            assert status == 401

            status, _ = await _request(host, port, "GET", "/v1/runs", token="wrong")
            assert status == 401

            status, body = await _request(host, port, "GET", "/v1/runs")
            assert status == 200
            assert json.loads(body)["runs"] == []
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_the_service_refuses_to_bind_beyond_loopback() -> None:
    with pytest.raises(ValueError):
        ServiceConfig(host="0.0.0.0", token="t")


def test_an_unknown_route_is_a_clean_404(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = AthenaService(_registry(tmp_path, _ScriptedProvider([])), _config())
        host, port = await service.start()
        try:
            status, body = await _request(host, port, "GET", "/v1/nope")
            assert status == 404
            assert json.loads(body)["error"]["code"] == "not_found"
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_starting_a_run_over_http_reports_its_capabilities(tmp_path: Path) -> None:
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        provider = _ScriptedProvider([ModelResponse("Nothing to change.", "scripted", "stop")])
        service = AthenaService(_registry(tmp_path, provider), _config())
        host, port = await service.start()
        try:
            status, body = await _request(
                host,
                port,
                "POST",
                "/v1/runs",
                body={"objective": "Explain calc.py", "workspace": str(root)},
            )

            assert status == 201
            payload = json.loads(body)
            assert payload["writes"] == "ask"
            assert payload["exec"] == "ask"
            assert payload["run_id"]
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_a_workspace_outside_the_authorised_set_is_refused(tmp_path: Path) -> None:
    """A real, valid workspace is still refused when the host application says no."""
    root = _sandbox(tmp_path / "repo")
    assert (root / "calc.py").is_file()

    async def scenario() -> None:
        service = AthenaService(
            _registry(tmp_path, _ScriptedProvider([])),
            _config(authorized_workspace=lambda path: False),
        )
        host, port = await service.start()
        try:
            status, body = await _request(
                host, port, "POST", "/v1/runs", body={"objective": "x", "workspace": str(root)}
            )
            assert status == 400
            assert "not authorised" in json.loads(body)["error"]["message"]
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_a_bad_request_body_is_a_validation_error(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = AthenaService(_registry(tmp_path, _ScriptedProvider([])), _config())
        host, port = await service.start()
        try:
            status, body = await _request(host, port, "POST", "/v1/runs", body={"objective": ""})
            assert status == 400
            assert json.loads(body)["error"]["code"] == "tool_validation_error"
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_an_expired_artifact_is_gone_not_empty(tmp_path: Path) -> None:
    """A reference that resolves to nothing would be worse than one that fails."""

    async def scenario() -> None:
        service = AthenaService(_registry(tmp_path, _ScriptedProvider([])), _config())
        host, port = await service.start()
        try:
            status, body = await _request(host, port, "GET", "/v1/results/nope")
            assert status == 410
            assert json.loads(body)["error"]["code"] == "tool_result_unavailable"
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_a_stored_artifact_is_served_verbatim(tmp_path: Path) -> None:
    async def scenario() -> None:
        registry = _registry(tmp_path, _ScriptedProvider([]))
        service = AthenaService(registry, _config())
        host, port = await service.start()
        try:
            from athena.cancellation import CancellationSource

            reference = await registry.result_store.put(
                "PAYLOAD-" + "x" * 5_000,
                media_type="text/plain",
                cancellation=CancellationSource().token,
            )
            status, body = await _request(host, port, "GET", f"/v1/results/{reference.store_key}")

            assert status == 200
            assert body.startswith("PAYLOAD-")
            assert len(body) > 5_000
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_recovery_pending_runs_are_listed_after_a_restart(tmp_path: Path) -> None:
    """A crashed run must never be presented as finished."""
    root = _sandbox(tmp_path / "repo")
    database = tmp_path / "sessions.db"

    async def scenario() -> None:
        from athena.session_store import SessionRecord
        from athena.working_state import WorkingState

        store = SqliteSessionStore(database)
        await store.save(
            SessionRecord(
                session_id="abandoned-1",
                workspace_id="ws",
                status=AgentStatus.RUNNING,
                working_memory=WorkingState(objective="Half-finished work"),
            )
        )

        registry = RunRegistry(
            _ScriptedProvider([]),
            InMemoryEventBus(),
            SqliteSessionStore(database),
            SqliteToolResultStore(tmp_path / "results.db"),
        )
        service = AthenaService(registry, _config())
        host, port = await service.start()
        try:
            status, body = await _request(host, port, "GET", "/v1/runs?status=recovery_pending")

            assert status == 200
            runs = json.loads(body)["runs"]
            assert [run["run_id"] for run in runs] == ["abandoned-1"]
            assert runs[0]["resumable"] is True
            assert runs[0]["status"] == "recovery_pending"
            assert root.exists()
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_the_snapshot_projection_carries_what_a_client_needs() -> None:
    from athena.session_store import EventCheckpoint, SessionRecord
    from athena.tools import ToolResultReference
    from athena.working_state import WorkingState

    record = SessionRecord(
        session_id="s-1",
        workspace_id="ws-1",
        status=AgentStatus.RECOVERY_PENDING,
        working_memory=WorkingState(objective="Fix calc").modifying(files_modified=("calc.py",)),
        tool_references=(ToolResultReference("k", "text/plain", 10, "abc"),),
        verification={"status": "failed"},
        checkpoints=(EventCheckpoint("started"),),
    )

    payload = json.loads(json.dumps(session_to_json(record)))

    assert payload["run_id"] == "s-1"
    assert payload["status"] == "recovery_pending"
    assert payload["resumable"] is True
    assert payload["objective"] == "Fix calc"
    assert payload["tool_references"][0]["uri"].startswith("athena-result://")
    assert payload["working_memory"]["files_modified"] == ["calc.py"]
    assert [item["name"] for item in payload["checkpoints"]] == ["started"]


def test_an_intent_must_prove_it_comes_from_the_controlling_client(tmp_path: Path) -> None:
    """Intents arrive on their own connection, so the client echoes its subscriber id.

    Without this, "one writer per run" would be unenforceable across connections — and
    with it but undocumented, the controlling client would be locked out of its own run.
    """
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        registry = _registry(tmp_path, _ScriptedProvider([]))
        service = AthenaService(registry, _config())
        host, port = await service.start()
        try:
            run_id = await registry.start("Look", Workspace.from_path(root))
            controller = registry.subscribe(run_id, control=True)

            # No header at all: refused while somebody holds control.
            assert not registry.controls(run_id, None)
            # The wrong id: refused.
            assert not registry.controls(run_id, "someone-else")
            # The id the stream handed out: accepted.
            assert registry.controls(run_id, controller.subscriber_id)

            status, body = await _request(
                host,
                port,
                "POST",
                f"/v1/runs/{run_id}/approvals/missing",
                body={"decision": "allow"},
            )
            assert status == 403
            assert json.loads(body)["error"]["code"] == "not_controller"
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_the_state_frame_hands_the_client_what_it_needs_to_act() -> None:
    """A subscriber id the client never sees would make control unusable."""
    import inspect

    from athena.adapters.service import server

    source = inspect.getsource(server.AthenaService._stream_events)

    assert '"subscriber_id": subscriber.subscriber_id' in source
    assert '"controls": subscriber.controls' in source
    assert '"pending_approvals"' in source


def test_arguments_are_summarised_and_redacted_before_they_leave(tmp_path: Path) -> None:
    """An approval carries what a person needs to judge it, not the payload itself."""
    from athena.adapters.service.approvals import sanitise_arguments

    sanitised = sanitise_arguments(
        {
            "path": "calc.py",
            "content": "x" * 40_000,
            "api_key": "sk-super-secret-value",
            "overwrite": True,
        }
    )

    assert sanitised["path"] == "calc.py"
    assert sanitised["overwrite"] is True
    # The file body is described, never shipped.
    content = sanitised["content"]
    assert isinstance(content, dict)
    assert content["chars"] == 40_000
    assert content["truncated"] is True
    assert len(str(content["preview"])) <= 200
    # A secret never leaves the process in clear.
    assert sanitised["api_key"] == "[REDACTED]"


def test_the_published_approval_carries_everything_a_human_needs(tmp_path: Path) -> None:
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        registry = _registry(tmp_path, _ScriptedProvider([]))
        run_id = await registry.start("Look", Workspace.from_path(root))
        subscriber = registry.subscribe(run_id)
        workspace = Workspace.from_path(root)
        request = PermissionRequest(
            tool_name="write_file",
            operation="write_file",
            action="overwrite calc.py",
            workspace=workspace,
            risk=RiskLevel.HIGH,
            tier=RiskTier.R1_WORKSPACE_WRITE,
            is_read_only=False,
            is_destructive=True,
            reason="The agent requested a full-content write.",
            possible_effects=("Replaces calc.py", "Discards most of the file"),
            resources=(str(root / "calc.py"),),
            arguments={"path": "calc.py", "content": "y" * 5_000, "token": "sk-abc123456789"},
        )
        pending = PendingApproval(
            request_id="req-1",
            run_id=run_id,
            request=request,
            future=asyncio.get_running_loop().create_future(),
            deadline_monotonic=0.0,
        )

        registry._publish_approval(pending)

        event = subscriber.queue.get_nowait()
        assert event is not None
        payload = json.loads(json.dumps(dict(event.payload)))
        # Everything the interface must display.
        assert payload["tool_name"] == "write_file"
        assert payload["action"] == "overwrite calc.py"
        assert payload["risk"] == "high"
        assert payload["tier"] == "r1_workspace_write"
        assert payload["reason"]
        assert payload["is_destructive"] is True
        efectos = payload["possible_effects"]
        assert isinstance(efectos, list)
        assert len(efectos) == 2
        assert payload["resources"]
        assert payload["workspace"]
        # Arguments arrive summarised and with the secret gone.
        argumentos = payload["arguments"]
        assert isinstance(argumentos, dict)
        assert argumentos["path"] == "calc.py"
        assert argumentos["content"]["chars"] == 5_000
        assert "sk-abc123456789" not in json.dumps(payload)
        await registry.shutdown()

    asyncio.run(scenario())


# ------------------------------------------------------- reconnect, resume, idempotency


async def _open_stream(
    host: str,
    port: int,
    run_id: str,
    *,
    last_event_id: str | None = None,
    token: str = "test-token",
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open the SSE stream the way a reconnecting client would."""
    reader, writer = await asyncio.open_connection(host, port)
    lines = [
        f"GET /v1/runs/{run_id}/events HTTP/1.1",
        f"Host: {host}:{port}",
        f"Authorization: Bearer {token}",
        "Accept: text/event-stream",
    ]
    if last_event_id is not None:
        lines.append(f"Last-Event-ID: {last_event_id}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
    await writer.drain()
    await reader.readuntil(b"\r\n\r\n")
    return reader, writer


async def _read_frame(reader: asyncio.StreamReader) -> tuple[str, str, dict[str, Any]]:
    """One SSE frame as (event id, event name, payload), skipping keepalives."""
    event_id = ""
    name = ""
    while True:
        raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
        line = raw.decode("utf-8").rstrip("\n")
        if line.startswith(": "):
            continue
        if line.startswith("id: "):
            event_id = line[4:]
        elif line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: "):
            payload = json.loads(line[6:])
            assert isinstance(payload, dict)
            return event_id, name, payload


def test_a_reconnecting_client_resumes_from_the_last_event_it_saw(tmp_path: Path) -> None:
    """The point of putting ids on the wire.

    Without honouring `Last-Event-ID`, a client that drops for two seconds pays a full
    resynchronisation and throws away everything it had derived — and anything the
    snapshot does not happen to capture is simply lost.
    """
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        registry = _registry(tmp_path, _ScriptedProvider([]))
        service = AthenaService(registry, _config())
        host, port = await service.start()
        try:
            run_id = await registry.start("Look", Workspace.from_path(root))
            reader, writer = await _open_stream(host, port, run_id)
            await _read_frame(reader)  # the state frame

            registry._fan_out(RuntimeEvent(EventName.TOOL_STARTED, run_id, {"i": 1}))
            first_id, _, _ = await _read_frame(reader)
            writer.close()

            # Missed while away.
            for index in (2, 3):
                registry._fan_out(RuntimeEvent(EventName.TOOL_STARTED, run_id, {"i": index}))

            reader, writer = await _open_stream(host, port, run_id, last_event_id=first_id)
            _, name, state = await _read_frame(reader)
            assert name == "state"
            assert state["resumed"] is True
            assert state["snapshot"] is None, "a resume does not need a resynchronisation"

            seen = [(await _read_frame(reader))[2]["payload"]["i"] for _ in range(2)]
            writer.close()

            assert seen == [2, 3], "in order, and only what was missed"
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_an_unknown_resume_point_falls_back_to_a_snapshot(tmp_path: Path) -> None:
    # "Up to date" and "too far behind to replay" must not look the same. A client that
    # was away longer than the buffer has to be told to resynchronise, not reassured.
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        registry = _registry(tmp_path, _ScriptedProvider([]))
        service = AthenaService(registry, _config())
        host, port = await service.start()
        try:
            run_id = await registry.start("Look", Workspace.from_path(root))
            reader, writer = await _open_stream(
                host, port, run_id, last_event_id="an-id-from-another-life"
            )
            _, name, state = await _read_frame(reader)
            writer.close()

            assert name == "state"
            assert state["resumed"] is False
            assert state["snapshot"] is not None
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_events_arriving_during_a_replay_are_not_sent_twice(tmp_path: Path) -> None:
    # The stream subscribes before reading the buffer, so the window between them is
    # covered twice. A client that counts things would otherwise count them twice.
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        registry = _registry(tmp_path, _ScriptedProvider([]))
        service = AthenaService(registry, _config())
        host, port = await service.start()
        try:
            run_id = await registry.start("Look", Workspace.from_path(root))
            reader, writer = await _open_stream(host, port, run_id)
            await _read_frame(reader)
            registry._fan_out(RuntimeEvent(EventName.TOOL_STARTED, run_id, {"i": 1}))
            first_id, _, _ = await _read_frame(reader)
            writer.close()

            registry._fan_out(RuntimeEvent(EventName.TOOL_STARTED, run_id, {"i": 2}))
            reader, writer = await _open_stream(host, port, run_id, last_event_id=first_id)
            await _read_frame(reader)
            replayed_id, _, payload = await _read_frame(reader)
            assert payload["payload"]["i"] == 2

            registry._fan_out(RuntimeEvent(EventName.TOOL_STARTED, run_id, {"i": 3}))
            next_id, _, following = await _read_frame(reader)
            writer.close()

            assert next_id != replayed_id
            assert following["payload"]["i"] == 3
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_the_replay_buffer_does_not_grow_without_bound(tmp_path: Path) -> None:
    """A journal sized by how long a client stays away is a journal the runtime cannot
    afford. Past the window the answer is a snapshot, which always works."""
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        registry = _registry(tmp_path, _ScriptedProvider([]))
        run_id = await registry.start("Look", Workspace.from_path(root))
        first = RuntimeEvent(EventName.TOOL_STARTED, run_id, {"i": 0})
        registry._fan_out(first)
        for index in range(1, 400):
            registry._fan_out(RuntimeEvent(EventName.TOOL_STARTED, run_id, {"i": index}))

        assert registry.replay(run_id, first.event_id) is None, "aged out, so resynchronise"
        await registry.shutdown()

    asyncio.run(scenario())


def test_a_repeated_create_run_makes_one_run(tmp_path: Path) -> None:
    """A retry is not a second request.

    Two agents on one workspace is exactly what a client retrying a timed-out POST is
    trying to avoid, and starting a run is not the kind of thing that can be undone by
    noticing later.
    """
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        registry = _registry(tmp_path, _ScriptedProvider([]))
        service = AthenaService(registry, _config())
        host, port = await service.start()
        try:
            body = {"objective": "Look around", "workspace": str(root), "writes": "off"}
            first_status, first_body = await _request(
                host, port, "POST", "/v1/runs", body=body, headers={"Idempotency-Key": "k-1"}
            )
            second_status, second_body = await _request(
                host, port, "POST", "/v1/runs", body=body, headers={"Idempotency-Key": "k-1"}
            )

            assert first_status == 201
            assert second_status == 200
            assert json.loads(first_body)["run_id"] == json.loads(second_body)["run_id"]
            assert json.loads(second_body)["idempotent_replay"] is True
            assert len(registry.live_ids()) == 1
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_two_concurrent_retries_of_one_key_still_make_one_run(tmp_path: Path) -> None:
    # The window that matters is the one where the first call has not finished. A
    # check-then-act across `await registry.start(...)` would let both callers miss.
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        registry = _registry(tmp_path, _ScriptedProvider([]))
        service = AthenaService(registry, _config())
        host, port = await service.start()
        try:
            body = {"objective": "Look around", "workspace": str(root), "writes": "off"}
            results = await asyncio.gather(
                *(
                    _request(
                        host,
                        port,
                        "POST",
                        "/v1/runs",
                        body=body,
                        headers={"Idempotency-Key": "same"},
                    )
                    for _ in range(4)
                )
            )

            run_ids = {json.loads(payload)["run_id"] for _, payload in results}
            assert len(run_ids) == 1
            assert len(registry.live_ids()) == 1
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_different_keys_make_different_runs(tmp_path: Path) -> None:
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        registry = _registry(tmp_path, _ScriptedProvider([]))
        service = AthenaService(registry, _config())
        host, port = await service.start()
        try:
            body = {"objective": "Look around", "workspace": str(root), "writes": "off"}
            for key in ("a", "b"):
                await _request(
                    host, port, "POST", "/v1/runs", body=body, headers={"Idempotency-Key": key}
                )

            assert len(registry.live_ids()) == 2
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_a_failed_create_does_not_poison_the_key(tmp_path: Path) -> None:
    # Caching a failure would leave the caller unable to ever succeed with that key, which
    # turns a transient problem into a permanent one.
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        registry = _registry(tmp_path, _ScriptedProvider([]))
        service = AthenaService(registry, _config())
        host, port = await service.start()
        try:
            bad = {"objective": "", "workspace": str(root)}
            status, _ = await _request(
                host, port, "POST", "/v1/runs", body=bad, headers={"Idempotency-Key": "retry"}
            )
            assert status == 400

            good = {"objective": "Look around", "workspace": str(root), "writes": "off"}
            status, payload = await _request(
                host, port, "POST", "/v1/runs", body=good, headers={"Idempotency-Key": "retry"}
            )

            assert status == 201
            assert json.loads(payload)["run_id"]
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_an_internal_failure_does_not_name_a_python_exception(tmp_path: Path) -> None:
    """A caller learns that Athena failed, not how Athena is built.

    `KeyError` on the wire tells someone about the inside of the process and tells them
    nothing they can act on.
    """
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        registry = _registry(tmp_path, _ScriptedProvider([]))
        service = AthenaService(registry, _config())

        async def explode(request: object) -> None:
            del request
            raise KeyError("an internal detail nobody outside should read")

        service._route = explode  # type: ignore[assignment, method-assign]
        host, port = await service.start()
        try:
            status, payload = await _request(
                host, port, "POST", "/v1/runs", body={"objective": "x", "workspace": str(root)}
            )

            assert status == 500
            assert "KeyError" not in payload
            assert "internal detail" not in payload
            assert json.loads(payload)["error"]["code"] == "internal_error"
        finally:
            await service.stop()

    asyncio.run(scenario())
