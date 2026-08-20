"""Where the token comes from, and who is allowed to speak.

Both are security decisions, so both are made here, from configuration a person wrote
down — never inferred from a message, and never from a display name. Telegram usernames
can be changed and reused; the numeric id cannot, which is why it is the only thing the
allow-list holds.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

TOKEN_VARIABLE = "ATHENA_TELEGRAM_TOKEN"
TOKEN_FILE_VARIABLE = "ATHENA_TELEGRAM_TOKEN_FILE"
ALLOWLIST_VARIABLE = "ATHENA_TELEGRAM_ALLOWED_IDS"

#: The channel name that appears in every `ChannelIdentity` this adapter produces.
CHANNEL = "telegram"


class TelegramConfigError(ValueError):
    """The bot cannot be started as configured. Raised at startup, never mid-flight."""


def resolve_token(
    *,
    token: str | None = None,
    environment: dict[str, str] | None = None,
) -> str:
    """Find the bot token, preferring a file over an environment variable.

    A file is offered first because an environment variable is visible to every child
    process the agent starts, and this agent starts processes for a living. An explicit
    argument wins over both so that a host with its own secret store — DPAPI, a vault, a
    keychain — can hand the token in without it ever touching the environment.
    """
    if token is not None:
        if not token.strip():
            raise TelegramConfigError("The supplied Telegram token is empty")
        return token.strip()

    env = os.environ if environment is None else environment
    path = env.get(TOKEN_FILE_VARIABLE)
    if path:
        try:
            contents = Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise TelegramConfigError(f"The token file could not be read: {exc}") from exc
        if not contents:
            raise TelegramConfigError("The token file is empty")
        return contents

    variable = env.get(TOKEN_VARIABLE, "").strip()
    if not variable:
        raise TelegramConfigError(
            f"No Telegram token. Set {TOKEN_FILE_VARIABLE} or {TOKEN_VARIABLE}."
        )
    return variable


def parse_allowlist(raw: str | None) -> frozenset[int]:
    """Read a comma-separated list of numeric Telegram ids.

    Anything that is not a number is a configuration error rather than a skipped entry. A
    typo that silently shrinks an allow-list is a security hole that looks like a working
    system, and this is the last moment it can be caught cheaply.
    """
    if raw is None or not raw.strip():
        return frozenset()
    identifiers: set[int] = set()
    for piece in raw.split(","):
        candidate = piece.strip()
        if not candidate:
            continue
        try:
            identifiers.add(int(candidate))
        except ValueError as exc:
            raise TelegramConfigError(
                f"{ALLOWLIST_VARIABLE} must be numeric Telegram ids; got {candidate!r}"
            ) from exc
    return frozenset(identifiers)


@dataclass(frozen=True, slots=True)
class TelegramSecurity:
    """Who may talk to this bot.

    `private_mode` is on by default and means what it says: an account that is not on the
    list is refused. Turning it off is possible and deliberately awkward to do by accident,
    because a bot with an open door is one search away from being found.

    The allow-list is *also* enforced by Athena's own `ChannelAccessPolicy`, which is what
    maps an identity to a workspace. Two independent checks is the point — a mistake in
    either one is not enough on its own.
    """

    allowed_user_ids: frozenset[int] = field(default_factory=frozenset)
    private_mode: bool = True
    #: Refuse anything that is not a one-to-one conversation. A group has bystanders, and
    #: plain text is treated as an objective in this channel.
    private_chats_only: bool = True

    @classmethod
    def from_environment(cls, environment: dict[str, str] | None = None) -> TelegramSecurity:
        env = os.environ if environment is None else environment
        return cls(allowed_user_ids=parse_allowlist(env.get(ALLOWLIST_VARIABLE)))

    @classmethod
    def open_to(cls, user_ids: Iterable[int]) -> TelegramSecurity:
        return cls(allowed_user_ids=frozenset(user_ids))

    def permits(self, user_id: int) -> bool:
        if not self.private_mode:
            return True
        return user_id in self.allowed_user_ids

    def validate(self) -> None:
        """Refuse to start in a configuration that is almost certainly a mistake."""
        if self.private_mode and not self.allowed_user_ids:
            raise TelegramConfigError(
                "Private mode is on and the allow-list is empty, so the bot would refuse "
                f"everyone. Set {ALLOWLIST_VARIABLE}, or turn private mode off knowingly."
            )


__all__ = [
    "ALLOWLIST_VARIABLE",
    "CHANNEL",
    "TOKEN_FILE_VARIABLE",
    "TOKEN_VARIABLE",
    "TelegramConfigError",
    "TelegramSecurity",
    "parse_allowlist",
    "resolve_token",
]
