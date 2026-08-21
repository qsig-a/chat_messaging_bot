"""Discord adapter.

Channels are found by scanning guild.text_channels for an `sms:+E164` topic
token. That scan is free: discord.py keeps the channel list in a local cache
maintained by the gateway.
"""

from __future__ import annotations

import io
import logging
import re
from typing import Sequence

import discord
import httpx

from ..config import Config
from ..routing import channel_name_for, number_from_topic, topic_for
from .base import (
    Attachment,
    ChannelRef,
    InboundFile,
    MessageRef,
    OutboundMessage,
    Reaction,
    SecureResult,
)

log = logging.getLogger("bridge.discord")

_EMOJI = {
    Reaction.PENDING: "\N{HOURGLASS WITH FLOWING SAND}",
    Reaction.OK: "\N{WHITE HEAVY CHECK MARK}",
    Reaction.FAIL: "\N{CROSS MARK}",
}

_CODEBLOCK = re.compile(r"```(?:[a-zA-Z0-9+-]*\n)?(.*?)```", re.S)
_INLINE = re.compile(r"`([^`]*)`")
# Asterisk, tilde and spoiler delimiters pair freely, which matches how Discord
# renders them - it really does italicise across `5*x + 3*y`.
_EMPHASIS = re.compile(r"(\*\*\*|\*\*|\*|~~|\|\|)(.+?)\1", re.S)
# Underscores need word boundaries, which Discord also requires: it does not
# italicise snake_case_name. Without this guard any even number of underscores
# flattened the span between them, so `SIGNALWIRE_API_TOKEN` reached the handset
# as `SIGNALWIREAPITOKEN` - env var names are exactly what people paste when
# troubleshooting this bridge.
_UNDERSCORE = re.compile(r"(?<![A-Za-z0-9_])(___|__|_)(.+?)\1(?![A-Za-z0-9_])", re.S)
_CUSTOM_EMOJI = re.compile(r"<a?:([A-Za-z0-9_]+):\d+>")
_MENTION = re.compile(r"<[@#][!&]?\d+>")


def strip_markup(text: str) -> str:
    """Discord formatting is literal text over SMS. Flatten it."""
    text = _CODEBLOCK.sub(lambda m: m.group(1).strip(), text)
    text = _INLINE.sub(r"\1", text)
    for _ in range(3):  # nested emphasis
        text = _EMPHASIS.sub(r"\2", text)
        text = _UNDERSCORE.sub(r"\2", text)
    text = _CUSTOM_EMOJI.sub(r":\1:", text)
    text = _MENTION.sub("", text)
    text = re.sub(r"^>\s?", "", text, flags=re.M)
    return text.strip()


def access_hint(channel_id: int, label: str) -> str:
    """Actionable text for a channel the bot cannot use.

    Discord reports "channel deleted", "bot was never given access" and "a
    channel-level override denies it" all as 50001 Missing Access, so name the
    places worth checking rather than repeating the API's wording.
    """
    return (
        f"cannot post in the {label} channel ({channel_id}) - check that the channel "
        "still exists, that the bot's role has View Channel + Send Messages on it, and "
        "that no channel-level permission override denies either"
    )


class DiscordAdapter:
    name = "discord"
    max_post_chars = 1900  # Discord's hard cap is 2000

    def __init__(self, config: Config) -> None:
        self._c = config
        # Voice is never used; silence discord.py's one-time "voice will NOT be
        # supported" warnings (PyNaCl and davey are deliberately not installed).
        discord.VoiceClient.warn_nacl = False
        discord.VoiceClient.warn_dave = False
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        self._client = discord.Client(intents=intents)
        self._on_outbound = None
        self._ready = False

        @self._client.event
        async def on_ready() -> None:
            log.info("logged in as %s", self._client.user)
            self._ready = True

        @self._client.event
        async def on_message(message: discord.Message) -> None:
            if message.author.bot or message.guild is None:
                return
            if message.guild.id != self._c.discord_guild_id:
                return
            if self._on_outbound is None:
                return
            await self._on_outbound(self._to_outbound(message))

    # -- lifecycle -------------------------------------------------------

    async def start(self, on_outbound) -> None:
        self._on_outbound = on_outbound
        await self._client.start(self._c.discord_token)

    async def close(self) -> None:
        await self._client.close()

    def is_ready(self) -> bool:
        return self._client.is_ready()

    def latency_ms(self) -> float:
        return round((self._client.latency or 0) * 1000, 1)

    # -- translation -----------------------------------------------------

    def _to_outbound(self, message: discord.Message) -> OutboundMessage:
        return OutboundMessage(
            channel=ChannelRef(id=str(message.channel.id)),
            message=MessageRef(
                channel_id=str(message.channel.id), message_id=str(message.id)
            ),
            text=message.content or "",
            channel_topic=getattr(message.channel, "topic", None),
            attachments=tuple(
                Attachment(file_id=a.url, filename=a.filename, size=a.size)
                for a in message.attachments
            ),
        )

    @property
    def _guild(self) -> discord.Guild:
        guild = self._client.get_guild(self._c.discord_guild_id)
        if guild is None:
            raise RuntimeError(f"Bot is not in guild {self._c.discord_guild_id}")
        return guild

    # -- channels --------------------------------------------------------

    async def find_channel(self, number: str) -> ChannelRef | None:
        for ch in self._guild.text_channels:
            if number_from_topic(ch.topic) == number:
                return ChannelRef(id=str(ch.id))
        return None

    async def create_channel(self, number: str) -> ChannelRef:
        category = (
            self._guild.get_channel(self._c.discord_category_id)
            if self._c.discord_category_id
            else None
        )
        channel = await self._guild.create_text_channel(
            name=channel_name_for(number),
            topic=topic_for(number),
            category=category if isinstance(category, discord.CategoryChannel) else None,
            reason="New SMS contact",
        )
        await self.notify_inbox(f"New contact **{number}** -> {channel.mention}")
        log.info("created channel %s for %s", channel.name, number)
        return ChannelRef(id=str(channel.id))

    # -- messages --------------------------------------------------------

    async def post(
        self, channel: ChannelRef, text: str, files: Sequence[InboundFile] = ()
    ) -> MessageRef:
        target = self._client.get_channel(int(channel.id))
        if target is None:
            raise RuntimeError(access_hint(int(channel.id), "contact"))
        sent = await target.send(
            text,
            files=[
                discord.File(io.BytesIO(f.data), filename=f.filename) for f in files
            ],
        )
        return MessageRef(channel_id=channel.id, message_id=str(sent.id))

    async def _message(self, ref: MessageRef) -> discord.Message | None:
        channel = self._client.get_channel(int(ref.channel_id))
        if channel is None:
            return None
        try:
            return await channel.fetch_message(int(ref.message_id))
        except discord.NotFound:
            return None

    async def reply(self, ref: MessageRef, text: str) -> None:
        message = await self._message(ref)
        if message is not None:
            await message.reply(text, mention_author=False)

    async def react(self, ref: MessageRef, reaction: Reaction) -> None:
        message = await self._message(ref)
        if message is not None:
            await message.add_reaction(_EMOJI[reaction])

    async def unreact(self, ref: MessageRef, reaction: Reaction) -> None:
        message = await self._message(ref)
        if message is None:
            return
        try:
            await message.remove_reaction(_EMOJI[reaction], self._client.user)
        except discord.HTTPException:
            pass

    # -- secure channel --------------------------------------------------

    async def post_secure(self, text: str) -> tuple[SecureResult, str]:
        channel_id = self._c.discord_secure_channel_id
        if not channel_id:
            return SecureResult.NOT_CONFIGURED, ""

        channel = self._client.get_channel(channel_id)
        if channel is None:
            return SecureResult.UNAVAILABLE, access_hint(channel_id, "secure")

        try:
            await channel.send(text)
        except discord.Forbidden:
            return SecureResult.UNAVAILABLE, access_hint(channel_id, "secure")
        return SecureResult.DELIVERED, ""

    # -- misc ------------------------------------------------------------

    async def fetch_attachment(self, file_id: str) -> tuple[bytes, str]:
        """file_id is the CDN URL captured when the message arrived.

        Those URLs are signed and expire in roughly 24 hours, which is far longer
        than a token's 10-minute life, so no refresh is needed.
        """
        async with httpx.AsyncClient() as http:
            r = await http.get(file_id, follow_redirects=True, timeout=20)
            r.raise_for_status()
        ctype = r.headers.get("content-type", "application/octet-stream").split(";")[0]
        return r.content, ctype

    async def notify_inbox(self, text: str) -> None:
        """Best-effort operator notice. Never raises: callers use it on error paths."""
        channel_id = self._c.discord_inbox_channel_id
        inbox = self._client.get_channel(channel_id)
        if inbox is None:
            log.error("%s (wanted to report: %s)", access_hint(channel_id, "inbox"), text)
            return
        try:
            await inbox.send(text)
        except discord.Forbidden:
            log.error("%s (wanted to report: %s)", access_hint(channel_id, "inbox"), text)

    async def check_access(self) -> None:
        """Report unusable channels at startup instead of when a message needs them.

        The secure channel is the one that matters: nothing routine writes to it,
        so a permissions mistake there stays invisible until a passcode arrives.
        """
        targets = [(self._c.discord_inbox_channel_id, "inbox")]
        if self._c.discord_secure_channel_id:
            targets.append((self._c.discord_secure_channel_id, "secure"))
        for channel_id, label in targets:
            channel = self._client.get_channel(channel_id)
            if channel is None:
                log.error("startup check: %s", access_hint(channel_id, label))
                continue
            try:
                perms = channel.permissions_for(channel.guild.me)
            except Exception:  # noqa: BLE001
                log.warning("startup check: could not read permissions on %s channel", label)
                continue
            if perms.view_channel and perms.send_messages:
                log.info("startup check: %s channel #%s is writable", label, channel.name)
            else:
                log.error("startup check: %s", access_hint(channel_id, label))

    def strip_markup(self, text: str) -> str:
        return strip_markup(text)
