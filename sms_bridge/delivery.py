"""Platform-agnostic message handling.

This module owns every policy decision. Adapters report what they can do; the
rules about what happens next live here, so they exist once and are testable
against a fake.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from .chat.base import (
    ChannelRef,
    InboundFile,
    MessageRef,
    OutboundMessage,
    Reaction,
    SecureResult,
)
from .config import Config
from .routing import normalise_number, number_from_topic
from .signalwire import SignalWire
from .store import Store
from .text import chunk, looks_like_a_code, segment_count

log = logging.getLogger("bridge.delivery")

SUPPRESSED_NOTICE = (
    "_(message contained a passcode - suppressed. Read it in the "
    "SignalWire message logs.)_"
)
NO_BODY_NOTICE = "_(no text body)_"


@dataclass(frozen=True)
class InboundSms:
    sender: str
    body: str
    media_urls: tuple[str, ...] = field(default_factory=tuple)
    sid: str = ""


class Delivery:
    def __init__(
        self,
        config: Config,
        adapter,
        store: Store,
        signalwire: SignalWire,
        media=None,
    ) -> None:
        self._c = config
        self._adapter = adapter
        self._store = store
        self._sw = signalwire
        self._media = media

    # -- inbound ---------------------------------------------------------

    async def handle_inbound(self, sms: InboundSms) -> None:
        body = sms.body
        files: list[InboundFile] = []
        captions: list[str] = []

        # Resolve media first. Carriers routinely deliver an MMS caption as its
        # own text/plain part instead of in Body, and that text has to be folded
        # back in *before* the passcode check below - otherwise a code sent as a
        # caption skips redaction and lands in the contact channel.
        for url in sms.media_urls:
            got = await self._sw.fetch_media(url)
            if not got:
                body = (body + f"\n_(attachment too large or unfetchable: {url})_").strip()
                continue
            data, filename, ctype = got
            if ctype == "text/plain":
                caption = data.decode("utf-8", "replace").strip()
                if caption:
                    captions.append(caption)
                continue
            files.append(InboundFile(filename=filename, content_type=ctype, data=data))

        if captions:
            body = "\n".join([body, *captions]).strip()

        if self._c.redact_codes and looks_like_a_code(body):
            await self._handle_passcode(sms.sender, body)
            return

        channel = await self._channel_for(sms.sender)
        content = body.strip() or NO_BODY_NOTICE
        limit = self._adapter.max_post_chars
        pending = files
        for piece in [content[i:i + limit] for i in range(0, len(content), limit)]:
            await self._adapter.post(channel, piece, pending)
            pending = []  # only attach to the first chunk

    async def _handle_passcode(self, sender: str, body: str) -> None:
        """The one rule that must never bend: a code never reaches a contact channel."""
        result, hint = await self._adapter.post_secure(f"**{sender}**\n```{body}```")

        if result is SecureResult.DELIVERED:
            return

        if result is SecureResult.UNAVAILABLE:
            # Configured but unusable. Without this the code silently takes the
            # "no secure channel" path and nobody learns it went nowhere.
            log.error("passcode not delivered: %s", hint)
            await self._adapter.notify_inbox(
                f"Passcode from **{sender}** was suppressed: {hint}."
            )

        channel = await self._channel_for(sender)
        await self._adapter.post(channel, SUPPRESSED_NOTICE)

    async def _channel_for(self, number: str) -> ChannelRef:
        existing = await self._adapter.find_channel(number)
        if existing is not None:
            return existing
        channel = await self._adapter.create_channel(number)
        log.info("created channel for %s", number)
        return channel

    # -- outbound --------------------------------------------------------

    async def handle_outbound(self, msg: OutboundMessage) -> None:
        text = msg.text or ""

        if self._c.note_prefix and text.startswith(self._c.note_prefix):
            return  # internal note: visible in chat, never sent

        if self._c.command_prefix and text.startswith(self._c.command_prefix):
            await self._handle_command(msg, text)
            return

        to = number_from_topic(msg.channel_topic)
        if not to:
            return  # not a contact channel

        await self._send(msg, to, text)

    async def _handle_command(self, msg: OutboundMessage, text: str) -> None:
        parts = text[len(self._c.command_prefix):].strip().split(None, 1)
        if not parts:
            await self._adapter.reply(
                msg.message, f"Usage: `{self._c.command_prefix} +14165550123 message`"
            )
            return

        number = normalise_number(parts[0])
        if not number:
            await self._adapter.reply(msg.message, "That doesn't look like a phone number.")
            return

        channel = await self._channel_for(number)
        if len(parts) > 1 and parts[1].strip():
            await self._send(msg, number, parts[1])
        else:
            await self._adapter.reply(msg.message, f"Channel ready: {channel.id}")

    async def _send(self, msg: OutboundMessage, to: str, raw: str) -> None:
        body = self._adapter.strip_markup(raw)
        media_urls = await self._media_urls_for(msg)

        if not body and not media_urls:
            return

        segments = segment_count(body)
        if segments > 1:
            log.info("outbound to %s is %d segments", to, segments)

        await self._adapter.react(msg.message, Reaction.PENDING)
        sent_sids: list[str] = []
        try:
            pieces = chunk(body) if body else [""]
            for index, piece in enumerate(pieces):
                sid = await self._sw.send_sms(
                    to, piece, media_urls if index == 0 else ()
                )
                sent_sids.append(sid)
                self._store.remember_outbound(
                    sid, msg.message.channel_id, msg.message.message_id
                )
        except Exception as exc:  # noqa: BLE001
            log.exception("send failed")
            # Forget the chunks that did go out: a late `delivered` callback for
            # one of them would otherwise put OK on a message we just marked FAIL.
            for sid in sent_sids:
                self._store.forget_outbound(sid)
            await self._adapter.unreact(msg.message, Reaction.PENDING)
            await self._adapter.react(msg.message, Reaction.FAIL)
            await self._adapter.reply(msg.message, f"Send failed: `{exc}`")

    async def _media_urls_for(self, msg: OutboundMessage) -> list[str]:
        """Overridden in Task 14 to mint signed URLs. Empty until then."""
        return []

    # -- status ----------------------------------------------------------

    async def update_status(self, sid: str, status: str, error_code: str) -> None:
        ref = self._store.lookup_outbound(sid)
        if not ref:
            return
        channel_id, message_id = ref
        message = MessageRef(channel_id=channel_id, message_id=message_id)

        if status == "delivered":
            await self._adapter.unreact(message, Reaction.PENDING)
            await self._adapter.react(message, Reaction.OK)
        elif status in ("failed", "undelivered"):
            await self._adapter.unreact(message, Reaction.PENDING)
            await self._adapter.react(message, Reaction.FAIL)
            detail = f" (error {error_code})" if error_code else ""
            await self._adapter.reply(message, f"Carrier reported `{status}`{detail}")

    # -- worker ----------------------------------------------------------

    async def run_worker(self, queue: asyncio.Queue) -> None:
        while True:
            sms: InboundSms = await queue.get()
            try:
                await self.handle_inbound(sms)
            except Exception:  # noqa: BLE001
                log.exception("failed to deliver inbound message")
                await self._adapter.notify_inbox(
                    f"Failed to deliver inbound SMS from {sms.sender}. Check logs."
                )
            finally:
                queue.task_done()
