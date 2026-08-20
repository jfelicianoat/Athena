"""The Telegram transport, driven by simulated updates.

No network and no bot library: a fake `TelegramApi` hands out the JSON documents Telegram
would send, and records what would have been sent back. What is being tested is that the
adapter stays transport — that it refuses the wrong people before the runtime hears them,
survives the three kinds of nonsense a real bot receives, and never becomes a second place
where agent decisions are made.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from athena.adapters.channel_gateway import ChannelAccessPolicy, ChannelGateway, ChannelGrant
from athena.adapters.service.runs import CapabilityMode, RunOptions, RunRegistry
from athena.cancellation import CancellationToken
from athena.channels import ChannelIdentity, ChannelResponse, ResponseKind
from athena.errors import ToolValidationError
from athena.events import EventName, InMemoryEventBus, ModelEvent, RuntimeEvent
from athena.models import (
    ModelCapabilities,
    ModelHealth,
    ModelHealthStatus,
    ModelProvider,
    ModelRequest,
    ModelResponse,
)
from athena.session_store import SqliteSessionStore
from athena.stores import SqliteToolResultStore
from athena.types import JSONValue
from athena_telegram import (
    TelegramAdapter,
    TelegramApi,
    TelegramConfigError,
    TelegramSecurity,
    TelegramTransportError,
    parse_allowlist,
    parse_update,
    resolve_token,
)

ALLOWED_ID = 4_242
STRANGER_ID = 9_999
CHAT_ID = 100_100


def _update(
    update_id: int,
    text: str,
    *,
    user_id: int = ALLOWED_ID,
    chat_id: int = CHAT_ID,
    chat_type: str = "private",
) -> dict[str, JSONValue]:
    """One `getUpdates` entry, shaped the way Telegram actually shapes it."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id * 10,
            "from": {"id": user_id, "is_bot": False, "username": "someone"},
            "chat": {"id": chat_id, "type": chat_type},
            "date": 1_770_000_000,
            "text": text,
        },
    }


class _FakeApi(TelegramApi):
    """Hands out scripted update batches and records what was sent."""

    def __init__(
        self,
        batches: Sequence[Sequence[JSONValue]] = (),
        *,
        fail_with: Exception | None = None,
    ) -> None:
        super().__init__("1234:fake-token")
        self._batches: list[list[JSONValue]] = [list(batch) for batch in batches]
        self._fail_with = fail_with
        self.sent: list[tuple[int, str]] = []
        self.polls = 0

    async def get_updates(self, offset: int | None) -> list[JSONValue]:
        del offset
        self.polls += 1
        if self._fail_with is not None:
            raise self._fail_with
        if not self._batches:
            return []
        return self._batches.pop(0)

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))

    def texts(self) -> list[str]:
        return [text for _, text in self.sent]


class _IdleProvider(ModelProvider):
    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        del request
        cancellation.raise_if_cancelled()
        return ModelResponse("Nothing to do.", "scripted", "stop")

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


def _adapter(api: TelegramApi, *, allowed: Sequence[int] = (ALLOWED_ID,)) -> TelegramAdapter:
    return TelegramAdapter(api, TelegramSecurity.open_to(allowed), idle_backoff_seconds=0.0)


def _identity(user_id: int = ALLOWED_ID) -> ChannelIdentity:
    return ChannelIdentity("telegram", str(user_id), str(CHAT_ID))


async def _drain(adapter: TelegramAdapter, count: int) -> list[str]:
    """Read `count` messages, then stop the adapter so nothing is left polling."""
    await adapter.start()
    texts: list[str] = []
    for _ in range(count):
        message = await adapter.receive()
        if message is None:
            break
        texts.append(message.text)
    await adapter.stop()
    return texts


# --- parsing what Telegram sends -------------------------------------------------------


def test_a_normal_message_becomes_an_update() -> None:
    update = parse_update(_update(1, "/status"))

    assert update is not None
    assert update.update_id == 1
    assert update.user_id == ALLOWED_ID
    assert update.chat_id == CHAT_ID
    assert update.is_private


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "not a document",
        {},
        {"update_id": 1},
        {"update_id": 1, "message": {}},
        {"update_id": "one", "message": {"chat": {"id": 1, "type": "private"}}},
        {"update_id": 1, "edited_message": {"text": "hi"}},
        {"update_id": 1, "message": {"chat": {"id": 1, "type": "private"}, "text": "hi"}},
        {"update_id": 1, "message": {"from": {"id": 1}, "chat": {"type": "private"}, "text": "x"}},
        {"update_id": 1, "message": {"from": {"id": 1}, "chat": {"id": 1}, "sticker": {}}},
    ],
    ids=[
        "none",
        "not-an-object",
        "empty",
        "no-message",
        "message-without-anything",
        "non-numeric-update-id",
        "an-edit-not-a-message",
        "no-sender",
        "chat-without-id",
        "a-sticker",
    ],
)
def test_anything_that_is_not_an_actionable_message_is_ignored(raw: object) -> None:
    # None of these is an error. A bot receives edits, joins, stickers and whatever the API
    # adds next; raising on them would make ordinary traffic look like a fault.
    assert parse_update(raw) is None


def test_a_malformed_update_does_not_stop_the_ones_around_it(tmp_path: Path) -> None:
    del tmp_path

    async def scenario() -> None:
        api = _FakeApi([[{"update_id": 7, "message": {"nonsense": True}}, _update(8, "/status")]])
        adapter = _adapter(api)

        texts = await _drain(adapter, 1)

        assert texts == ["/status"]
        assert adapter.dropped_malformed == 1

    asyncio.run(scenario())


# --- who gets through ------------------------------------------------------------------


def test_an_unauthorized_user_never_reaches_the_runtime() -> None:
    """Refused by the transport, which is the cheapest place to say no.

    The runtime is not asked, so an account that is not on the list cannot cost the host a
    single decision — not even the decision to refuse it.
    """

    async def scenario() -> None:
        api = _FakeApi([[_update(1, "/new break everything", user_id=STRANGER_ID)]])
        adapter = _adapter(api)
        await adapter.start()
        task = asyncio.ensure_future(adapter.receive())
        await asyncio.sleep(0.01)
        await adapter.stop()
        assert await task is None

        assert adapter.refused_identities == 1
        assert api.texts() == [
            "This account is not authorised to use Athena. If that is wrong, whoever runs "
            "this bot has to add your Telegram id."
        ]

    asyncio.run(scenario())


def test_deny_by_default_when_private_mode_is_on() -> None:
    security = TelegramSecurity(allowed_user_ids=frozenset({ALLOWED_ID}))

    assert security.permits(ALLOWED_ID)
    assert not security.permits(STRANGER_ID)


def test_an_empty_allowlist_with_private_mode_refuses_to_start() -> None:
    # It would refuse everyone, which is safe but silently useless. Better to say so at
    # startup than to leave someone wondering why the bot ignores them.
    with pytest.raises(TelegramConfigError):
        TelegramSecurity().validate()


def test_turning_private_mode_off_is_a_decision_not_an_accident() -> None:
    security = TelegramSecurity(private_mode=False)
    security.validate()

    assert security.permits(STRANGER_ID)


def test_identity_is_the_numeric_id_not_the_username() -> None:
    # A username can be changed and reused, so an allow-list keyed by one decays into a
    # list of whoever holds those names today.
    async def scenario() -> None:
        api = _FakeApi([[_update(1, "/status")]])
        adapter = _adapter(api)
        await adapter.start()
        message = await adapter.receive()
        await adapter.stop()

        assert message is not None
        assert message.identity.key == f"telegram:{ALLOWED_ID}"
        assert message.identity.display_name == "someone"

    asyncio.run(scenario())


def test_a_group_chat_is_refused_and_not_nagged() -> None:
    # Plain text is an objective in this channel, so a room with bystanders is exactly
    # where that must not apply. Replying once and then going quiet is the polite version.
    async def scenario() -> None:
        api = _FakeApi(
            [[_update(1, "hello", chat_type="group"), _update(2, "still here", chat_type="group")]]
        )
        adapter = _adapter(api)
        await adapter.start()
        task = asyncio.ensure_future(adapter.receive())
        await asyncio.sleep(0.01)
        await adapter.stop()
        await task

        assert len(api.sent) == 1
        assert "direct conversation" in api.texts()[0]

    asyncio.run(scenario())


# --- duplicates ------------------------------------------------------------------------


def test_a_duplicate_update_is_acted_on_once() -> None:
    """A replayed batch must not start a second run.

    Telegram's offset usually prevents this, but a crash between receiving a batch and
    committing the offset replays it — and an update acted on twice is a run started twice.
    """

    async def scenario() -> None:
        api = _FakeApi([[_update(5, "/new fix it")], [_update(5, "/new fix it")]])
        adapter = _adapter(api)
        await adapter.start()
        first = await adapter.receive()
        task = asyncio.ensure_future(adapter.receive())
        await asyncio.sleep(0.01)
        await adapter.stop()
        await task

        assert first is not None
        assert first.text == "/new fix it"
        assert adapter.dropped_duplicates == 1

    asyncio.run(scenario())


# --- Telegram itself being unavailable -------------------------------------------------


def test_an_unreachable_telegram_backs_off_instead_of_spinning() -> None:
    async def scenario() -> None:
        api = _FakeApi(fail_with=TelegramTransportError("no route"))
        adapter = TelegramAdapter(
            api, TelegramSecurity.open_to([ALLOWED_ID]), idle_backoff_seconds=0.02
        )
        await adapter.start()
        task = asyncio.ensure_future(adapter.receive())
        await asyncio.sleep(0.05)
        await adapter.stop()
        await task

        assert api.polls >= 1
        assert api.polls < 20, "a failing poll must not become a tight loop"

    asyncio.run(scenario())


def test_a_failed_send_is_an_ordinary_event() -> None:
    class _RefusingApi(_FakeApi):
        async def send_message(self, chat_id: int, text: str) -> None:
            del chat_id, text
            raise TelegramTransportError("the send failed")

    async def scenario() -> None:
        adapter = _adapter(_RefusingApi())
        await adapter.start()
        # No raise: a channel that is momentarily down must not take the runtime with it.
        await adapter.deliver(ChannelResponse(_identity(), "anything", ResponseKind.RESULT))
        await adapter.stop()

    asyncio.run(scenario())


# --- outbound behaviour ----------------------------------------------------------------


def test_progress_is_coalesced_but_conclusions_are_not() -> None:
    """Telegram allows about one message a second to a chat.

    A run produces bursts, so the newest state is worth more than a faithful replay of how
    it got there. What is never dropped is anything that ends a run: those are the messages
    the person is waiting for.
    """

    async def scenario() -> None:
        api = _FakeApi()
        adapter = _adapter(api)
        await adapter.start()
        identity = _identity()
        for text in ("Athena has started.", "Checking that the changes hold up."):
            await adapter.deliver(ChannelResponse(identity, text, ResponseKind.PROGRESS))
        await adapter.deliver(
            ChannelResponse(identity, "Athena stopped: it broke", ResponseKind.ERROR)
        )
        await adapter.stop()

        assert adapter.coalesced_progress == 1
        assert api.texts()[-1] == "Athena stopped: it broke"

    asyncio.run(scenario())


def test_the_same_progress_line_twice_says_nothing_new() -> None:
    async def scenario() -> None:
        api = _FakeApi()
        adapter = _adapter(api)
        await adapter.start()
        identity = _identity()
        for _ in range(3):
            await adapter.deliver(
                ChannelResponse(identity, "Athena has started.", ResponseKind.PROGRESS)
            )
        await adapter.stop()

        assert len(api.sent) == 1

    asyncio.run(scenario())


def test_an_over_long_message_is_trimmed_rather_than_rejected() -> None:
    # Telegram refuses anything past 4096 characters, and a refused message is a message
    # the person never sees.
    async def scenario() -> None:
        api = _FakeApi()
        adapter = _adapter(api)
        await adapter.start()
        await adapter.deliver(ChannelResponse(_identity(), "x" * 5_000, ResponseKind.RESULT))
        await adapter.stop()

        assert len(api.texts()[0]) == 4_096
        assert api.texts()[0].endswith("…")

    asyncio.run(scenario())


# --- configuration ---------------------------------------------------------------------


def test_the_token_comes_from_a_file_before_the_environment(tmp_path: Path) -> None:
    # An environment variable is visible to every child process, and this agent starts
    # processes for a living.
    secret = tmp_path / "token"
    secret.write_text("  file-token  \n", encoding="utf-8")

    resolved = resolve_token(
        environment={
            "ATHENA_TELEGRAM_TOKEN_FILE": str(secret),
            "ATHENA_TELEGRAM_TOKEN": "env-token",
        }
    )

    assert resolved == "file-token"


def test_a_missing_token_is_refused_at_startup() -> None:
    with pytest.raises(TelegramConfigError):
        resolve_token(environment={})


def test_a_typo_in_the_allowlist_is_an_error_not_a_skipped_entry() -> None:
    # An allow-list that silently shrinks is a security hole that looks like it works.
    with pytest.raises(TelegramConfigError):
        parse_allowlist("4242, not-a-number")

    assert parse_allowlist(" 1, 2 ,3 ") == frozenset({1, 2, 3})


# --- through the gateway, end to end ---------------------------------------------------


def _registry(tmp_path: Path, bus: InMemoryEventBus) -> RunRegistry:
    return RunRegistry(
        _IdleProvider(),
        bus,
        SqliteSessionStore(tmp_path / "sessions.db"),
        SqliteToolResultStore(tmp_path / "results.db"),
    )


def _policy(root: Path) -> ChannelAccessPolicy:
    policy = ChannelAccessPolicy()
    policy.grant(
        f"telegram:{ALLOWED_ID}",
        ChannelGrant(root, RunOptions(writes=CapabilityMode.OFF, execution=CapabilityMode.OFF)),
    )
    return policy


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "notes.md").write_text("nothing to see\n", encoding="utf-8")
    return root


def _gateway(
    adapter: TelegramAdapter, registry: RunRegistry, root: Path, bus: InMemoryEventBus
) -> ChannelGateway:
    return ChannelGateway(adapter, registry, _policy(root), bus, bare_text_starts_run=True)


def test_plain_text_starts_a_run_in_a_private_chat(tmp_path: Path) -> None:
    """The ambiguity that made plain text unsafe is gone here.

    One-to-one, allow-listed, nobody else in the room. That is what a channel has to remove
    before `bare_text_starts_run` is anything but reckless.
    """

    async def scenario() -> None:
        bus = InMemoryEventBus()
        registry = _registry(tmp_path, bus)
        api = _FakeApi([[_update(1, "have a look around")]])
        adapter = _adapter(api)
        gateway = _gateway(adapter, registry, _workspace(tmp_path), bus)
        try:
            await gateway.start()
            message = await adapter.receive()
            assert message is not None
            from athena.channels import parse_command

            await gateway.handle(parse_command(message, bare_text_starts_run=True))
            run_id = gateway.run_for(message.identity)
            assert run_id is not None
            await registry.wait(run_id)
        finally:
            await gateway.stop()
            await registry.shutdown()

        assert any("Started" in text for text in api.texts())

    asyncio.run(scenario())


def test_the_five_commands_reach_the_runtime(tmp_path: Path) -> None:
    async def scenario() -> None:
        bus = InMemoryEventBus()
        registry = _registry(tmp_path, bus)
        api = _FakeApi(
            [
                [
                    _update(1, "/start"),
                    _update(2, "/new"),
                    _update(3, "/runs"),
                    _update(4, "/status"),
                    _update(5, "/cancel"),
                ]
            ]
        )
        adapter = _adapter(api)
        gateway = _gateway(adapter, registry, _workspace(tmp_path), bus)
        try:
            await gateway.start()
            await adapter.start()
            for _ in range(5):
                message = await adapter.receive()
                assert message is not None
                from athena.channels import parse_command

                await gateway.handle(parse_command(message, bare_text_starts_run=True))
        finally:
            await adapter.stop()
            await gateway.stop()
            await registry.shutdown()

        texts = api.texts()
        assert any("/new <objective>" in text for text in texts), "/start explains itself"
        assert any("Tell me what to do" in text for text in texts), "/new invites an objective"
        assert any("no runs yet" in text for text in texts), "/runs is honest when empty"
        assert any("no run to cancel" in text for text in texts), "/cancel has nothing to stop"

    asyncio.run(scenario())


def test_cancel_stops_the_run_it_started(tmp_path: Path) -> None:
    async def scenario() -> None:
        bus = InMemoryEventBus()
        registry = _registry(tmp_path, bus)
        api = _FakeApi()
        adapter = _adapter(api)
        gateway = _gateway(adapter, registry, _workspace(tmp_path), bus)
        identity = _identity()
        try:
            await gateway.start()
            from athena.channels import ChannelCommand, CommandName

            await gateway.handle(
                ChannelCommand(CommandName.START_RUN, identity, objective="look around")
            )
            run_id = gateway.run_for(identity)
            assert run_id is not None
            await gateway.handle(ChannelCommand(CommandName.CANCEL_RUN, identity))
            await registry.wait(run_id)
        finally:
            await gateway.stop()
            await registry.shutdown()

        assert any("Cancelling" in text for text in api.texts())

    asyncio.run(scenario())


def test_athena_being_unavailable_is_reported_not_crashed(tmp_path: Path) -> None:
    """A runtime that cannot start a run is not a reason for the bot to fall over.

    The person gets told; the transport keeps listening. Anything else means one bad run
    takes the channel down with it.
    """

    class _BrokenRegistry(RunRegistry):
        async def start(self, objective: str, workspace: Any, options: Any = None) -> str:
            del objective, workspace, options
            raise ToolValidationError("Athena is not available right now")

    async def scenario() -> None:
        bus = InMemoryEventBus()
        registry = _BrokenRegistry(
            _IdleProvider(),
            bus,
            SqliteSessionStore(tmp_path / "sessions.db"),
            SqliteToolResultStore(tmp_path / "results.db"),
        )
        api = _FakeApi()
        adapter = _adapter(api)
        gateway = _gateway(adapter, registry, _workspace(tmp_path), bus)
        from athena.channels import ChannelCommand, CommandName

        try:
            await gateway.start()
            await gateway.handle(
                ChannelCommand(CommandName.START_RUN, _identity(), objective="anything")
            )
            # Still serving afterwards: the next command is answered normally.
            await gateway.handle(ChannelCommand(CommandName.HELP, _identity()))
        finally:
            await gateway.stop()
            await registry.shutdown()

        assert "Athena is not available right now" in api.texts()[0]
        assert "/new <objective>" in api.texts()[1]

    asyncio.run(scenario())


def test_a_run_event_reaches_the_chat_that_asked_for_it(tmp_path: Path) -> None:
    async def scenario() -> None:
        bus = InMemoryEventBus()
        registry = _registry(tmp_path, bus)
        api = _FakeApi([[_update(1, "/status")]])
        adapter = _adapter(api)
        gateway = _gateway(adapter, registry, _workspace(tmp_path), bus)
        from athena.channels import ChannelCommand, CommandName

        try:
            await gateway.start()
            await adapter.start()
            await adapter.receive()  # teaches the adapter which chat this identity is in
            await gateway.handle(
                ChannelCommand(CommandName.START_RUN, _identity(), objective="look around")
            )
            run_id = gateway.run_for(_identity())
            assert run_id is not None
            await registry.wait(run_id)
            await bus.publish(
                RuntimeEvent(
                    EventName.AGENT_COMPLETED, run_id, {"verification": "All checks pass."}
                )
            )
        finally:
            await adapter.stop()
            await gateway.stop()
            await registry.shutdown()

        assert all(chat_id == CHAT_ID for chat_id, _ in api.sent)
        assert any("All checks pass." in text for text in api.texts())

    asyncio.run(scenario())


def test_the_adapter_owns_no_agent_logic() -> None:
    """Transport only, checked by reading the file.

    A comment saying so would rot. The imports are the claim: no model provider, no tool,
    no agent loop, no permission engine.
    """
    import ast

    import athena_telegram

    forbidden = {
        "athena.agent_loop",
        "athena.models",
        "athena.tools",
        "athena.tool_executor",
        "athena.permissions",
        "athena.verification",
    }
    package = Path(athena_telegram.__file__).parent
    imported: set[str] = set()
    for module in sorted(package.rglob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)

    assert imported & forbidden == set()
    assert "athena.channels" in imported
