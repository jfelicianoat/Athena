"""Memory that is worth having, which mostly means memory that refuses things.

A store that accepts whatever the model concluded is not knowledge; it is a stale second
copy of the repository, held with more confidence than the repository itself. Most of what
follows checks that the store keeps the distinction between what was said, what was
checked, and what a person stood behind.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from athena.project_memory import (
    MemoryError_,
    MemoryKind,
    MemoryStatus,
    ProjectMemoryItem,
    ProjectMemoryStore,
    SqliteProjectMemory,
    VerificationState,
    render_for_context,
)

PROJECT = "athena"


def _store(tmp_path: Path) -> SqliteProjectMemory:
    return SqliteProjectMemory(tmp_path / "memory.db")


# ----------------------------------------------------------------- nothing enters as true


def test_everything_the_agent_produces_enters_unverified(tmp_path: Path) -> None:
    """The rule the whole module is built around.

    Writing an inference straight in as truth is how a memory ends up confidently wrong
    about a codebase that changed underneath it.
    """

    async def scenario() -> None:
        store = _store(tmp_path)

        item = await store.propose(
            PROJECT,
            MemoryKind.VERIFIED_COMMAND,
            "the tests run with pytest -q",
            source="agent",
        )

        assert item.verification_state is VerificationState.PROPOSED
        assert item.confidence <= 0.5

    asyncio.run(scenario())


def test_there_is_no_way_to_write_a_fact_directly(tmp_path: Path) -> None:
    # `propose` is the only entry point and it always produces PROPOSED. An agent that
    # could write VERIFIED would be grading its own homework.
    del tmp_path

    assert not hasattr(SqliteProjectMemory, "remember")
    assert "propose" in dir(SqliteProjectMemory)


def test_an_untraceable_memory_is_refused(tmp_path: Path) -> None:
    """An item that cannot be traced cannot be judged, and an unjudgeable hint is a rumour."""

    async def scenario() -> None:
        store = _store(tmp_path)

        with pytest.raises(MemoryError_):
            await store.propose(PROJECT, MemoryKind.DOMAIN_FACT, "something", source="  ")

    asyncio.run(scenario())


def test_an_empty_memory_is_refused(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)

        with pytest.raises(MemoryError_):
            await store.propose(PROJECT, MemoryKind.DOMAIN_FACT, "   ", source="agent")

    asyncio.run(scenario())


def test_confidence_is_a_probability(tmp_path: Path) -> None:
    del tmp_path

    with pytest.raises(MemoryError_):
        ProjectMemoryItem(
            id="x",
            project_id=PROJECT,
            kind=MemoryKind.DOMAIN_FACT,
            content="something",
            source="agent",
            confidence=1.5,
        )


# ------------------------------------------------------------------------- earning weight


def test_standing_goes_up_when_something_checks_it(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        item = await store.propose(
            PROJECT, MemoryKind.VERIFIED_COMMAND, "pytest -q works", source="agent"
        )

        checked = await store.approve(item.id, state=VerificationState.VERIFIED)

        assert checked.verification_state is VerificationState.VERIFIED
        assert checked.confidence > item.confidence

    asyncio.run(scenario())


def test_a_person_outranks_a_check(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        item = await store.propose(
            PROJECT, MemoryKind.PROJECT_CONVENTION, "comments in Spanish", source="agent"
        )
        await store.approve(item.id, state=VerificationState.VERIFIED)

        confirmed = await store.approve(item.id, state=VerificationState.USER_CONFIRMED)

        assert confirmed.verification_state is VerificationState.USER_CONFIRMED

    asyncio.run(scenario())


def test_standing_cannot_be_lowered_quietly(tmp_path: Path) -> None:
    """A belief that turns out wrong is superseded or forgotten — both keep the record.

    A silent demotion would leave an item that once looked trustworthy with no trace of why
    it stopped being so.
    """

    async def scenario() -> None:
        store = _store(tmp_path)
        item = await store.propose(
            PROJECT, MemoryKind.DOMAIN_FACT, "the API is versioned", source="agent"
        )
        await store.approve(item.id, state=VerificationState.USER_CONFIRMED)

        with pytest.raises(MemoryError_):
            await store.approve(item.id, state=VerificationState.PROPOSED)

    asyncio.run(scenario())


def test_approving_something_that_does_not_exist_is_an_error(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)

        with pytest.raises(MemoryError_):
            await store.approve("nobody", state=VerificationState.VERIFIED)

    asyncio.run(scenario())


# ----------------------------------------------------------------------------- supersedes


def test_correcting_a_memory_keeps_what_was_believed_before(tmp_path: Path) -> None:
    """ "We used to think X" is exactly what a person debugging a wrong decision needs."""

    async def scenario() -> None:
        store = _store(tmp_path)
        first = await store.propose(
            PROJECT, MemoryKind.VERIFIED_COMMAND, "build with make", source="agent"
        )

        second = await store.update(first.id, "build with cargo build", source="user")

        old = await store.get(first.id)
        assert old is not None
        assert old.status is MemoryStatus.SUPERSEDED
        assert old.content == "build with make", "the old wording survives"
        assert second.supersedes == first.id
        assert second.is_active

    asyncio.run(scenario())


def test_the_chain_of_belief_can_be_walked(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        first = await store.propose(PROJECT, MemoryKind.DOMAIN_FACT, "one", source="agent")
        second = await store.update(first.id, "two", source="agent")
        third = await store.update(second.id, "three", source="agent")

        chain = await store.history(third.id)

        assert [item.content for item in chain] == ["three", "two", "one"]

    asyncio.run(scenario())


def test_superseding_something_already_replaced_is_refused(tmp_path: Path) -> None:
    # Two corrections racing each other would otherwise both claim to replace the same
    # item, leaving two actives where there should be one.
    async def scenario() -> None:
        store = _store(tmp_path)
        first = await store.propose(PROJECT, MemoryKind.DOMAIN_FACT, "one", source="agent")
        await store.update(first.id, "two", source="agent")

        with pytest.raises(MemoryError_):
            await store.propose(
                PROJECT,
                MemoryKind.DOMAIN_FACT,
                "also two",
                source="agent",
                supersedes=first.id,
            )

    asyncio.run(scenario())


def test_a_superseded_memory_is_not_retrieved(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        first = await store.propose(
            PROJECT, MemoryKind.VERIFIED_COMMAND, "build with make", source="agent"
        )
        await store.update(first.id, "build with cargo", source="agent")

        found = await store.search(PROJECT, "build")

        assert [item.content for item in found] == ["build with cargo"]

    asyncio.run(scenario())


# ------------------------------------------------------------------------------- forgetting


def test_forgetting_retires_without_erasing(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        item = await store.propose(
            PROJECT, MemoryKind.ENVIRONMENT_FACT, "python 3.11 on this host", source="agent"
        )

        assert await store.forget(item.id) is True

        gone = await store.get(item.id)
        assert gone is not None, "deleting would hide that it was ever believed"
        assert gone.status is MemoryStatus.FORGOTTEN
        assert await store.search(PROJECT, "python") == ()

    asyncio.run(scenario())


def test_forgetting_twice_is_not_an_error_and_is_not_a_lie(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        item = await store.propose(PROJECT, MemoryKind.DOMAIN_FACT, "x", source="agent")
        await store.forget(item.id)

        assert await store.forget(item.id) is False

    asyncio.run(scenario())


# ------------------------------------------------------------------------------ retrieval


def test_retrieval_is_selective_and_never_returns_the_store(tmp_path: Path) -> None:
    """A context builder that loaded everything would spend the window on the irrelevant."""

    async def scenario() -> None:
        store = _store(tmp_path)
        for index in range(30):
            await store.propose(
                PROJECT, MemoryKind.DOMAIN_FACT, f"fact number {index}", source="agent"
            )
        await store.propose(
            PROJECT, MemoryKind.PROJECT_CONVENTION, "tests live in tests/", source="agent"
        )

        found = await store.search(PROJECT, "tests", limit=5)

        assert len(found) <= 5
        assert any("tests" in item.content for item in found)

    asyncio.run(scenario())


def test_what_a_person_confirmed_outranks_what_a_model_guessed(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        guess = await store.propose(
            PROJECT, MemoryKind.VERIFIED_COMMAND, "the build command is make", source="agent"
        )
        truth = await store.propose(
            PROJECT, MemoryKind.VERIFIED_COMMAND, "the build command is cargo", source="user"
        )
        await store.approve(truth.id, state=VerificationState.USER_CONFIRMED)

        found = await store.search(PROJECT, "build command")

        assert found[0].id == truth.id
        assert guess.id in {item.id for item in found}, "the guess is not hidden, just lower"

    asyncio.run(scenario())


def test_retrieval_can_demand_a_minimum_standing(tmp_path: Path) -> None:
    # A prompt that must not carry guesses can say so.
    async def scenario() -> None:
        store = _store(tmp_path)
        await store.propose(PROJECT, MemoryKind.DOMAIN_FACT, "a plausible guess", source="agent")
        checked = await store.propose(
            PROJECT, MemoryKind.DOMAIN_FACT, "a checked guess", source="agent"
        )
        await store.approve(checked.id, state=VerificationState.VERIFIED)

        found = await store.search(PROJECT, "guess", minimum_state=VerificationState.VERIFIED)

        assert [item.content for item in found] == ["a checked guess"]

    asyncio.run(scenario())


def test_retrieval_can_be_narrowed_to_a_kind(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        await store.propose(PROJECT, MemoryKind.VERIFIED_COMMAND, "run pytest", source="agent")
        await store.propose(
            PROJECT, MemoryKind.PROJECT_CONVENTION, "run tests before pushing", source="agent"
        )

        found = await store.search(PROJECT, "run", kinds=[MemoryKind.VERIFIED_COMMAND])

        assert [item.kind for item in found] == [MemoryKind.VERIFIED_COMMAND]

    asyncio.run(scenario())


def test_one_project_never_sees_another_s_memory(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        await store.propose("alpha", MemoryKind.DOMAIN_FACT, "alpha detail", source="agent")

        assert await store.search("beta", "alpha detail") == ()

    asyncio.run(scenario())


def test_an_unrelated_query_returns_nothing_rather_than_the_nearest_thing(
    tmp_path: Path,
) -> None:
    # Crude overlap is honest about being crude. Returning the least-bad match would make
    # an empty memory look full.
    async def scenario() -> None:
        store = _store(tmp_path)
        await store.propose(
            PROJECT, MemoryKind.DOMAIN_FACT, "the parser is recursive descent", source="agent"
        )

        assert await store.search(PROJECT, "kubernetes ingress") == ()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------- freshness


def test_a_memory_knows_how_old_its_hint_is(tmp_path: Path) -> None:
    """The repository is the present; a memory is a hint about the past."""
    del tmp_path
    old = ProjectMemoryItem(
        id="x",
        project_id=PROJECT,
        kind=MemoryKind.VERIFIED_COMMAND,
        content="build with make",
        source="agent",
        created_at=datetime.now(UTC) - timedelta(days=90),
    )

    assert old.is_stale(older_than=timedelta(days=30))
    assert not old.is_stale(older_than=timedelta(days=365))


def test_it_survives_the_process_that_wrote_it(tmp_path: Path) -> None:
    """The entire reason the module exists: Athena starts a session knowing something."""

    async def scenario() -> None:
        first = _store(tmp_path)
        item = await first.propose(
            PROJECT, MemoryKind.PROJECT_CONVENTION, "comments in Spanish", source="user"
        )
        await first.approve(item.id, state=VerificationState.USER_CONFIRMED)

        reopened = _store(tmp_path)
        found = await reopened.search(PROJECT, "comments")

        assert [entry.content for entry in found] == ["comments in Spanish"]
        assert found[0].verification_state is VerificationState.USER_CONFIRMED

    asyncio.run(scenario())


# ------------------------------------------------------------------------ reaching a prompt


def test_what_reaches_a_prompt_is_labelled_with_its_standing() -> None:
    """A model told "the build command is X" behaves differently from one told
    "somebody once proposed that X", and the second is what an unverified item is."""
    unverified = ProjectMemoryItem(
        id="a",
        project_id=PROJECT,
        kind=MemoryKind.VERIFIED_COMMAND,
        content="build with make",
        source="agent",
    )
    confirmed = ProjectMemoryItem(
        id="b",
        project_id=PROJECT,
        kind=MemoryKind.PROJECT_CONVENTION,
        content="comments in Spanish",
        source="user",
        verification_state=VerificationState.USER_CONFIRMED,
    )

    rendered = render_for_context([unverified, confirmed])

    assert "unverified" in rendered
    assert "confirmed by the user" in rendered
    assert "the repository is the source of truth" in rendered


def test_nothing_to_say_says_nothing() -> None:
    # An empty preamble in every prompt would cost tokens to communicate absence.
    assert render_for_context([]) == ""


def test_the_store_satisfies_the_contract(tmp_path: Path) -> None:
    assert isinstance(_store(tmp_path), ProjectMemoryStore)
