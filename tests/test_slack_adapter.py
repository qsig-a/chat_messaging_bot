"""Slack adapter behaviour that does not need a network.

Two areas earn coverage here: the event filter, which decides what becomes an
outbound SMS, and post_secure, which feeds the passcode decision table.
"""

import pytest
from slack_sdk.errors import SlackApiError

from sms_bridge.chat.base import Reaction, SecureResult
from sms_bridge.chat.slack import SlackAdapter, access_hint
from sms_bridge.config import load

ENV = {
    "CHAT_PLATFORM": "slack",
    "SLACK_BOT_TOKEN": "xoxb-test",
    "SLACK_APP_TOKEN": "xapp-test",
    "SLACK_INBOX_CHANNEL_ID": "C-inbox",
    "SIGNALWIRE_SPACE_URL": "https://example.signalwire.com",
    "SIGNALWIRE_PROJECT_ID": "proj",
    "SIGNALWIRE_API_TOKEN": "tok",
    "SIGNALWIRE_SIGNING_KEY": "sign",
    "SIGNALWIRE_NUMBER": "+14165550100",
    "PUBLIC_BASE_URL": "https://sms.example.com",
}

BOT_USER = "U-bot"


class FakeResponse(dict):
    """Slack responses support both mapping access and .data."""

    @property
    def data(self):
        return dict(self)


class FakeWeb:
    """Records calls and returns canned responses."""

    def __init__(self, **errors: str):
        self.errors = errors           # method name -> Slack error code to raise
        self.calls: list[tuple[str, dict]] = []
        self.topics: dict[str, str] = {}
        self.posted: list[tuple[str, str]] = []

    def _maybe_raise(self, method: str) -> None:
        if method in self.errors:
            raise SlackApiError(
                f"{method} failed", FakeResponse({"ok": False, "error": self.errors[method]})
            )

    async def auth_test(self):
        return FakeResponse({"user_id": BOT_USER, "user": "smsbot"})

    async def chat_postMessage(self, channel, text, thread_ts=None):
        self.calls.append(("chat_postMessage", {"channel": channel, "text": text}))
        self._maybe_raise("chat_postMessage")
        self.posted.append((channel, text))
        return FakeResponse({"ts": "1699999999.000100", "channel": channel})

    async def conversations_create(self, name, is_private):
        self.calls.append(("conversations_create", {"name": name, "is_private": is_private}))
        self._maybe_raise("conversations_create")
        return FakeResponse({"channel": {"id": "C-new"}})

    async def conversations_setTopic(self, channel, topic):
        self.calls.append(("conversations_setTopic", {"channel": channel, "topic": topic}))
        self.topics[channel] = topic
        return FakeResponse({"ok": True})

    async def conversations_invite(self, channel, users):
        self.calls.append(("conversations_invite", {"channel": channel, "users": users}))
        self._maybe_raise("conversations_invite")
        return FakeResponse({"ok": True})

    async def conversations_info(self, channel):
        self.calls.append(("conversations_info", {"channel": channel}))
        self._maybe_raise("conversations_info")
        return FakeResponse(
            {"channel": {"id": channel, "name": "sms-x", "is_member": True,
                         "topic": {"value": self.topics.get(channel, "")}}}
        )

    async def conversations_list(self, **kwargs):
        self.calls.append(("conversations_list", kwargs))
        return FakeResponse({"channels": [], "response_metadata": {"next_cursor": ""}})

    async def reactions_add(self, channel, timestamp, name):
        self.calls.append(("reactions_add", {"name": name, "timestamp": timestamp}))
        self._maybe_raise("reactions_add")
        return FakeResponse({"ok": True})

    async def reactions_remove(self, channel, timestamp, name):
        self.calls.append(("reactions_remove", {"name": name, "timestamp": timestamp}))
        self._maybe_raise("reactions_remove")
        return FakeResponse({"ok": True})


def build(env_extra=None, **errors):
    cfg = load({**ENV, **(env_extra or {})})
    web = FakeWeb(**errors)
    adapter = SlackAdapter(cfg, web=web, socket=object())
    adapter._bot_user_id = BOT_USER
    return adapter, web


def collector():
    seen = []

    async def on_outbound(msg):
        seen.append(msg)

    return seen, on_outbound


# --------------------------------------------------------------------------
# Event filtering - what becomes an outbound SMS
# --------------------------------------------------------------------------

async def test_top_level_user_message_is_dispatched():
    adapter, web = build()
    seen, handler = collector()
    adapter._on_outbound = handler
    web.topics["C1"] = "sms:+14165550123"

    await adapter._dispatch(
        {"type": "message", "channel": "C1", "user": "U-human",
         "ts": "1.1", "text": "hello"}
    )

    assert len(seen) == 1
    assert seen[0].text == "hello"
    assert seen[0].channel_topic == "sms:+14165550123"
    assert seen[0].message.message_id == "1.1"


async def test_bot_messages_are_ignored():
    adapter, _ = build()
    seen, handler = collector()
    adapter._on_outbound = handler

    await adapter._dispatch(
        {"type": "message", "channel": "C1", "bot_id": "B1", "ts": "1.1", "text": "hi"}
    )

    assert seen == []


async def test_the_apps_own_messages_are_ignored():
    adapter, _ = build()
    seen, handler = collector()
    adapter._on_outbound = handler

    await adapter._dispatch(
        {"type": "message", "channel": "C1", "user": BOT_USER, "ts": "1.1", "text": "hi"}
    )

    assert seen == []


@pytest.mark.parametrize(
    "subtype", ["message_changed", "message_deleted", "channel_join", "bot_message"]
)
async def test_ignored_subtypes(subtype):
    adapter, _ = build()
    seen, handler = collector()
    adapter._on_outbound = handler

    await adapter._dispatch(
        {"type": "message", "subtype": subtype, "channel": "C1",
         "user": "U-human", "ts": "1.1", "text": "hi"}
    )

    assert seen == []


async def test_thread_replies_are_never_sent():
    """Threads are the in-workspace discussion space, not outbound traffic."""
    adapter, _ = build()
    seen, handler = collector()
    adapter._on_outbound = handler

    await adapter._dispatch(
        {"type": "message", "channel": "C1", "user": "U-human",
         "ts": "2.2", "thread_ts": "1.1", "text": "just a note"}
    )

    assert seen == []


async def test_a_thread_parent_is_still_sent():
    """A message that started a thread has thread_ts == ts and must still send."""
    adapter, web = build()
    seen, handler = collector()
    adapter._on_outbound = handler
    web.topics["C1"] = "sms:+14165550123"

    await adapter._dispatch(
        {"type": "message", "channel": "C1", "user": "U-human",
         "ts": "1.1", "thread_ts": "1.1", "text": "hi"}
    )

    assert len(seen) == 1


async def test_files_become_attachments():
    adapter, web = build()
    seen, handler = collector()
    adapter._on_outbound = handler
    web.topics["C1"] = "sms:+14165550123"

    await adapter._dispatch(
        {"type": "message", "channel": "C1", "user": "U-human", "ts": "1.1",
         "text": "look", "files": [{"id": "F1", "name": "a.png", "size": 4096}]}
    )

    attachment = seen[0].attachments[0]
    assert (attachment.file_id, attachment.filename, attachment.size) == ("F1", "a.png", 4096)


async def test_channel_events_update_the_index_not_the_handler():
    adapter, _ = build()
    seen, handler = collector()
    adapter._on_outbound = handler

    await adapter._dispatch(
        {"type": "channel_created",
         "channel": {"id": "C9", "topic": {"value": "sms:+14165550199"}}}
    )

    assert seen == []
    assert await adapter._index.lookup("+14165550199") == "C9"


# --------------------------------------------------------------------------
# post_secure - feeds the passcode decision table
# --------------------------------------------------------------------------

async def test_post_secure_not_configured():
    adapter, web = build()

    result, hint = await adapter.post_secure("code 1234")

    assert result is SecureResult.NOT_CONFIGURED
    assert hint == ""
    assert web.posted == []


async def test_post_secure_delivered():
    adapter, web = build(env_extra={"SLACK_SECURE_CHANNEL_ID": "C-secure"})

    result, hint = await adapter.post_secure("code 1234")

    assert result is SecureResult.DELIVERED
    assert hint == ""
    assert web.posted == [("C-secure", "code 1234")]


async def test_post_secure_unavailable_carries_an_actionable_hint():
    adapter, _ = build(
        env_extra={"SLACK_SECURE_CHANNEL_ID": "C-secure"},
        chat_postMessage="not_in_channel",
    )

    result, hint = await adapter.post_secure("code 1234")

    assert result is SecureResult.UNAVAILABLE
    assert "/invite" in hint
    assert "C-secure" in hint


async def test_post_secure_never_raises_on_a_slack_error():
    """Delivery relies on a returned result, not an exception."""
    adapter, _ = build(
        env_extra={"SLACK_SECURE_CHANNEL_ID": "C-secure"},
        chat_postMessage="missing_scope",
    )

    result, _ = await adapter.post_secure("code")

    assert result is SecureResult.UNAVAILABLE


@pytest.mark.parametrize(
    "error,expected",
    [
        ("channel_not_found", "the channel id is wrong"),
        ("not_in_channel", "/invite"),
        ("is_archived", "archived"),
        ("missing_scope", "OAuth scope"),
        ("something_new", "something_new"),
    ],
)
def test_access_hint_maps_slack_errors(error, expected):
    assert expected in access_hint("C1", "secure", error)


# --------------------------------------------------------------------------
# Channels and reactions
# --------------------------------------------------------------------------

async def test_create_channel_is_private_sets_topic_and_indexes():
    adapter, web = build()

    ref = await adapter.create_channel("+14165550123")

    created = dict(web.calls)["conversations_create"]
    assert created["is_private"] is True
    assert created["name"] == "sms-14165550123"
    assert web.topics["C-new"] == "sms:+14165550123"
    assert await adapter._index.lookup("+14165550123") == "C-new"
    assert ref.id == "C-new"


async def test_create_channel_invites_configured_operators():
    adapter, web = build(env_extra={"SLACK_INVITE_USERS": "U1,U2"})

    await adapter.create_channel("+14165550123")

    assert dict(web.calls)["conversations_invite"]["users"] == "U1,U2"


async def test_create_channel_survives_a_failed_invite():
    """A bad member id must not cost us the channel."""
    adapter, web = build(
        env_extra={"SLACK_INVITE_USERS": "U-bogus"}, conversations_invite="user_not_found"
    )

    ref = await adapter.create_channel("+14165550123")

    assert ref.id == "C-new"


async def test_react_ignores_already_reacted():
    adapter, _ = build(reactions_add="already_reacted")
    from sms_bridge.chat.base import MessageRef

    await adapter.react(MessageRef("C1", "1.1"), Reaction.OK)  # must not raise


async def test_react_reraises_unexpected_errors():
    adapter, _ = build(reactions_add="missing_scope")
    from sms_bridge.chat.base import MessageRef

    with pytest.raises(SlackApiError):
        await adapter.react(MessageRef("C1", "1.1"), Reaction.OK)


async def test_unreact_ignores_no_reaction():
    adapter, _ = build(reactions_remove="no_reaction")
    from sms_bridge.chat.base import MessageRef

    await adapter.unreact(MessageRef("C1", "1.1"), Reaction.PENDING)  # must not raise


async def test_notify_inbox_never_raises():
    """Callers use it on error paths, so it must swallow everything."""
    adapter, _ = build(chat_postMessage="channel_not_found")

    await adapter.notify_inbox("something broke")  # must not raise
