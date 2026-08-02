#!/usr/bin/env python3
"""
SignalWire <-> Discord SMS bridge.

One text channel per contact number inside a single private guild.
The channel *topic* holds the routing table ("sms:+14165550123"), so the
only persistent state is a tiny SQLite file for webhook de-duplication
and delivery-status reactions.

Run:  python sms_discord_bridge.py
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import logging
import os
import re
import signal
import sqlite3
import sys
import time
from typing import Optional
from urllib.parse import urlencode

import discord
import httpx
import uvicorn
from fastapi import FastAPI, Request, Response

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def _env(name: str, default: Optional[str] = None, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        sys.exit(f"Missing required env var: {name}")
    return val or ""


DISCORD_TOKEN = _env("DISCORD_TOKEN", required=True)
GUILD_ID = int(_env("DISCORD_GUILD_ID", required=True))
CATEGORY_ID = int(_env("DISCORD_CATEGORY_ID", "0") or 0)
INBOX_CHANNEL_ID = int(_env("DISCORD_INBOX_CHANNEL_ID", required=True))
# Optional: send anything that looks like a one-time passcode here instead.
SECURE_CHANNEL_ID = int(_env("DISCORD_SECURE_CHANNEL_ID", "0") or 0)
REDACT_CODES = _env("REDACT_CODES", "true").lower() in ("1", "true", "yes")

SW_SPACE = _env("SIGNALWIRE_SPACE_URL", required=True).replace("https://", "").strip("/")
SW_PROJECT = _env("SIGNALWIRE_PROJECT_ID", required=True)
SW_TOKEN = _env("SIGNALWIRE_API_TOKEN", required=True)
SW_NUMBER = _env("SIGNALWIRE_NUMBER", required=True)  # E.164, e.g. +14165550123

PUBLIC_BASE_URL = _env("PUBLIC_BASE_URL", required=True).rstrip("/")
BIND_HOST = _env("BIND_HOST", "0.0.0.0")  # 0.0.0.0 for containers; keep the port unpublished
BIND_PORT = int(_env("BIND_PORT", "8080"))
VERIFY_SIGNATURE = _env("VERIFY_SIGNATURE", "true").lower() in ("1", "true", "yes")

DB_PATH = _env("DB_PATH", "bridge.sqlite3")
HEARTBEAT_URL = _env("HEARTBEAT_URL", "")  # dead-man's-switch ping, optional
COMMAND_PREFIX = _env("COMMAND_PREFIX", "!sms")
NOTE_PREFIX = _env("NOTE_PREFIX", "//")

SW_API = f"https://{SW_SPACE}/api/laml/2010-04-01/Accounts/{SW_PROJECT}"
TOPIC_PREFIX = "sms:"
MAX_SMS_CHARS = 1500          # SignalWire hard cap is 1600 per API call
MAX_UPLOAD_BYTES = 8 * 1024 * 1024

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("bridge")

# --------------------------------------------------------------------------
# Tiny persistence layer
# --------------------------------------------------------------------------

_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.execute("CREATE TABLE IF NOT EXISTS seen (sid TEXT PRIMARY KEY, ts INTEGER)")
_db.execute(
    "CREATE TABLE IF NOT EXISTS outbound ("
    " sid TEXT PRIMARY KEY, channel_id INTEGER, message_id INTEGER, ts INTEGER)"
)
_db.commit()
_db_lock = asyncio.Lock()


def already_seen(sid: str) -> bool:
    """Insert-or-report. SignalWire retries webhooks; this makes them idempotent."""
    if not sid:
        return False
    try:
        _db.execute("INSERT INTO seen (sid, ts) VALUES (?, ?)", (sid, int(time.time())))
        _db.commit()
        return False
    except sqlite3.IntegrityError:
        return True


def remember_outbound(sid: str, channel_id: int, message_id: int) -> None:
    _db.execute(
        "INSERT OR REPLACE INTO outbound (sid, channel_id, message_id, ts) VALUES (?,?,?,?)",
        (sid, channel_id, message_id, int(time.time())),
    )
    _db.commit()


def lookup_outbound(sid: str) -> Optional[tuple[int, int]]:
    row = _db.execute(
        "SELECT channel_id, message_id FROM outbound WHERE sid = ?", (sid,)
    ).fetchone()
    return (row[0], row[1]) if row else None


def prune(days: int = 30) -> None:
    cutoff = int(time.time()) - days * 86400
    _db.execute("DELETE FROM seen WHERE ts < ?", (cutoff,))
    _db.execute("DELETE FROM outbound WHERE ts < ?", (cutoff,))
    _db.commit()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

E164 = re.compile(r"^\+[1-9]\d{7,14}$")

CODE_KEYWORDS = re.compile(
    r"\b(code|otp|passcode|password|verif\w*|2fa|authenticat\w*|token|pin)\b", re.I
)
CODE_DIGITS = re.compile(r"\b\d{4,8}\b")


def looks_like_a_code(body: str) -> bool:
    return bool(CODE_KEYWORDS.search(body) and CODE_DIGITS.search(body))


def channel_name_for(number: str) -> str:
    return "sms-" + re.sub(r"\D", "", number)


def topic_for(number: str) -> str:
    return f"{TOPIC_PREFIX}{number}"


def number_from_topic(topic: Optional[str]) -> Optional[str]:
    if not topic:
        return None
    for token in topic.split():
        if token.startswith(TOPIC_PREFIX):
            candidate = token[len(TOPIC_PREFIX):]
            if E164.match(candidate):
                return candidate
    return None


def normalise_number(raw: str) -> Optional[str]:
    """Accept 4165550123 / 14165550123 / +1 416-555-0123 -> +14165550123 (NANP)."""
    digits = re.sub(r"\D", "", raw)
    if raw.strip().startswith("+") and E164.match("+" + digits):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if 8 <= len(digits) <= 15:
        return "+" + digits
    return None


_CODEBLOCK = re.compile(r"```(?:[a-zA-Z0-9+-]*\n)?(.*?)```", re.S)
_INLINE = re.compile(r"`([^`]*)`")
_EMPHASIS = re.compile(r"(\*\*\*|\*\*|\*|___|__|_|~~|\|\|)(.+?)\1", re.S)
_CUSTOM_EMOJI = re.compile(r"<a?:([A-Za-z0-9_]+):\d+>")
_MENTION = re.compile(r"<[@#][!&]?\d+>")


def strip_discord_markup(text: str) -> str:
    """Discord formatting is literal text over SMS. Flatten it."""
    text = _CODEBLOCK.sub(lambda m: m.group(1).strip(), text)
    text = _INLINE.sub(r"\1", text)
    for _ in range(3):  # nested emphasis
        text = _EMPHASIS.sub(r"\2", text)
    text = _CUSTOM_EMOJI.sub(r":\1:", text)
    text = _MENTION.sub("", text)
    text = re.sub(r"^>\s?", "", text, flags=re.M)
    return text.strip()


GSM7 = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM7_EXT = set("^{}\\[~]|€")


def segment_count(body: str) -> int:
    if all(c in GSM7 or c in GSM7_EXT for c in body):
        length = sum(2 if c in GSM7_EXT else 1 for c in body)
        return 1 if length <= 160 else -(-length // 153)
    return 1 if len(body) <= 70 else -(-len(body) // 67)


def chunk(body: str, size: int = MAX_SMS_CHARS) -> list[str]:
    if len(body) <= size:
        return [body]
    out, cur = [], ""
    for word in body.split(" "):
        if len(cur) + len(word) + 1 > size:
            out.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        out.append(cur)
    return out


def valid_signature(url: str, params: dict[str, str], signature: str) -> bool:
    """Twilio-compatible request signature, as used by SignalWire's LaML API."""
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(SW_TOKEN.encode(), payload.encode("utf-8"), hashlib.sha1).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature or "")


# --------------------------------------------------------------------------
# Discord bot
# --------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

client = discord.Client(intents=intents)
inbound_queue: asyncio.Queue = asyncio.Queue()
http: httpx.AsyncClient  # set in setup


def find_channel(guild: discord.Guild, number: str) -> Optional[discord.TextChannel]:
    for ch in guild.text_channels:
        if number_from_topic(ch.topic) == number:
            return ch
    return None


async def get_or_create_channel(number: str) -> discord.TextChannel:
    guild = client.get_guild(GUILD_ID)
    if guild is None:
        raise RuntimeError(f"Bot is not in guild {GUILD_ID}")

    existing = find_channel(guild, number)
    if existing:
        return existing

    category = guild.get_channel(CATEGORY_ID) if CATEGORY_ID else None
    channel = await guild.create_text_channel(
        name=channel_name_for(number),
        topic=topic_for(number),
        category=category if isinstance(category, discord.CategoryChannel) else None,
        reason="New SMS contact",
    )
    inbox = client.get_channel(INBOX_CHANNEL_ID)
    if inbox:
        await inbox.send(f"New contact **{number}** -> {channel.mention}")
    log.info("created channel %s for %s", channel.name, number)
    return channel


async def fetch_media(url: str) -> Optional[tuple[bytes, str]]:
    try:
        r = await http.get(url, auth=(SW_PROJECT, SW_TOKEN), follow_redirects=True)
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.warning("media fetch failed %s: %s", url, exc)
        return None
    if len(r.content) > MAX_UPLOAD_BYTES:
        return None
    ctype = r.headers.get("content-type", "application/octet-stream").split(";")[0]
    ext = {
        "image/jpeg": "jpg", "image/png": "png", "image/gif": "gif",
        "image/webp": "webp", "video/mp4": "mp4", "audio/mpeg": "mp3",
        "audio/amr": "amr", "application/pdf": "pdf", "text/vcard": "vcf",
    }.get(ctype, "bin")
    return r.content, f"mms.{ext}"


async def deliver_inbound(payload: dict) -> None:
    number = payload["from"]
    body = payload["body"]
    media = payload["media"]

    if REDACT_CODES and looks_like_a_code(body):
        secure = client.get_channel(SECURE_CHANNEL_ID) if SECURE_CHANNEL_ID else None
        if secure is not None:
            # Dedicated locked-down channel: keep the code, keep it out of the thread.
            await secure.send(f"**{number}**\n```{body}```")
        else:
            # No secure channel configured: don't write the code to Discord at all.
            channel = await get_or_create_channel(number)
            await channel.send(
                "_(message contained a passcode — suppressed. Read it in the "
                "SignalWire message logs.)_"
            )
        return

    channel = await get_or_create_channel(number)

    files = []
    for url in media:
        got = await fetch_media(url)
        if got:
            data, filename = got
            files.append(discord.File(io.BytesIO(data), filename=filename))
        else:
            body = (body + f"\n_(attachment too large or unfetchable: {url})_").strip()

    content = body or "_(no text body)_"
    for piece in [content[i:i + 1900] for i in range(0, len(content), 1900)]:
        await channel.send(piece, files=files)
        files = []  # only attach to the first chunk


async def inbound_worker() -> None:
    while True:
        payload = await inbound_queue.get()
        try:
            await deliver_inbound(payload)
        except Exception:  # noqa: BLE001
            log.exception("failed to deliver inbound message")
            inbox = client.get_channel(INBOX_CHANNEL_ID)
            if inbox:
                await inbox.send(
                    f"Failed to deliver inbound SMS from {payload.get('from')}. Check logs."
                )
        finally:
            inbound_queue.task_done()


async def send_sms(to: str, body: str) -> str:
    data = {
        "From": SW_NUMBER,
        "To": to,
        "Body": body,
        "StatusCallback": f"{PUBLIC_BASE_URL}/sms/status",
    }
    r = await http.post(
        f"{SW_API}/Messages.json",
        data=data,
        auth=(SW_PROJECT, SW_TOKEN),
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["sid"]


async def handle_outbound(message: discord.Message, to: str, body: str) -> None:
    body = strip_discord_markup(body)

    for attachment in message.attachments:
        body = f"{body}\n{attachment.url}".strip()

    if not body:
        return

    segs = segment_count(body)
    if segs > 1:
        log.info("outbound to %s is %d segments", to, segs)

    await message.add_reaction("\N{HOURGLASS WITH FLOWING SAND}")
    try:
        for piece in chunk(body):
            sid = await send_sms(to, piece)
            remember_outbound(sid, message.channel.id, message.id)
    except Exception as exc:  # noqa: BLE001
        log.exception("send failed")
        await message.remove_reaction("\N{HOURGLASS WITH FLOWING SAND}", client.user)
        await message.add_reaction("\N{CROSS MARK}")
        await message.reply(f"Send failed: `{exc}`", mention_author=False)


_tasks_started = False


@client.event
async def on_ready() -> None:
    global _tasks_started
    log.info("logged in as %s", client.user)
    if not _tasks_started:  # on_ready fires again after every reconnect
        _tasks_started = True
        asyncio.create_task(inbound_worker())
        if HEARTBEAT_URL:
            asyncio.create_task(heartbeat_loop())
        prune()


@client.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or message.guild is None or message.guild.id != GUILD_ID:
        return

    content = message.content or ""

    # Internal note: visible in Discord, never sent.
    if content.startswith(NOTE_PREFIX):
        return

    # !sms +14165550123 hello -> start a new conversation from any channel
    if content.startswith(COMMAND_PREFIX):
        parts = content[len(COMMAND_PREFIX):].strip().split(None, 1)
        if not parts:
            await message.reply(f"Usage: `{COMMAND_PREFIX} +14165550123 message`",
                                mention_author=False)
            return
        number = normalise_number(parts[0])
        if not number:
            await message.reply("That doesn't look like a phone number.",
                                mention_author=False)
            return
        channel = await get_or_create_channel(number)
        if len(parts) > 1 and parts[1].strip():
            await handle_outbound(message, number, parts[1])
        else:
            await message.reply(f"Channel ready: {channel.mention}", mention_author=False)
        return

    to = number_from_topic(getattr(message.channel, "topic", None))
    if not to:
        return  # not a contact channel

    await handle_outbound(message, to, content)


async def update_reaction(sid: str, status: str, error_code: str) -> None:
    ref = lookup_outbound(sid)
    if not ref:
        return
    channel_id, message_id = ref
    channel = client.get_channel(channel_id)
    if channel is None:
        return
    try:
        msg = await channel.fetch_message(message_id)
    except discord.NotFound:
        return

    hourglass = "\N{HOURGLASS WITH FLOWING SAND}"
    if status == "delivered":
        try:
            await msg.remove_reaction(hourglass, client.user)
        except discord.HTTPException:
            pass
        await msg.add_reaction("\N{WHITE HEAVY CHECK MARK}")
    elif status in ("failed", "undelivered"):
        try:
            await msg.remove_reaction(hourglass, client.user)
        except discord.HTTPException:
            pass
        await msg.add_reaction("\N{CROSS MARK}")
        await msg.reply(
            f"Carrier reported `{status}`" + (f" (error {error_code})" if error_code else ""),
            mention_author=False,
        )


async def heartbeat_loop() -> None:
    while True:
        try:
            await http.get(HEARTBEAT_URL, timeout=10)
        except Exception as exc:  # noqa: BLE001
            log.warning("heartbeat failed: %s", exc)
        await asyncio.sleep(300)


# --------------------------------------------------------------------------
# Webhook server
# --------------------------------------------------------------------------

api = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


async def _check(request: Request, path: str) -> Optional[dict[str, str]]:
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    if VERIFY_SIGNATURE:
        signature = request.headers.get("X-Twilio-Signature", "")
        if not valid_signature(f"{PUBLIC_BASE_URL}{path}", params, signature):
            log.warning("rejected request with bad signature on %s", path)
            return None
    return params


@api.post("/sms/inbound")
async def inbound(request: Request) -> Response:
    params = await _check(request, "/sms/inbound")
    if params is None:
        return Response(status_code=403)

    sid = params.get("MessageSid", "")
    if already_seen(sid):
        log.info("duplicate webhook for %s, ignoring", sid)
        return Response(content="<Response/>", media_type="application/xml")

    media = []
    for i in range(int(params.get("NumMedia", "0") or 0)):
        url = params.get(f"MediaUrl{i}")
        if url:
            media.append(url)

    await inbound_queue.put(
        {
            "from": params.get("From", ""),
            "to": params.get("To", ""),
            "body": (params.get("Body") or "").strip(),
            "media": media,
            "sid": sid,
        }
    )
    # Answer immediately; the bot does the slow work off the request path.
    return Response(content="<Response/>", media_type="application/xml")


@api.post("/sms/status")
async def status(request: Request) -> Response:
    params = await _check(request, "/sms/status")
    if params is None:
        return Response(status_code=403)
    asyncio.create_task(
        update_reaction(
            params.get("MessageSid", ""),
            (params.get("MessageStatus") or "").lower(),
            params.get("ErrorCode", ""),
        )
    )
    return Response(status_code=204)


@api.get("/healthz")
async def healthz() -> dict:
    return {
        "ok": client.is_ready(),
        "queued": inbound_queue.qsize(),
        "latency_ms": round((client.latency or 0) * 1000, 1),
    }


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

async def main() -> None:
    global http
    http = httpx.AsyncClient()

    config = uvicorn.Config(
        api, host=BIND_HOST, port=BIND_PORT, log_level="warning", access_log=False
    )
    server = uvicorn.Server(config)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # e.g. Windows
            pass

    async with client:
        asyncio.create_task(server.serve())
        log.info("webhook listening on %s:%s (public: %s)", BIND_HOST, BIND_PORT, PUBLIC_BASE_URL)
        asyncio.create_task(client.start(DISCORD_TOKEN))
        await stop.wait()
        log.info("shutdown signal received, closing")
        server.should_exit = True
        await http.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
