"""Slack adapter, over Socket Mode.

Socket Mode rather than the Events API: it mirrors discord.py's gateway model,
needs no second public endpoint, and keeps the FastAPI app exclusively
SignalWire's - so "the signature check is the only auth on the webhook" stays
true.

Contact channels are created private. Slack workspaces are usually shared,
unlike the single private guild the Discord deployment assumes, so
workspace-visible SMS threads would be the wrong default.
"""

from __future__ import annotations

import logging
from typing import Sequence

import httpx
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_async_handlers import (
    AsyncConnectionErrorRetryHandler,
    AsyncRateLimitErrorRetryHandler,
)
from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

from ..config import Config
from ..routing import channel_name_for, topic_for
from .base import (
    Attachment,
    ChannelRef,
    InboundFile,
    MessageRef,
    OutboundMessage,
    Reaction,
    SecureResult,
)
from .slack_index import ChannelIndex
from .slack_markup import strip_markup

log = logging.getLogger("bridge.slack")

_EMOJI = {
    Reaction.PENDING: "hourglass_flowing_sand",
    Reaction.OK: "white_check_mark",
    Reaction.FAIL: "x",
}

# Subtypes that are edits, deletions, joins and the like rather than a user
# sending something new.
_IGNORED_SUBTYPES = {
    "message_changed", "message_deleted", "message_replied",
    "channel_join", "channel_leave", "channel_topic", "channel_purpose",
    "bot_message",
}


def access_hint(channel_id: str, label: str, error: str) -> str:
    """Actionable text for a Slack channel the bot cannot use.

    Slack names its failures precisely, unlike Discord's catch-all 50001, so
    each one gets its own remedy rather than a list of things to check.
    """
    remedies = {
        "channel_not_found": "the channel id is wrong, or the bot cannot see it",
        "not_in_channel": "invite the bot to the channel with /invite @yourbot",
        "is_archived": "the channel is archived; unarchive it or point at another",
        "missing_scope": "the app is missing an OAuth scope; reinstall it after adding one",
    }
    remedy = remedies.get(error, f"Slack reported {error!r}")
    return f"cannot post in the {label} channel ({channel_id}) - {remedy}"


class SlackAdapter:
    name = "slack"
    max_post_chars = 3800  # Slack's practical text limit is around 4000

    def __init__(self, config: Config, web=None, socket=None) -> None:
        self._c = config
        # The SDK's default handlers cover connection errors only. Without an
        # explicit rate-limit handler a 429 raises straight through, and on the
        # inbound path that means the SMS is dropped rather than retried --
        # conversations_list is tier-2 (~20/min) and is hit on every refresh.
        retry_handlers = [
            AsyncConnectionErrorRetryHandler(max_retry_count=2),
            AsyncRateLimitErrorRetryHandler(max_retry_count=2),
        ]
        self._web = web or AsyncWebClient(
            token=config.slack_bot_token, retry_handlers=retry_handlers
        )
        # Injecting `socket` also avoids SocketModeClient's constructor, which
        # builds an aiohttp.ClientSession and so needs a running event loop -
        # that is what lets these tests construct the adapter synchronously.
        self._socket = socket or SocketModeClient(
            app_token=config.slack_app_token, web_client=self._web
        )
        self._index = ChannelIndex(self._list_conversations)
        self._on_outbound = None
        self._bot_user_id = ""
        self._ready = False

    # -- lifecycle -------------------------------------------------------

    async def _list_conversations(self, cursor: str | None = None) -> dict:
        response = await self._web.conversations_list(
            types="public_channel,private_channel",
            exclude_archived=True,
            limit=200,
            cursor=cursor or None,
        )
        return response.data

    async def start(self, on_outbound) -> None:
        self._on_outbound = on_outbound

        auth = await self._web.auth_test()
        self._bot_user_id = auth["user_id"]
        log.info("logged in as %s (%s)", auth.get("user"), self._bot_user_id)

        await self._index.refresh()

        self._socket.socket_mode_request_listeners.append(self._handle_request)
        await self._socket.connect()
        self._ready = True

    async def close(self) -> None:
        self._ready = False
        await self._socket.close()

    def is_ready(self) -> bool:
        return self._ready

    def latency_ms(self) -> float:
        return 0.0  # Socket Mode exposes no round-trip metric

    # -- event handling --------------------------------------------------

    async def _handle_request(self, client: SocketModeClient, req: SocketModeRequest) -> None:
        # Acknowledge first: Slack disconnects a client that is slow to ack.
        await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        if req.type != "events_api":
            return
        event = (req.payload or {}).get("event") or {}
        try:
            await self._dispatch(event)
        except Exception:  # noqa: BLE001
            log.exception("failed handling %s event", event.get("type"))

    async def _dispatch(self, event: dict) -> None:
        kind = event.get("type")

        if kind in ("channel_created", "channel_rename", "channel_archive", "channel_deleted"):
            self._index.apply_event(event)
            return

        if kind != "message" or self._on_outbound is None:
            return
        if event.get("subtype") in _IGNORED_SUBTYPES:
            return
        if event.get("bot_id") or event.get("user") == self._bot_user_id:
            return
        # Thread replies stay inside the workspace: they are the discussion
        # space, not outbound traffic.
        if event.get("thread_ts") and event["thread_ts"] != event.get("ts"):
            return

        channel_id = event.get("channel", "")
        topic = await self._topic_of(channel_id)
        await self._on_outbound(
            OutboundMessage(
                channel=ChannelRef(id=channel_id),
                message=MessageRef(channel_id=channel_id, message_id=event["ts"]),
                text=event.get("text") or "",
                channel_topic=topic,
                attachments=tuple(
                    Attachment(
                        file_id=f["id"],
                        filename=f.get("name", "file"),
                        size=int(f.get("size", 0)),
                    )
                    for f in event.get("files", [])
                ),
            )
        )

    async def _topic_of(self, channel_id: str) -> str | None:
        try:
            info = await self._web.conversations_info(channel=channel_id)
        except SlackApiError:
            return None
        return ((info["channel"].get("topic") or {}).get("value")) or None

    # -- channels --------------------------------------------------------

    async def find_channel(self, number: str) -> ChannelRef | None:
        channel_id = await self._index.lookup(number)
        return ChannelRef(id=channel_id) if channel_id else None

    async def create_channel(self, number: str) -> ChannelRef:
        created = await self._web.conversations_create(
            name=channel_name_for(number), is_private=True
        )
        channel_id = created["channel"]["id"]
        await self._web.conversations_setTopic(channel=channel_id, topic=topic_for(number))
        self._index.remember(number, channel_id)

        if self._c.slack_invite_users:
            try:
                await self._web.conversations_invite(
                    channel=channel_id, users=",".join(self._c.slack_invite_users)
                )
            except SlackApiError as exc:
                log.warning("could not invite operators to %s: %s", channel_id, exc)

        await self.notify_inbox(f"New contact *{number}* -> <#{channel_id}>")
        log.info("created channel %s for %s", channel_id, number)
        return ChannelRef(id=channel_id)

    # -- messages --------------------------------------------------------

    async def post(
        self, channel: ChannelRef, text: str, files: Sequence[InboundFile] = ()
    ) -> MessageRef:
        if files:
            first = files[0]
            uploaded = await self._web.files_upload_v2(
                channel=channel.id,
                file=first.data,
                filename=first.filename,
                initial_comment=text or None,
            )
            for extra in files[1:]:
                await self._web.files_upload_v2(
                    channel=channel.id, file=extra.data, filename=extra.filename
                )
            ts = _message_ts_from_upload(uploaded)
            if not ts:
                log.debug(
                    "no message ts for uploaded file in %s; the returned "
                    "MessageRef cannot be reacted to",
                    channel.id,
                )
            return MessageRef(channel_id=channel.id, message_id=ts)

        sent = await self._web.chat_postMessage(channel=channel.id, text=text)
        return MessageRef(channel_id=channel.id, message_id=sent["ts"])

    async def reply(self, ref: MessageRef, text: str) -> None:
        await self._web.chat_postMessage(
            channel=ref.channel_id, text=text, thread_ts=ref.message_id
        )

    async def react(self, ref: MessageRef, reaction: Reaction) -> None:
        try:
            await self._web.reactions_add(
                channel=ref.channel_id, timestamp=ref.message_id, name=_EMOJI[reaction]
            )
        except SlackApiError as exc:
            if exc.response.get("error") != "already_reacted":
                raise

    async def unreact(self, ref: MessageRef, reaction: Reaction) -> None:
        try:
            await self._web.reactions_remove(
                channel=ref.channel_id, timestamp=ref.message_id, name=_EMOJI[reaction]
            )
        except SlackApiError as exc:
            if exc.response.get("error") not in ("no_reaction", "message_not_found"):
                raise

    # -- secure channel --------------------------------------------------

    async def post_secure(self, text: str) -> tuple[SecureResult, str]:
        channel_id = self._c.slack_secure_channel_id
        if not channel_id:
            return SecureResult.NOT_CONFIGURED, ""
        try:
            await self._web.chat_postMessage(channel=channel_id, text=text)
        except SlackApiError as exc:
            return SecureResult.UNAVAILABLE, access_hint(
                channel_id, "secure", exc.response.get("error", "unknown")
            )
        return SecureResult.DELIVERED, ""

    # -- misc ------------------------------------------------------------

    async def fetch_attachment(self, file_id: str) -> tuple[bytes, str]:
        """Slack file URLs are private, so this fetch carries the bot token."""
        info = await self._web.files_info(file=file_id)
        url = info["file"]["url_private"]
        async with httpx.AsyncClient() as http:
            r = await http.get(
                url,
                headers={"Authorization": f"Bearer {self._c.slack_bot_token}"},
                follow_redirects=True,
                timeout=20,
            )
            r.raise_for_status()
        ctype = info["file"].get("mimetype") or r.headers.get(
            "content-type", "application/octet-stream"
        ).split(";")[0]
        return r.content, ctype

    async def notify_inbox(self, text: str) -> None:
        """Best-effort operator notice. Never raises: callers use it on error paths."""
        channel_id = self._c.slack_inbox_channel_id
        try:
            await self._web.chat_postMessage(channel=channel_id, text=text)
        except SlackApiError as exc:
            log.error(
                "%s (wanted to report: %s)",
                access_hint(channel_id, "inbox", exc.response.get("error", "unknown")),
                text,
            )

    async def check_access(self) -> None:
        """Report unusable channels at startup instead of when a message needs them.

        The secure channel is the one that matters: nothing routine writes to it,
        so a permissions mistake there stays invisible until a passcode arrives.
        """
        targets = [(self._c.slack_inbox_channel_id, "inbox")]
        if self._c.slack_secure_channel_id:
            targets.append((self._c.slack_secure_channel_id, "secure"))
        for channel_id, label in targets:
            try:
                info = await self._web.conversations_info(channel=channel_id)
            except SlackApiError as exc:
                log.error(
                    "startup check: %s",
                    access_hint(channel_id, label, exc.response.get("error", "unknown")),
                )
                continue
            if not info["channel"].get("is_member"):
                log.error("startup check: %s", access_hint(channel_id, label, "not_in_channel"))
            else:
                log.info(
                    "startup check: %s channel #%s is writable",
                    label,
                    info["channel"].get("name", channel_id),
                )

    def strip_markup(self, text: str) -> str:
        return strip_markup(text)


def _message_ts_from_upload(uploaded) -> str:
    """Best-effort message ts for an uploaded file.

    files_upload_v2 completes through files.completeUploadExternal, whose
    response carries no `shares` block - that only comes back from
    files.info, which v2 no longer calls. So there is often no message ts
    to be had, and callers must treat an empty id as "cannot be reacted to"
    rather than as a valid reference.
    """
    for shares in (
        (uploaded.get("file") or {}).get("shares"),
        *[(f or {}).get("shares") for f in (uploaded.get("files") or [])],
    ):
        for scope in ("public", "private"):
            for entries in ((shares or {}).get(scope) or {}).values():
                if entries:
                    return entries[0].get("ts") or ""
    return ""
