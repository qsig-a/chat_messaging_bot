"""Topic -> channel index for Slack.

Slack has no local channel cache and conversations.list is tier-2 rate limited
(around 20 requests per minute), so the per-message scan the Discord adapter
does for free would exhaust the budget here.

This index is derived state: in-memory, never persisted, and rebuildable from
channel topics at any moment. Channel topics remain the routing table.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from ..routing import number_from_topic

log = logging.getLogger("bridge.slack.index")

Lister = Callable[..., Awaitable[dict]]


class ChannelIndex:
    def __init__(self, list_conversations: Lister) -> None:
        self._list = list_conversations
        self._by_number: dict[str, str] = {}
        self._missing: set[str] = set()

    async def refresh(self) -> None:
        found: dict[str, str] = {}
        cursor: str | None = None
        while True:
            page = await self._list(cursor)
            for channel in page.get("channels", []):
                topic = (channel.get("topic") or {}).get("value", "")
                number = number_from_topic(topic)
                if number:
                    found[number] = channel["id"]
            cursor = (page.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                break
        self._by_number = found
        self._missing.clear()
        log.info("channel index rebuilt: %d contact channels", len(found))

    async def lookup(self, number: str) -> str | None:
        hit = self._by_number.get(number)
        if hit is not None:
            return hit

        # A miss may mean the index is stale, so refresh once - but remember the
        # miss, or every message from an unknown number would cost a full
        # paginated conversations.list.
        if number in self._missing:
            return None

        await self.refresh()
        hit = self._by_number.get(number)
        if hit is None:
            self._missing.add(number)
        return hit

    def remember(self, number: str, channel_id: str) -> None:
        self._by_number[number] = channel_id
        self._missing.discard(number)

    def forget(self, channel_id: str) -> None:
        for number, cid in list(self._by_number.items()):
            if cid == channel_id:
                del self._by_number[number]

    def apply_event(self, event: dict) -> None:
        """Keep the index current from channel_* events."""
        kind = event.get("type")
        channel = event.get("channel")

        if kind in ("channel_created", "channel_rename") and isinstance(channel, dict):
            number = number_from_topic((channel.get("topic") or {}).get("value", ""))
            if number:
                self.remember(number, channel["id"])
            return

        if kind in ("channel_archive", "channel_deleted"):
            cid = channel if isinstance(channel, str) else (channel or {}).get("id")
            if cid:
                self.forget(cid)
