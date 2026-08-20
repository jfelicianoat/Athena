"""Telegram transport for Athena.

Deliberately a package of its own, outside `athena/`. ADR-019 puts the boundary in the
runtime and the channel outside it, and `tests/test_channels.py` enforces exactly that by
reading imports — so this is where a Telegram SDK *could* live, and the runtime still
cannot reach it.

Wiring, end to end::

    from athena.adapters.channel_gateway import (
        ChannelAccessPolicy, ChannelGrant, serve_channel,
    )
    from athena_telegram import TelegramAdapter, TelegramApi, TelegramSecurity, resolve_token

    security = TelegramSecurity.from_environment()
    adapter = TelegramAdapter(TelegramApi(resolve_token()), security)
    policy = ChannelAccessPolicy({"telegram:12345": ChannelGrant(Path("D:/repo"))})
    await serve_channel(adapter, registry, policy, event_bus, bare_text_starts_run=True)

Two allow-lists appear in that snippet and both are meant: `TelegramSecurity` decides who
the transport will listen to, and `ChannelAccessPolicy` decides which workspace an identity
gets. A mistake in either one is not enough on its own.
"""

from athena_telegram.adapter import TelegramAdapter
from athena_telegram.api import (
    TelegramApi,
    TelegramRejectedError,
    TelegramTransportError,
    TelegramUpdate,
    parse_update,
)
from athena_telegram.config import (
    ALLOWLIST_VARIABLE,
    CHANNEL,
    TOKEN_FILE_VARIABLE,
    TOKEN_VARIABLE,
    TelegramConfigError,
    TelegramSecurity,
    parse_allowlist,
    resolve_token,
)

__all__ = [
    "ALLOWLIST_VARIABLE",
    "CHANNEL",
    "TOKEN_FILE_VARIABLE",
    "TOKEN_VARIABLE",
    "TelegramAdapter",
    "TelegramApi",
    "TelegramConfigError",
    "TelegramRejectedError",
    "TelegramSecurity",
    "TelegramTransportError",
    "TelegramUpdate",
    "parse_allowlist",
    "parse_update",
    "resolve_token",
]
