"""Test environment bootstrap.

sms_discord_bridge.py resolves its configuration and opens SQLite at import
time, exiting on any missing required variable. These values must therefore be
in os.environ before the module is imported anywhere in the test run. pytest
imports conftest.py before collecting test modules, so this is the right place.
"""

import os

_TEST_ENV = {
    "DISCORD_TOKEN": "test-discord-token",
    "DISCORD_GUILD_ID": "1",
    "DISCORD_INBOX_CHANNEL_ID": "2",
    "SIGNALWIRE_SPACE_URL": "https://example.signalwire.com",
    "SIGNALWIRE_PROJECT_ID": "test-project",
    "SIGNALWIRE_API_TOKEN": "test-api-token",
    "SIGNALWIRE_SIGNING_KEY": "test-signing-key",
    "SIGNALWIRE_NUMBER": "+14165550100",
    "PUBLIC_BASE_URL": "https://sms.example.com",
    "DB_PATH": ":memory:",
}
for _key, _value in _TEST_ENV.items():
    os.environ.setdefault(_key, _value)

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def bridge():
    """The module under test. Imported lazily so the env above is applied first."""
    import sms_discord_bridge

    return sms_discord_bridge
