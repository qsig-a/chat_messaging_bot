"""Environment parsing.

Required variables depend on CHAT_PLATFORM: only the selected platform's
variables are demanded, and the error names the platform that wanted them.

CHAT_PLATFORM defaults to "discord" so pre-split .env files keep working. This
does not contradict the rule that SIGNALWIRE_SIGNING_KEY must never default: a
wrong signing key fails asymmetrically and silently (sending works, receiving
403s), whereas a wrong platform exits at startup naming the missing variable.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from typing import Mapping

PLATFORMS = ("discord", "slack")
DEFAULT_MAX_MMS_BYTES = 1024 * 1024


class ConfigError(Exception):
    """Configuration is unusable. __main__ turns this into a startup exit."""


@dataclass(frozen=True)
class Config:
    platform: str

    discord_token: str
    discord_guild_id: int
    discord_category_id: int
    discord_inbox_channel_id: int
    discord_secure_channel_id: int

    slack_bot_token: str
    slack_app_token: str
    slack_inbox_channel_id: str
    slack_secure_channel_id: str
    slack_invite_users: tuple[str, ...]

    sw_space: str
    sw_project: str
    sw_token: str
    sw_signing_key: str
    sw_number: str

    public_base_url: str
    bind_host: str
    bind_port: int
    verify_signature: bool
    redact_codes: bool
    db_path: str
    heartbeat_url: str
    command_prefix: str
    note_prefix: str
    media_signing_key: bytes
    max_mms_bytes: int

    @property
    def sw_api_base(self) -> str:
        return f"https://{self.sw_space}/api/laml/2010-04-01/Accounts/{self.sw_project}"


def _flag(env: Mapping[str, str], name: str, default: str = "true") -> bool:
    return env.get(name, default).strip().lower() in ("1", "true", "yes")


def load(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    missing: list[str] = []

    platform = env.get("CHAT_PLATFORM", "discord").strip().lower()
    if platform not in PLATFORMS:
        raise ConfigError(
            f"CHAT_PLATFORM={platform!r} is not supported; expected one of "
            f"{', '.join(PLATFORMS)}"
        )

    def need(name: str, owner: str = "") -> str:
        value = (env.get(name) or "").strip()
        if not value:
            missing.append(f"{name} (required when CHAT_PLATFORM={owner})" if owner else name)
        return value

    sw_space = need("SIGNALWIRE_SPACE_URL").replace("https://", "").strip("/")
    sw_project = need("SIGNALWIRE_PROJECT_ID")
    sw_token = need("SIGNALWIRE_API_TOKEN")
    sw_signing_key = need("SIGNALWIRE_SIGNING_KEY")
    sw_number = need("SIGNALWIRE_NUMBER")
    public_base_url = need("PUBLIC_BASE_URL").rstrip("/")

    discord_token = need("DISCORD_TOKEN", "discord") if platform == "discord" else ""
    discord_guild = need("DISCORD_GUILD_ID", "discord") if platform == "discord" else "0"
    discord_inbox = need("DISCORD_INBOX_CHANNEL_ID", "discord") if platform == "discord" else "0"

    slack_bot = need("SLACK_BOT_TOKEN", "slack") if platform == "slack" else ""
    slack_app = need("SLACK_APP_TOKEN", "slack") if platform == "slack" else ""
    slack_inbox = need("SLACK_INBOX_CHANNEL_ID", "slack") if platform == "slack" else ""

    if missing:
        raise ConfigError("Missing required env vars: " + ", ".join(missing))

    raw_key = (env.get("MEDIA_SIGNING_KEY") or "").strip()
    media_key = raw_key.encode() if raw_key else secrets.token_bytes(32)

    invite = tuple(
        u.strip() for u in (env.get("SLACK_INVITE_USERS") or "").split(",") if u.strip()
    )

    return Config(
        platform=platform,
        discord_token=discord_token,
        discord_guild_id=int(discord_guild or 0),
        discord_category_id=int(env.get("DISCORD_CATEGORY_ID") or 0),
        discord_inbox_channel_id=int(discord_inbox or 0),
        discord_secure_channel_id=int(env.get("DISCORD_SECURE_CHANNEL_ID") or 0),
        slack_bot_token=slack_bot,
        slack_app_token=slack_app,
        slack_inbox_channel_id=slack_inbox,
        slack_secure_channel_id=(env.get("SLACK_SECURE_CHANNEL_ID") or "").strip(),
        slack_invite_users=invite,
        sw_space=sw_space,
        sw_project=sw_project,
        sw_token=sw_token,
        sw_signing_key=sw_signing_key,
        sw_number=sw_number,
        public_base_url=public_base_url,
        bind_host=env.get("BIND_HOST", "0.0.0.0"),
        bind_port=int(env.get("BIND_PORT") or 8080),
        verify_signature=_flag(env, "VERIFY_SIGNATURE"),
        redact_codes=_flag(env, "REDACT_CODES"),
        db_path=env.get("DB_PATH", "bridge.sqlite3"),
        heartbeat_url=(env.get("HEARTBEAT_URL") or "").strip(),
        command_prefix=env.get("COMMAND_PREFIX", "!sms"),
        note_prefix=env.get("NOTE_PREFIX", "//"),
        media_signing_key=media_key,
        max_mms_bytes=int(env.get("MAX_MMS_BYTES") or DEFAULT_MAX_MMS_BYTES),
    )
