"""Who a person is, across every surface they reach Athena through.

A person using ChatyGPT and Telegram is one person. Without something that says so, they
are two, and everything downstream doubles: two sets of runs, two grants, two answers to
"what am I working on". This module is the something.

The linking itself is Athena's, not a channel's and not a client's. A channel knows a chat
id; a client knows its own session; neither is in a position to decide that two accounts are
the same human, and both would be easy to lie to. So the claim is made once, here, and the
only thing that can make it is a token Athena issued.

**Names never link anything.** Not a display name, not a Telegram username, not an email
that happens to match. A username can be changed, released and re-registered by someone
else within days, so inferring identity from one is inferring it from whoever holds a
string today. The only evidence accepted is possession of a short-lived secret the person
carried from one surface to the other, which is a claim they had to actively make.

The token is minted where the person is already authenticated, redeemed where they are not
yet known, hashed at rest, usable once, and dead within minutes. Failures are counted per
channel account, because the only way to attack a short code is to try a lot of them.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import uuid4

from athena.channels import ChannelIdentity
from athena.errors import AthenaRuntimeError

#: Unambiguous when typed or read aloud: no I, L, O, U, 0 or 1. A code that arrives by chat
#: gets retyped by hand, and a character pair nobody can tell apart is a support ticket.
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"

#: 12 characters over a 30-symbol alphabet is a little under 59 bits. Enough on its own,
#: and it is not on its own: the code dies in minutes, works once, and guessing is rate
#: limited per account.
_CODE_LENGTH = 12

DEFAULT_TTL = timedelta(minutes=10)

#: Failed redemptions one channel account may make before it is refused outright.
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_ATTEMPT_WINDOW = timedelta(minutes=15)


class IdentityError(AthenaRuntimeError):
    code = "identity_error"


@dataclass(frozen=True, slots=True)
class UserIdentity:
    """A person, as Athena knows them.

    `user_id` is opaque and Athena-minted. It is deliberately not an email, a username or
    anything a person could also be reached at: an identifier that doubles as an address is
    an identifier somebody will eventually try to match on.

    `display_name` exists to make a log readable and is never consulted by any decision
    here. That is the rule this module is built around.
    """

    user_id: str
    display_name: str | None = None

    @classmethod
    def create(cls, display_name: str | None = None) -> UserIdentity:
        return cls(user_id=str(uuid4()), display_name=display_name)


@dataclass(frozen=True, slots=True)
class ChannelLink:
    """One channel account, bound to one person."""

    identity_key: str
    user_id: str
    channel: str
    linked_at: datetime


@dataclass(frozen=True, slots=True)
class LinkToken:
    """A minted code, and the only moment its plaintext exists.

    `code` is returned once, to the surface that asked for it, and is never stored. What
    the store keeps is `token_id` and a hash, so a copy of the database is not a set of
    working link codes.
    """

    token_id: str
    code: str
    user_id: str
    expires_at: datetime


class LinkOutcome(StrEnum):
    """Why a redemption did or did not work.

    Separate reasons, one message. The caller decides how much to say — see
    `LinkResult.message`, which collapses the failures a stranger could learn from.
    """

    LINKED = "linked"
    UNKNOWN_CODE = "unknown_code"
    EXPIRED = "expired"
    ALREADY_USED = "already_used"
    #: This channel account is already somebody's. Re-binding it would be a takeover.
    ALREADY_LINKED = "already_linked"
    RATE_LIMITED = "rate_limited"


@dataclass(frozen=True, slots=True)
class LinkResult:
    outcome: LinkOutcome
    user_id: str | None = None

    @property
    def linked(self) -> bool:
        return self.outcome is LinkOutcome.LINKED

    @property
    def message(self) -> str:
        """What to tell the person, which is less than what was decided.

        Unknown, expired and already-used all say the same thing on purpose. Told apart,
        they answer "did this code ever exist" for someone who is guessing, which is the
        one question a brute-force attempt most wants answered.
        """
        match self.outcome:
            case LinkOutcome.LINKED:
                return "Linked. This account and Athena are the same person now."
            case LinkOutcome.ALREADY_LINKED:
                return (
                    "This account is already linked. Unlink it first if you mean to "
                    "attach it to someone else."
                )
            case LinkOutcome.RATE_LIMITED:
                return "Too many attempts. Wait a while before trying again."
            case _:
                return "That code is not valid. Generate a new one and use it promptly."


@dataclass(frozen=True, slots=True)
class IdentityAuditEntry:
    """An append-only record of every decision this module made.

    Codes never appear here — only `token_id`, which is the handle a person can be asked
    about without the record itself becoming a credential.
    """

    action: str
    outcome: str
    occurred_at: datetime
    user_id: str | None = None
    identity_key: str | None = None
    token_id: str | None = None


@runtime_checkable
class IdentityDirectory(Protocol):
    """Athena's answer to "who is this", and the only thing that may change it."""

    async def create_user(self, display_name: str | None = None) -> UserIdentity: ...

    async def issue_link_token(
        self, user_id: str, *, ttl: timedelta | None = None
    ) -> LinkToken: ...

    async def redeem(self, code: str, identity: ChannelIdentity) -> LinkResult: ...

    async def resolve(self, identity: ChannelIdentity) -> UserIdentity | None: ...

    async def unlink(self, identity: ChannelIdentity) -> bool: ...

    async def links_for(self, user_id: str) -> tuple[ChannelLink, ...]: ...

    async def audit(self, limit: int = 50) -> tuple[IdentityAuditEntry, ...]: ...


def generate_code() -> str:
    """A code a person can retype, from a source suitable for a secret.

    `secrets.choice`, not `random`: the module's whole value is that a code cannot be
    predicted from another one, and the default generator is seeded well enough to be
    reproducible by someone who cares to.
    """
    return "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LENGTH))


def hash_code(code: str) -> str:
    """What the store holds instead of the code.

    Plain SHA-256 rather than a password hash, deliberately: this secret has ~59 bits of
    entropy and lives for ten minutes, so there is no dictionary to slow down and no reuse
    across services to protect. The threat it defends against is a leaked database being a
    working set of codes, and a digest handles that.
    """
    return hashlib.sha256(_normalise(code).encode("utf-8")).hexdigest()


def _normalise(code: str) -> str:
    """Forgive the ways a chat mangles a typed code, without forgiving the code itself."""
    return code.strip().replace(" ", "").replace("-", "").upper()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_links (
    identity_key TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    linked_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_channel_links_user ON channel_links(user_id);

CREATE TABLE IF NOT EXISTS link_tokens (
    token_id TEXT PRIMARY KEY,
    code_hash TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    consumed_by TEXT
);

CREATE TABLE IF NOT EXISTS link_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_key TEXT NOT NULL,
    attempted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_link_attempts ON link_attempts(identity_key, attempted_at);

CREATE TABLE IF NOT EXISTS identity_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    outcome TEXT NOT NULL,
    user_id TEXT,
    identity_key TEXT,
    token_id TEXT,
    occurred_at TEXT NOT NULL
);
"""


class SqliteIdentityDirectory:
    """The directory, persisted. Blocking work runs off the event loop.

    Every state change goes through one SQL statement whose `WHERE` carries the condition
    being relied on, and then checks how many rows it touched. Reading a token and then
    marking it used in a second statement would leave a window where two redemptions both
    saw it unused — which is precisely the property "one use" is supposed to deny.
    """

    def __init__(
        self,
        database: Path | str,
        *,
        ttl: timedelta = DEFAULT_TTL,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        attempt_window: timedelta = DEFAULT_ATTEMPT_WINDOW,
    ) -> None:
        self.database = Path(database)
        if str(self.database) != ":memory:":
            self.database.parent.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl
        self.max_attempts = max_attempts
        self.attempt_window = attempt_window
        self._lock = asyncio.Lock()
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    # -- users --------------------------------------------------------------------------

    async def create_user(self, display_name: str | None = None) -> UserIdentity:
        user = UserIdentity.create(display_name)
        async with self._lock:
            await asyncio.to_thread(self._create_user, user)
        return user

    def _create_user(self, user: UserIdentity) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO users (user_id, display_name, created_at) VALUES (?, ?, ?)",
                (user.user_id, user.display_name, now),
            )
            self._record(connection, "user.created", "ok", user_id=user.user_id)

    # -- tokens -------------------------------------------------------------------------

    async def issue_link_token(self, user_id: str, *, ttl: timedelta | None = None) -> LinkToken:
        """Mint a code for a user who is already authenticated somewhere else.

        The caller is trusted to say which user it is, because the only caller is a client
        that already proved itself to Athena. This is where that trust is spent, and it is
        the reason the code is short-lived: a mistake here should not outlive the mistake.
        """
        code = generate_code()
        token = LinkToken(
            token_id=str(uuid4()),
            code=code,
            user_id=user_id,
            expires_at=datetime.now(UTC) + (ttl or self.ttl),
        )
        async with self._lock:
            await asyncio.to_thread(self._issue, token)
        return token

    def _issue(self, token: LinkToken) -> None:
        with self._connect() as connection:
            known = connection.execute(
                "SELECT 1 FROM users WHERE user_id = ?", (token.user_id,)
            ).fetchone()
            if known is None:
                self._record(connection, "token.issued", "unknown_user", user_id=token.user_id)
                raise IdentityError("No such Athena user", details={"user_id": token.user_id})
            connection.execute(
                """
                INSERT INTO link_tokens (token_id, code_hash, user_id, issued_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    token.token_id,
                    hash_code(token.code),
                    token.user_id,
                    _now(),
                    token.expires_at.isoformat(),
                ),
            )
            self._record(
                connection,
                "token.issued",
                "ok",
                user_id=token.user_id,
                token_id=token.token_id,
            )

    async def redeem(self, code: str, identity: ChannelIdentity) -> LinkResult:
        async with self._lock:
            return await asyncio.to_thread(self._redeem, code, identity)

    def _redeem(self, code: str, identity: ChannelIdentity) -> LinkResult:
        now = datetime.now(UTC)
        with self._connect() as connection:
            if self._rate_limited(connection, identity.key, now):
                self._record(
                    connection,
                    "token.redeemed",
                    LinkOutcome.RATE_LIMITED.value,
                    identity_key=identity.key,
                )
                return LinkResult(LinkOutcome.RATE_LIMITED)

            existing = connection.execute(
                "SELECT user_id FROM channel_links WHERE identity_key = ?", (identity.key,)
            ).fetchone()
            if existing is not None:
                # Refused even when the token belongs to the same person: a rebind is the
                # shape an account takeover has, and there is a deliberate way to do it.
                self._fail(connection, identity.key, now, LinkOutcome.ALREADY_LINKED)
                return LinkResult(LinkOutcome.ALREADY_LINKED, user_id=str(existing["user_id"]))

            digest = hash_code(code)
            row = connection.execute(
                "SELECT token_id, user_id, expires_at, consumed_at FROM link_tokens "
                "WHERE code_hash = ?",
                (digest,),
            ).fetchone()
            if row is None:
                self._fail(connection, identity.key, now, LinkOutcome.UNKNOWN_CODE)
                return LinkResult(LinkOutcome.UNKNOWN_CODE)

            token_id = str(row["token_id"])
            if row["consumed_at"] is not None:
                self._fail(
                    connection, identity.key, now, LinkOutcome.ALREADY_USED, token_id=token_id
                )
                return LinkResult(LinkOutcome.ALREADY_USED)
            if _parse(str(row["expires_at"])) <= now:
                self._fail(connection, identity.key, now, LinkOutcome.EXPIRED, token_id=token_id)
                return LinkResult(LinkOutcome.EXPIRED)

            # One statement, and the condition that matters is in the WHERE. Two
            # redemptions racing each other cannot both see `consumed_at IS NULL`.
            consumed = connection.execute(
                "UPDATE link_tokens SET consumed_at = ?, consumed_by = ? "
                "WHERE token_id = ? AND consumed_at IS NULL",
                (_now(), identity.key, token_id),
            )
            if consumed.rowcount != 1:
                self._fail(
                    connection, identity.key, now, LinkOutcome.ALREADY_USED, token_id=token_id
                )
                return LinkResult(LinkOutcome.ALREADY_USED)

            user_id = str(row["user_id"])
            connection.execute(
                "INSERT INTO channel_links (identity_key, user_id, channel, linked_at) "
                "VALUES (?, ?, ?, ?)",
                (identity.key, user_id, identity.channel, _now()),
            )
            connection.execute("DELETE FROM link_attempts WHERE identity_key = ?", (identity.key,))
            self._record(
                connection,
                "token.redeemed",
                LinkOutcome.LINKED.value,
                user_id=user_id,
                identity_key=identity.key,
                token_id=token_id,
            )
            return LinkResult(LinkOutcome.LINKED, user_id=user_id)

    # -- resolution ---------------------------------------------------------------------

    async def resolve(self, identity: ChannelIdentity) -> UserIdentity | None:
        return await asyncio.to_thread(self._resolve, identity)

    def _resolve(self, identity: ChannelIdentity) -> UserIdentity | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT u.user_id, u.display_name FROM channel_links l "
                "JOIN users u ON u.user_id = l.user_id WHERE l.identity_key = ?",
                (identity.key,),
            ).fetchone()
        if row is None:
            return None
        display = row["display_name"]
        return UserIdentity(str(row["user_id"]), None if display is None else str(display))

    async def unlink(self, identity: ChannelIdentity) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._unlink, identity)

    def _unlink(self, identity: ChannelIdentity) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT user_id FROM channel_links WHERE identity_key = ?", (identity.key,)
            ).fetchone()
            removed = connection.execute(
                "DELETE FROM channel_links WHERE identity_key = ?", (identity.key,)
            )
            linked = removed.rowcount == 1
            self._record(
                connection,
                "link.removed",
                "ok" if linked else "not_linked",
                user_id=None if row is None else str(row["user_id"]),
                identity_key=identity.key,
            )
            return linked

    async def links_for(self, user_id: str) -> tuple[ChannelLink, ...]:
        return await asyncio.to_thread(self._links_for, user_id)

    def _links_for(self, user_id: str) -> tuple[ChannelLink, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT identity_key, user_id, channel, linked_at FROM channel_links "
                "WHERE user_id = ? ORDER BY linked_at",
                (user_id,),
            ).fetchall()
        return tuple(
            ChannelLink(
                identity_key=str(row["identity_key"]),
                user_id=str(row["user_id"]),
                channel=str(row["channel"]),
                linked_at=_parse(str(row["linked_at"])),
            )
            for row in rows
        )

    async def audit(self, limit: int = 50) -> tuple[IdentityAuditEntry, ...]:
        return await asyncio.to_thread(self._audit, limit)

    def _audit(self, limit: int) -> tuple[IdentityAuditEntry, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT action, outcome, user_id, identity_key, token_id, occurred_at "
                "FROM identity_audit ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return tuple(
            IdentityAuditEntry(
                action=str(row["action"]),
                outcome=str(row["outcome"]),
                occurred_at=_parse(str(row["occurred_at"])),
                user_id=None if row["user_id"] is None else str(row["user_id"]),
                identity_key=None if row["identity_key"] is None else str(row["identity_key"]),
                token_id=None if row["token_id"] is None else str(row["token_id"]),
            )
            for row in rows
        )

    # -- internals ----------------------------------------------------------------------

    def _rate_limited(
        self, connection: sqlite3.Connection, identity_key: str, now: datetime
    ) -> bool:
        since = (now - self.attempt_window).isoformat()
        row = connection.execute(
            "SELECT COUNT(*) AS failures FROM link_attempts "
            "WHERE identity_key = ? AND attempted_at > ?",
            (identity_key, since),
        ).fetchone()
        return int(row["failures"]) >= self.max_attempts

    def _fail(
        self,
        connection: sqlite3.Connection,
        identity_key: str,
        now: datetime,
        outcome: LinkOutcome,
        *,
        token_id: str | None = None,
    ) -> None:
        """Count the failure, then record it.

        Every failed redemption counts, including ones caused by a code that was merely
        stale. Only counting "wrong" guesses would let an attacker probe for free by
        supplying anything that fails a different check first.
        """
        connection.execute(
            "INSERT INTO link_attempts (identity_key, attempted_at) VALUES (?, ?)",
            (identity_key, now.isoformat()),
        )
        self._record(
            connection,
            "token.redeemed",
            outcome.value,
            identity_key=identity_key,
            token_id=token_id,
        )

    def _record(
        self,
        connection: sqlite3.Connection,
        action: str,
        outcome: str,
        *,
        user_id: str | None = None,
        identity_key: str | None = None,
        token_id: str | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO identity_audit "
            "(action, outcome, user_id, identity_key, token_id, occurred_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (action, outcome, user_id, identity_key, token_id, _now()),
        )


@dataclass(frozen=True, slots=True)
class ResolvedIdentity:
    """A channel account and the person behind it, if Athena knows of one.

    An unlinked account is not an error and not anonymous-but-allowed: it is a channel
    account that has not yet made the claim. What a caller does about that is the caller's
    decision, which is why this carries both halves rather than collapsing them.
    """

    channel: ChannelIdentity
    user: UserIdentity | None = None

    @property
    def owner_key(self) -> str:
        """The key ownership hangs off.

        Linked, it is the person — so a run started in ChatyGPT is the same run Telegram
        can see and cancel. Unlinked, it falls back to the channel account, which keeps an
        unlinked user working on their own rather than sharing with every other stranger.
        """
        return self.user.user_id if self.user is not None else self.channel.key


__all__ = [
    "DEFAULT_ATTEMPT_WINDOW",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_TTL",
    "ChannelLink",
    "IdentityAuditEntry",
    "IdentityDirectory",
    "IdentityError",
    "LinkOutcome",
    "LinkResult",
    "LinkToken",
    "ResolvedIdentity",
    "SqliteIdentityDirectory",
    "UserIdentity",
    "generate_code",
    "hash_code",
]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
