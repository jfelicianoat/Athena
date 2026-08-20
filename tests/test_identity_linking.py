"""Linking one person's accounts, and refusing every shortcut to it.

The interesting tests here are the negative ones. A link is a claim that two accounts are
the same human, and the only acceptable evidence is a secret Athena issued and the person
carried across. Everything else — a matching name, a second attempt, a stale code, a
patient guesser — has to fail, and fail without teaching the guesser anything.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import timedelta
from pathlib import Path

import pytest

from athena.adapters.channel_gateway import ChannelAccessPolicy, ChannelGateway, ChannelGrant
from athena.adapters.service.runs import CapabilityMode, RunOptions, RunRegistry
from athena.cancellation import CancellationToken
from athena.channels import ChannelCommand, ChannelIdentity, CommandName, ResponseKind
from athena.events import EventName, InMemoryEventBus, ModelEvent
from athena.identity import (
    IdentityError,
    LinkOutcome,
    SqliteIdentityDirectory,
    UserIdentity,
    generate_code,
    hash_code,
)
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
from athena.testing import FakeChannelAdapter

TELEGRAM_A = ChannelIdentity("telegram", "1001", "chat-1", "alice")
TELEGRAM_B = ChannelIdentity("telegram", "2002", "chat-2", "bob")
DESKTOP_A = ChannelIdentity("chatygpt", "desktop-1", "window-1", "alice")


def _directory(tmp_path: Path, **kwargs: object) -> SqliteIdentityDirectory:
    return SqliteIdentityDirectory(tmp_path / "identity.db", **kwargs)  # type: ignore[arg-type]


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


# --- the code itself -------------------------------------------------------------------


def test_codes_are_unpredictable_and_typable() -> None:
    codes = {generate_code() for _ in range(500)}

    assert len(codes) == 500, "500 codes should not collide at ~59 bits"
    alphabet = set("23456789ABCDEFGHJKMNPQRSTVWXYZ")
    assert all(set(code) <= alphabet for code in codes)
    # No character a person could misread as another when retyping it from a chat.
    assert not alphabet & set("ILOU01")


def test_the_plaintext_code_is_never_stored(tmp_path: Path) -> None:
    """A copy of the database must not be a set of working link codes."""

    async def scenario() -> None:
        directory = _directory(tmp_path)
        user = await directory.create_user("Alice")
        token = await directory.issue_link_token(user.user_id)

        # Every file SQLite writes, not just the main one: in WAL mode a fresh row lives
        # in the sidecar until a checkpoint, and checking only `identity.db` would pass
        # for the wrong reason — the code would be absent because nothing was there yet.
        raw = b"".join(path.read_bytes() for path in sorted(tmp_path.glob("identity.db*")))

        assert token.code.encode() not in raw
        assert hash_code(token.code).encode() in raw, "the digest is what is persisted"

    asyncio.run(scenario())


def test_a_code_survives_the_way_a_chat_mangles_it(tmp_path: Path) -> None:
    # People retype these. Spaces, dashes and lowercase are transcription noise, not a
    # different code — but nothing about the code itself is forgiven.
    async def scenario() -> None:
        directory = _directory(tmp_path)
        user = await directory.create_user()
        token = await directory.issue_link_token(user.user_id)
        mangled = f" {token.code[:4].lower()}-{token.code[4:8]} {token.code[8:]} "

        result = await directory.redeem(mangled, TELEGRAM_A)

        assert result.outcome is LinkOutcome.LINKED

    asyncio.run(scenario())


# --- the happy path --------------------------------------------------------------------


def test_a_redeemed_code_makes_two_accounts_one_person(tmp_path: Path) -> None:
    async def scenario() -> None:
        directory = _directory(tmp_path)
        user = await directory.create_user("Alice")
        await directory.redeem((await directory.issue_link_token(user.user_id)).code, DESKTOP_A)
        token = await directory.issue_link_token(user.user_id)

        result = await directory.redeem(token.code, TELEGRAM_A)

        assert result.outcome is LinkOutcome.LINKED
        assert result.user_id == user.user_id
        resolved = await directory.resolve(TELEGRAM_A)
        assert resolved is not None
        assert resolved.user_id == user.user_id
        links = await directory.links_for(user.user_id)
        assert {link.channel for link in links} == {"telegram", "chatygpt"}

    asyncio.run(scenario())


# --- the six refusals ------------------------------------------------------------------


def test_an_expired_code_is_refused(tmp_path: Path) -> None:
    async def scenario() -> None:
        directory = _directory(tmp_path)
        user = await directory.create_user()
        token = await directory.issue_link_token(user.user_id, ttl=timedelta(seconds=-1))

        result = await directory.redeem(token.code, TELEGRAM_A)

        assert result.outcome is LinkOutcome.EXPIRED
        assert await directory.resolve(TELEGRAM_A) is None

    asyncio.run(scenario())


def test_a_code_works_once_and_only_once(tmp_path: Path) -> None:
    """The second use is refused even by the same person from a different account."""

    async def scenario() -> None:
        directory = _directory(tmp_path)
        user = await directory.create_user()
        token = await directory.issue_link_token(user.user_id)

        first = await directory.redeem(token.code, TELEGRAM_A)
        second = await directory.redeem(token.code, TELEGRAM_B)

        assert first.outcome is LinkOutcome.LINKED
        assert second.outcome is LinkOutcome.ALREADY_USED
        assert await directory.resolve(TELEGRAM_B) is None

    asyncio.run(scenario())


def test_two_redemptions_racing_each_other_cannot_both_win(tmp_path: Path) -> None:
    # "One use" is a claim about concurrency, not about calling order. Reading the token
    # and then marking it used in a second statement would leave a window where both saw
    # it unused.
    async def scenario() -> None:
        directory = _directory(tmp_path)
        user = await directory.create_user()
        token = await directory.issue_link_token(user.user_id)

        outcomes = await asyncio.gather(
            directory.redeem(token.code, TELEGRAM_A),
            directory.redeem(token.code, TELEGRAM_B),
        )

        linked = [result for result in outcomes if result.linked]
        assert len(linked) == 1

    asyncio.run(scenario())


def test_an_invalid_code_is_refused(tmp_path: Path) -> None:
    async def scenario() -> None:
        directory = _directory(tmp_path)
        await directory.create_user()

        result = await directory.redeem("ZZZZZZZZZZZZ", TELEGRAM_A)

        assert result.outcome is LinkOutcome.UNKNOWN_CODE
        assert await directory.resolve(TELEGRAM_A) is None

    asyncio.run(scenario())


def test_guessing_is_stopped_before_the_code_space_matters(tmp_path: Path) -> None:
    """Brute force protection, which is what makes a short code acceptable.

    Fifty-nine bits does not need a rate limit to be safe. A rate limit is what lets the
    code be short enough that a person will actually retype it.
    """

    async def scenario() -> None:
        directory = _directory(tmp_path, max_attempts=3)
        user = await directory.create_user()
        real = await directory.issue_link_token(user.user_id)

        for _ in range(3):
            assert (await directory.redeem("AAAAAAAAAAAA", TELEGRAM_A)).outcome is (
                LinkOutcome.UNKNOWN_CODE
            )

        blocked = await directory.redeem("BBBBBBBBBBBB", TELEGRAM_A)
        # And the real code is refused too: once an account is guessing, it is guessing,
        # and stumbling onto the answer at attempt four must not be rewarded.
        with_real_code = await directory.redeem(real.code, TELEGRAM_A)

        assert blocked.outcome is LinkOutcome.RATE_LIMITED
        assert with_real_code.outcome is LinkOutcome.RATE_LIMITED
        assert await directory.resolve(TELEGRAM_A) is None

    asyncio.run(scenario())


def test_the_limit_is_per_account_not_global(tmp_path: Path) -> None:
    # A global counter would let one guesser lock everybody else out, which turns a
    # brute-force defence into a denial of service.
    async def scenario() -> None:
        directory = _directory(tmp_path, max_attempts=2)
        user = await directory.create_user()
        token = await directory.issue_link_token(user.user_id)
        for _ in range(3):
            await directory.redeem("AAAAAAAAAAAA", TELEGRAM_B)

        result = await directory.redeem(token.code, TELEGRAM_A)

        assert result.outcome is LinkOutcome.LINKED

    asyncio.run(scenario())


def test_a_stale_code_still_counts_as_an_attempt(tmp_path: Path) -> None:
    # Only counting "wrong" guesses would let an attacker probe for free by supplying
    # something that fails an earlier check.
    async def scenario() -> None:
        directory = _directory(tmp_path, max_attempts=2)
        user = await directory.create_user()
        for _ in range(2):
            stale = await directory.issue_link_token(user.user_id, ttl=timedelta(seconds=-1))
            await directory.redeem(stale.code, TELEGRAM_A)

        good = await directory.issue_link_token(user.user_id)
        result = await directory.redeem(good.code, TELEGRAM_A)

        assert result.outcome is LinkOutcome.RATE_LIMITED

    asyncio.run(scenario())


def test_user_a_cannot_redeem_user_b_token_onto_a_taken_account(tmp_path: Path) -> None:
    """Rebinding a linked account is the shape an account takeover has.

    Even holding a perfectly valid code for someone else does not move an account that
    already belongs to a person. There is a deliberate way to do it, and it starts with
    unlinking.
    """

    async def scenario() -> None:
        directory = _directory(tmp_path)
        alice = await directory.create_user("Alice")
        bob = await directory.create_user("Bob")
        await directory.redeem((await directory.issue_link_token(alice.user_id)).code, TELEGRAM_A)
        bobs_token = await directory.issue_link_token(bob.user_id)

        result = await directory.redeem(bobs_token.code, TELEGRAM_A)

        assert result.outcome is LinkOutcome.ALREADY_LINKED
        still_alice = await directory.resolve(TELEGRAM_A)
        assert still_alice is not None
        assert still_alice.user_id == alice.user_id

    asyncio.run(scenario())


def test_bobs_token_is_not_burned_by_alices_failed_attempt(tmp_path: Path) -> None:
    # The refusal happened before the token was touched. Otherwise anyone could destroy
    # someone else's code by trying it somewhere it could not apply.
    async def scenario() -> None:
        directory = _directory(tmp_path)
        alice = await directory.create_user("Alice")
        bob = await directory.create_user("Bob")
        await directory.redeem((await directory.issue_link_token(alice.user_id)).code, TELEGRAM_A)
        bobs_token = await directory.issue_link_token(bob.user_id)
        await directory.redeem(bobs_token.code, TELEGRAM_A)

        result = await directory.redeem(bobs_token.code, TELEGRAM_B)

        assert result.outcome is LinkOutcome.LINKED
        assert result.user_id == bob.user_id

    asyncio.run(scenario())


def test_a_failure_never_says_which_failure_it_was(tmp_path: Path) -> None:
    """Told apart, unknown/expired/used answer "did this code ever exist" for a guesser."""

    async def scenario() -> None:
        directory = _directory(tmp_path)
        user = await directory.create_user()
        used = await directory.issue_link_token(user.user_id)
        await directory.redeem(used.code, TELEGRAM_B)
        stale = await directory.issue_link_token(user.user_id, ttl=timedelta(seconds=-1))

        messages = {
            (await directory.redeem("ZZZZZZZZZZZZ", TELEGRAM_A)).message,
            (await directory.redeem(used.code, TELEGRAM_A)).message,
            (await directory.redeem(stale.code, TELEGRAM_A)).message,
        }

        assert len(messages) == 1

    asyncio.run(scenario())


def test_names_are_never_evidence(tmp_path: Path) -> None:
    """The rule the whole module exists for.

    A Telegram username can be released and re-registered by someone else within days, so
    matching on one is matching on whoever holds a string today.
    """

    async def scenario() -> None:
        directory = _directory(tmp_path)
        alice = await directory.create_user("alice")
        await directory.redeem((await directory.issue_link_token(alice.user_id)).code, DESKTOP_A)
        impostor = ChannelIdentity("telegram", "6666", "chat-6", "alice")

        assert await directory.resolve(impostor) is None
        # And the display name recorded on the user changes nothing either.
        assert await directory.resolve(ChannelIdentity("telegram", "7777", "c", "Alice")) is None

    asyncio.run(scenario())


def test_a_token_for_an_unknown_user_is_refused(tmp_path: Path) -> None:
    async def scenario() -> None:
        directory = _directory(tmp_path)
        with pytest.raises(IdentityError):
            await directory.issue_link_token("nobody")

    asyncio.run(scenario())


# --- unlinking -------------------------------------------------------------------------


def test_unlinking_returns_an_account_to_itself(tmp_path: Path) -> None:
    async def scenario() -> None:
        directory = _directory(tmp_path)
        user = await directory.create_user()
        await directory.redeem((await directory.issue_link_token(user.user_id)).code, TELEGRAM_A)

        removed = await directory.unlink(TELEGRAM_A)

        assert removed
        assert await directory.resolve(TELEGRAM_A) is None
        assert await directory.links_for(user.user_id) == ()

    asyncio.run(scenario())


def test_unlinking_something_that_was_not_linked_is_not_an_error(tmp_path: Path) -> None:
    async def scenario() -> None:
        directory = _directory(tmp_path)

        assert await directory.unlink(TELEGRAM_A) is False

    asyncio.run(scenario())


def test_an_unlinked_account_can_be_linked_again(tmp_path: Path) -> None:
    # Unlink is the deliberate route that `ALREADY_LINKED` points at, so it has to work.
    async def scenario() -> None:
        directory = _directory(tmp_path)
        alice = await directory.create_user("Alice")
        bob = await directory.create_user("Bob")
        await directory.redeem((await directory.issue_link_token(alice.user_id)).code, TELEGRAM_A)
        await directory.unlink(TELEGRAM_A)

        result = await directory.redeem(
            (await directory.issue_link_token(bob.user_id)).code, TELEGRAM_A
        )

        assert result.outcome is LinkOutcome.LINKED
        assert result.user_id == bob.user_id

    asyncio.run(scenario())


# --- audit -----------------------------------------------------------------------------


def test_every_decision_is_audited_and_no_code_appears_in_it(tmp_path: Path) -> None:
    async def scenario() -> None:
        directory = _directory(tmp_path)
        user = await directory.create_user("Alice")
        token = await directory.issue_link_token(user.user_id)
        await directory.redeem("ZZZZZZZZZZZZ", TELEGRAM_A)
        await directory.redeem(token.code, TELEGRAM_A)
        await directory.unlink(TELEGRAM_A)

        entries = await directory.audit()
        actions = [(entry.action, entry.outcome) for entry in entries]

        assert ("user.created", "ok") in actions
        assert ("token.issued", "ok") in actions
        assert ("token.redeemed", "unknown_code") in actions
        assert ("token.redeemed", "linked") in actions
        assert ("link.removed", "ok") in actions
        # The audit is a record, not a credential.
        assert all(token.code not in str(entry) for entry in entries)

    asyncio.run(scenario())


# --- what it is all for ----------------------------------------------------------------


def _registry(tmp_path: Path, bus: InMemoryEventBus) -> RunRegistry:
    return RunRegistry(
        _IdleProvider(),
        bus,
        SqliteSessionStore(tmp_path / "sessions.db"),
        SqliteToolResultStore(tmp_path / "results.db"),
    )


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    (root / "notes.md").write_text("nothing to see\n", encoding="utf-8")
    return root


def _policy(root: Path, owners: Sequence[str]) -> ChannelAccessPolicy:
    policy = ChannelAccessPolicy()
    for owner in owners:
        policy.grant(
            owner,
            ChannelGrant(root, RunOptions(writes=CapabilityMode.OFF, execution=CapabilityMode.OFF)),
        )
    return policy


def test_one_person_two_surfaces_one_run(tmp_path: Path) -> None:
    """The goal, stated as a test.

    A run started from one linked account is the same run the other can see and cancel —
    not a second run that happens to look similar.
    """

    async def scenario() -> None:
        bus = InMemoryEventBus()
        directory = _directory(tmp_path)
        user = await directory.create_user("Alice")
        for identity in (DESKTOP_A, TELEGRAM_A):
            await directory.redeem((await directory.issue_link_token(user.user_id)).code, identity)

        registry = _registry(tmp_path, bus)
        adapter = FakeChannelAdapter()
        gateway = ChannelGateway(
            adapter,
            registry,
            _policy(_workspace(tmp_path), [user.user_id]),
            bus,
            directory=directory,
        )
        try:
            await gateway.start()
            await gateway.handle(
                ChannelCommand(CommandName.START_RUN, DESKTOP_A, objective="look around")
            )
            run_id = await gateway.run_for(DESKTOP_A)
            assert run_id is not None

            # The other surface sees the same run, and starting a second one is refused.
            assert await gateway.run_for(TELEGRAM_A) == run_id
            await gateway.handle(
                ChannelCommand(CommandName.START_RUN, TELEGRAM_A, objective="and again")
            )
            await gateway.handle(ChannelCommand(CommandName.LIST_RUNS, TELEGRAM_A))
            await registry.wait(run_id)
        finally:
            await gateway.stop()
            await registry.shutdown()

        texts = adapter.texts()
        assert any("already going" in text for text in texts), "no duplicate AgentRun"
        assert any(run_id in text for text in texts), "the other surface lists it"

    asyncio.run(scenario())


def test_two_unlinked_accounts_stay_separate(tmp_path: Path) -> None:
    # The fallback is per-account ownership, which keeps an unlinked person working on
    # their own rather than sharing with every other stranger.
    async def scenario() -> None:
        bus = InMemoryEventBus()
        directory = _directory(tmp_path)
        registry = _registry(tmp_path, bus)
        adapter = FakeChannelAdapter()
        gateway = ChannelGateway(
            adapter,
            registry,
            _policy(_workspace(tmp_path), [TELEGRAM_A.key, TELEGRAM_B.key]),
            bus,
            directory=directory,
        )
        try:
            await gateway.start()
            await gateway.handle(
                ChannelCommand(CommandName.START_RUN, TELEGRAM_A, objective="mine")
            )
            run_id = await gateway.run_for(TELEGRAM_A)
            assert run_id is not None

            assert await gateway.run_for(TELEGRAM_B) is None
            await gateway.handle(ChannelCommand(CommandName.CANCEL_RUN, TELEGRAM_B, run_id=run_id))
            await registry.wait(run_id)
        finally:
            await gateway.stop()
            await registry.shutdown()

        assert "You have no run to cancel." in adapter.texts()

    asyncio.run(scenario())


def test_linking_over_a_channel_needs_no_grant(tmp_path: Path) -> None:
    """Someone with a code has not been granted anything yet.

    Refusing them for lack of a grant would make the code unusable, which is the one way
    to get a grant in the first place.
    """

    async def scenario() -> None:
        bus = InMemoryEventBus()
        directory = _directory(tmp_path)
        user = await directory.create_user("Alice")
        token = await directory.issue_link_token(user.user_id)
        registry = _registry(tmp_path, bus)
        adapter = FakeChannelAdapter()
        gateway = ChannelGateway(
            adapter, registry, _policy(_workspace(tmp_path), []), bus, directory=directory
        )
        try:
            await gateway.start()
            await gateway.handle(
                ChannelCommand(CommandName.LINK_IDENTITY, TELEGRAM_A, link_code=token.code)
            )
        finally:
            await gateway.stop()
            await registry.shutdown()

        assert adapter.of_kind(ResponseKind.RESULT)
        resolved = await directory.resolve(TELEGRAM_A)
        assert resolved is not None
        assert resolved.user_id == user.user_id

    asyncio.run(scenario())


def test_unlinking_over_a_channel_stops_the_sharing(tmp_path: Path) -> None:
    async def scenario() -> None:
        bus = InMemoryEventBus()
        directory = _directory(tmp_path)
        user = await directory.create_user("Alice")
        await directory.redeem((await directory.issue_link_token(user.user_id)).code, TELEGRAM_A)
        registry = _registry(tmp_path, bus)
        adapter = FakeChannelAdapter()
        gateway = ChannelGateway(
            adapter,
            registry,
            _policy(_workspace(tmp_path), [user.user_id]),
            bus,
            directory=directory,
        )
        try:
            await gateway.start()
            await gateway.handle(ChannelCommand(CommandName.UNLINK_IDENTITY, TELEGRAM_A))
            # No longer the granted user, so no longer authorised through that grant.
            await gateway.handle(
                ChannelCommand(CommandName.START_RUN, TELEGRAM_A, objective="anything")
            )
        finally:
            await gateway.stop()
            await registry.shutdown()

        assert any("Unlinked" in text for text in adapter.texts())
        assert any("not authorised" in text for text in adapter.texts())
        assert registry.live_ids() == ()

    asyncio.run(scenario())


def test_a_gateway_without_a_directory_still_works(tmp_path: Path) -> None:
    # Identity linking is an addition, not a prerequisite. A deployment that does not want
    # it keeps per-account ownership and loses nothing else.
    async def scenario() -> None:
        bus = InMemoryEventBus()
        registry = _registry(tmp_path, bus)
        adapter = FakeChannelAdapter()
        gateway = ChannelGateway(
            adapter, registry, _policy(_workspace(tmp_path), [TELEGRAM_A.key]), bus
        )
        try:
            await gateway.start()
            await gateway.handle(
                ChannelCommand(CommandName.START_RUN, TELEGRAM_A, objective="look around")
            )
            run_id = await gateway.run_for(TELEGRAM_A)
            assert run_id is not None
            await gateway.handle(
                ChannelCommand(CommandName.LINK_IDENTITY, TELEGRAM_A, link_code="ANY")
            )
            await registry.wait(run_id)
        finally:
            await gateway.stop()
            await registry.shutdown()

        assert any("not enabled" in text for text in adapter.texts())

    asyncio.run(scenario())


def test_the_user_identity_is_opaque() -> None:
    # Not an email, not a username: an identifier that doubles as an address is one
    # somebody will eventually try to match on.
    user = UserIdentity.create("Alice")

    assert "alice" not in user.user_id.lower()
    assert len(user.user_id) == 36
