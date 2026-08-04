"""Environment parsing and per-platform requirements."""

import pytest

from sms_bridge.config import ConfigError, load

COMMON = {
    "SIGNALWIRE_SPACE_URL": "https://example.signalwire.com",
    "SIGNALWIRE_PROJECT_ID": "proj",
    "SIGNALWIRE_API_TOKEN": "tok",
    "SIGNALWIRE_SIGNING_KEY": "sign",
    "SIGNALWIRE_NUMBER": "+14165550100",
    "PUBLIC_BASE_URL": "https://sms.example.com",
}
DISCORD = {
    "DISCORD_TOKEN": "dt",
    "DISCORD_GUILD_ID": "1",
    "DISCORD_INBOX_CHANNEL_ID": "2",
}
SLACK = {
    "SLACK_BOT_TOKEN": "xoxb-1",
    "SLACK_APP_TOKEN": "xapp-1",
    "SLACK_INBOX_CHANNEL_ID": "C123",
}


def test_platform_defaults_to_discord():
    cfg = load({**COMMON, **DISCORD})
    assert cfg.platform == "discord"


def test_slack_platform_requires_slack_vars_only():
    cfg = load({**COMMON, **SLACK, "CHAT_PLATFORM": "slack"})
    assert cfg.platform == "slack"
    assert cfg.slack_bot_token == "xoxb-1"
    assert cfg.discord_token == ""


def test_missing_common_var_raises():
    env = {**COMMON, **DISCORD}
    del env["SIGNALWIRE_SIGNING_KEY"]
    with pytest.raises(ConfigError) as exc:
        load(env)
    assert "SIGNALWIRE_SIGNING_KEY" in str(exc.value)


def test_missing_platform_var_names_the_platform():
    """The error has to say which platform demanded the variable."""
    env = {**COMMON, **DISCORD}
    del env["DISCORD_GUILD_ID"]
    with pytest.raises(ConfigError) as exc:
        load(env)
    assert "DISCORD_GUILD_ID" in str(exc.value)
    assert "discord" in str(exc.value).lower()


def test_slack_vars_not_required_under_discord():
    load({**COMMON, **DISCORD})  # must not raise


def test_discord_vars_not_required_under_slack():
    load({**COMMON, **SLACK, "CHAT_PLATFORM": "slack"})  # must not raise


def test_unknown_platform_rejected():
    with pytest.raises(ConfigError) as exc:
        load({**COMMON, "CHAT_PLATFORM": "irc"})
    assert "irc" in str(exc.value)


def test_space_url_is_normalised():
    cfg = load({**COMMON, **DISCORD, "SIGNALWIRE_SPACE_URL": "https://a.signalwire.com/"})
    assert cfg.sw_space == "a.signalwire.com"


def test_public_base_url_loses_trailing_slash():
    cfg = load({**COMMON, **DISCORD, "PUBLIC_BASE_URL": "https://sms.example.com/"})
    assert cfg.public_base_url == "https://sms.example.com"


def test_invite_users_parsed_and_trimmed():
    cfg = load({**COMMON, **SLACK, "CHAT_PLATFORM": "slack",
                "SLACK_INVITE_USERS": "U1, U2 ,U3"})
    assert cfg.slack_invite_users == ("U1", "U2", "U3")


def test_invite_users_empty_by_default():
    cfg = load({**COMMON, **SLACK, "CHAT_PLATFORM": "slack"})
    assert cfg.slack_invite_users == ()


def test_media_signing_key_is_random_when_unset():
    a = load({**COMMON, **DISCORD})
    b = load({**COMMON, **DISCORD})
    assert a.media_signing_key != b.media_signing_key
    assert len(a.media_signing_key) >= 32


def test_media_signing_key_honoured_when_set():
    cfg = load({**COMMON, **DISCORD, "MEDIA_SIGNING_KEY": "shared-secret"})
    assert cfg.media_signing_key == b"shared-secret"


@pytest.mark.parametrize("raw,expected", [("true", True), ("1", True), ("yes", True),
                                          ("false", False), ("0", False), ("no", False)])
def test_boolean_parsing(raw, expected):
    cfg = load({**COMMON, **DISCORD, "REDACT_CODES": raw})
    assert cfg.redact_codes is expected


def test_defaults():
    cfg = load({**COMMON, **DISCORD})
    assert cfg.bind_host == "0.0.0.0"
    assert cfg.bind_port == 8080
    assert cfg.verify_signature is True
    assert cfg.redact_codes is True
    assert cfg.command_prefix == "!sms"
    assert cfg.note_prefix == "//"
    assert cfg.max_mms_bytes == 1048576


def test_non_numeric_int_raises_config_error_not_value_error():
    """A bad port must fail like every other config error, not as a traceback."""
    with pytest.raises(ConfigError) as exc:
        load({**COMMON, **DISCORD, "BIND_PORT": "notanumber"})
    assert "BIND_PORT" in str(exc.value)


def test_non_numeric_guild_id_raises_config_error():
    with pytest.raises(ConfigError) as exc:
        load({**COMMON, **DISCORD, "DISCORD_GUILD_ID": "abc"})
    assert "DISCORD_GUILD_ID" in str(exc.value)


def test_config_error_exposes_message():
    with pytest.raises(ConfigError) as exc:
        load({**COMMON, "CHAT_PLATFORM": "irc"})
    assert exc.value.message == str(exc.value)


def test_http_space_url_is_normalised():
    cfg = load({**COMMON, **DISCORD, "SIGNALWIRE_SPACE_URL": "http://a.signalwire.com/"})
    assert cfg.sw_space == "a.signalwire.com"
    assert cfg.sw_api_base.startswith("https://a.signalwire.com/api/laml/")


def test_blank_optional_int_falls_back_to_default():
    cfg = load({**COMMON, **DISCORD, "BIND_PORT": "", "MAX_MMS_BYTES": ""})
    assert cfg.bind_port == 8080
    assert cfg.max_mms_bytes == 1048576
