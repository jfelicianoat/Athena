"""Telegram as a `ChannelAdapter`. Transport, and nothing else.

What this file does is turn Telegram updates into `ChannelMessage` and `ChannelResponse`
into Telegram messages. What it deliberately does not do is anything Athena does: it never
touches a `ModelProvider`, never calls a tool, never embeds an `AgentLoop`, and never
decides whether an action is allowed. Every one of those lives behind `ChannelGateway`,
which is handed this object and cannot tell what it is.

The three problems that are genuinely Telegram's are handled here, because nowhere else
knows about them:

- **Duplicates.** `getUpdates` with an offset usually prevents them, but a crash between
  receiving and committing the offset replays the batch. An update acted on twice is a run
  started twice, so seen ids are remembered.
- **Malformed and irrelevant updates.** A bot receives edits, joins, stickers and whatever
  the API adds next. None of that is an error, and none of it is a message.
- **Rate limits.** Telegram allows roughly one message per second to a chat. A run
  produces bursts, so progress is coalesced rather than queued — the newest state of a run
  is worth more than a faithful replay of how it got there.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict

from athena.channels import ChannelIdentity, ChannelMessage, ChannelResponse, ResponseKind
from athena.errors import AthenaRuntimeError
from athena_telegram.api import (
    TelegramApi,
    TelegramRejectedError,
    TelegramTransportError,
    TelegramUpdate,
    parse_update,
)
from athena_telegram.config import CHANNEL, TelegramSecurity

_logger = logging.getLogger(__name__)

#: How many update ids to remember. Telegram's own offset does the real work; this only
#: has to cover a replayed batch, and an unbounded set would be a slow leak.
_SEEN_UPDATES = 1_000

#: Minimum gap between two messages to the same chat. Telegram's documented limit is about
#: one per second; this stays under it rather than discovering the limit by being throttled.
_MIN_SEND_INTERVAL_SECONDS = 1.0

#: Telegram rejects anything longer. Athena's messages are short, but a verification
#: summary quoting a failing test is not guaranteed to be.
_MAX_MESSAGE_CHARS = 4_096

_REFUSAL = (
    "This account is not authorised to use Athena. If that is wrong, whoever runs this "
    "bot has to add your Telegram id."
)

_GROUP_REFUSAL = "Athena only works in a direct conversation. Message the bot privately instead."


class TelegramAdapter:
    """A `ChannelAdapter` backed by the Telegram Bot API.

    Satisfies the protocol structurally; it inherits nothing from Athena, which is what the
    boundary was shaped for.
    """

    def __init__(
        self,
        api: TelegramApi,
        security: TelegramSecurity,
        *,
        idle_backoff_seconds: float = 3.0,
    ) -> None:
        self._api = api
        self._security = security
        self._idle_backoff = idle_backoff_seconds
        self._offset: int | None = None
        self._pending: list[TelegramUpdate] = []
        self._seen: OrderedDict[int, None] = OrderedDict()
        self._chats: dict[str, int] = {}
        self._last_sent: dict[int, float] = {}
        self._last_progress: dict[int, str] = {}
        self._running = False
        #: Counters a test or an operator can read without parsing logs.
        self.dropped_duplicates = 0
        self.dropped_malformed = 0
        self.refused_identities = 0
        self.coalesced_progress = 0

    @property
    def channel(self) -> str:
        return CHANNEL

    async def start(self) -> None:
        self._security.validate()
        self._running = True

    async def stop(self) -> None:
        self._running = False

    # -- inbound ------------------------------------------------------------------------

    async def receive(self) -> ChannelMessage | None:
        """Next message worth acting on, or `None` once the adapter is stopped.

        Everything refused here is refused *before* the gateway sees it, so an unauthorised
        account cannot reach the runtime at all — not even to be told no by it. It is told
        no by the transport, which is the cheapest place to say it.
        """
        while self._running:
            update = await self._next_update()
            if update is None:
                return None

            if self._security.private_chats_only and not update.is_private:
                # Said once, then dropped: replying to every message in a group the bot was
                # added to would be the bot spamming a room it should not be in.
                await self._refuse(update, _GROUP_REFUSAL, only_once=True)
                continue

            if not self._security.permits(update.user_id):
                self.refused_identities += 1
                _logger.warning("telegram.identity_refused user=%s", update.user_id)
                await self._refuse(update, _REFUSAL)
                continue

            identity = self._identity(update)
            self._chats[identity.key] = update.chat_id
            return ChannelMessage(
                identity,
                update.text,
                message_id=None if update.message_id is None else str(update.message_id),
            )
        return None

    async def _next_update(self) -> TelegramUpdate | None:
        while self._running:
            if self._pending:
                return self._pending.pop(0)
            if not await self._poll():
                # Nothing to read, or Telegram is unhappy. Either way, waiting is right;
                # a tight loop would turn an outage into a self-inflicted rate limit.
                await asyncio.sleep(self._idle_backoff)
        return None

    async def _poll(self) -> bool:
        try:
            raw_updates = await self._api.get_updates(self._offset)
        except TelegramTransportError as error:
            _logger.warning("telegram.poll_failed %s", error)
            return False
        except TelegramRejectedError as error:
            # A wrong token does not get better by asking again, but the loop is not this
            # object's to end, so it reports and backs off rather than spinning.
            _logger.error("telegram.poll_rejected %s", error)
            return False

        accepted = False
        for raw in raw_updates:
            update = parse_update(raw)
            if update is None:
                self.dropped_malformed += 1
                _logger.info("telegram.update_ignored")
                continue
            # The offset moves for every well-formed update, including ones this adapter
            # will not act on. Leaving it behind would mean re-reading them forever.
            self._offset = max(self._offset or 0, update.update_id + 1)
            if update.update_id in self._seen:
                self.dropped_duplicates += 1
                _logger.info("telegram.update_duplicate id=%s", update.update_id)
                continue
            self._remember(update.update_id)
            self._pending.append(update)
            accepted = True
        return accepted

    def _remember(self, update_id: int) -> None:
        self._seen[update_id] = None
        while len(self._seen) > _SEEN_UPDATES:
            self._seen.popitem(last=False)

    def _identity(self, update: TelegramUpdate) -> ChannelIdentity:
        # Numeric ids only. A username can be changed and reused, so identifying by one
        # would mean the allow-list decays into a list of whoever holds those names now.
        return ChannelIdentity(
            channel=CHANNEL,
            user_id=str(update.user_id),
            conversation_id=str(update.chat_id),
            display_name=update.username,
        )

    async def _refuse(self, update: TelegramUpdate, text: str, *, only_once: bool = False) -> None:
        if only_once and update.chat_id in self._last_sent:
            return
        await self._send(update.chat_id, text)

    # -- outbound -----------------------------------------------------------------------

    async def deliver(self, response: ChannelResponse) -> None:
        """Send one response, coalescing progress and never queueing it.

        Progress is the only kind that may be dropped. Anything that ends a run, reports a
        refusal or announces a failure is sent whatever the clock says: those are the
        messages the person is waiting for, and a rate limit is not a reason to withhold
        one.
        """
        chat_id = self._chat_for(response)
        if chat_id is None:
            _logger.warning("telegram.no_chat_for_response")
            return

        if response.kind is ResponseKind.PROGRESS:
            if self._last_progress.get(chat_id) == response.text:
                # The same line twice says nothing new, and Telegram would reject an
                # identical edit anyway.
                self.coalesced_progress += 1
                return
            if not self._may_send(chat_id):
                self.coalesced_progress += 1
                return
            self._last_progress[chat_id] = response.text
        else:
            await self._wait_for_slot(chat_id)

        await self._send(chat_id, response.text)

    def _chat_for(self, response: ChannelResponse) -> int | None:
        """Where to reply.

        The conversation the identity last spoke in wins over the one carried on the
        response, because a person can move between chats and the newest is where they are
        looking. Falling back to the response keeps an event deliverable for an identity
        this adapter has not seen speak.
        """
        known = self._chats.get(response.identity.key)
        if known is not None:
            return known
        try:
            return int(response.identity.conversation_id)
        except ValueError:
            return None

    def _may_send(self, chat_id: int) -> bool:
        last = self._last_sent.get(chat_id)
        return last is None or (time.monotonic() - last) >= _MIN_SEND_INTERVAL_SECONDS

    async def _wait_for_slot(self, chat_id: int) -> None:
        last = self._last_sent.get(chat_id)
        if last is None:
            return
        remaining = _MIN_SEND_INTERVAL_SECONDS - (time.monotonic() - last)
        if remaining > 0:
            await asyncio.sleep(remaining)

    async def _send(self, chat_id: int, text: str) -> None:
        body = text if len(text) <= _MAX_MESSAGE_CHARS else text[: _MAX_MESSAGE_CHARS - 1] + "…"
        try:
            await self._api.send_message(chat_id, body)
        except AthenaRuntimeError as error:
            # A failed send is an ordinary event. The gateway already treats it as one; the
            # adapter's job is not to make it fatal on the way past.
            _logger.warning("telegram.send_failed code=%s", error.code)
            return
        self._last_sent[chat_id] = time.monotonic()


__all__ = ["TelegramAdapter"]
