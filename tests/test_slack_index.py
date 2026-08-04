"""In-memory topic -> channel index for Slack."""

import pytest

from sms_bridge.chat.slack_index import ChannelIndex


def make_lister(pages):
    """pages: list of (channels, next_cursor) tuples."""
    calls = []

    async def lister(cursor=None):
        calls.append(cursor)
        channels, next_cursor = pages[len(calls) - 1]
        return {
            "channels": channels,
            "response_metadata": {"next_cursor": next_cursor or ""},
        }

    lister.calls = calls
    return lister


def ch(cid, topic):
    return {"id": cid, "topic": {"value": topic}}


async def test_refresh_indexes_topics():
    lister = make_lister([([ch("C1", "sms:+14165550123")], None)])
    index = ChannelIndex(lister)

    await index.refresh()

    assert await index.lookup("+14165550123") == "C1"


async def test_refresh_follows_pagination():
    lister = make_lister([
        ([ch("C1", "sms:+14165550101")], "cursor2"),
        ([ch("C2", "sms:+14165550102")], None),
    ])
    index = ChannelIndex(lister)

    await index.refresh()

    assert await index.lookup("+14165550101") == "C1"
    assert await index.lookup("+14165550102") == "C2"
    assert lister.calls == [None, "cursor2"]


async def test_channels_without_a_topic_token_are_ignored():
    lister = make_lister([([ch("C1", "general chat"), ch("C2", "")], None)])
    index = ChannelIndex(lister)

    # No explicit refresh() here: lookup()'s own on-demand refresh (proven by
    # test_lookup_miss_triggers_exactly_one_refresh) consumes the single page
    # above. An explicit refresh() first would exhaust it, then the miss below
    # would trigger a second refresh with no page left to serve it.
    assert await index.lookup("+14165550123") is None


async def test_lookup_miss_triggers_exactly_one_refresh():
    lister = make_lister([
        ([], None),
        ([ch("C9", "sms:+14165550199")], None),
    ])
    index = ChannelIndex(lister)
    await index.refresh()          # call 1: empty

    found = await index.lookup("+14165550199")  # call 2: refresh on miss

    assert found == "C9"
    assert len(lister.calls) == 2


async def test_second_consecutive_miss_does_not_refresh_again():
    """A number with no channel must not cost a conversations.list per message."""
    lister = make_lister([([], None), ([], None), ([], None)])
    index = ChannelIndex(lister)
    await index.refresh()

    assert await index.lookup("+14165550199") is None
    calls_after_first_miss = len(lister.calls)
    assert await index.lookup("+14165550199") is None

    assert len(lister.calls) == calls_after_first_miss


async def test_remember_makes_a_new_channel_findable_without_a_refresh():
    lister = make_lister([([], None)])
    index = ChannelIndex(lister)
    await index.refresh()

    index.remember("+14165550123", "C5")

    assert await index.lookup("+14165550123") == "C5"
    assert len(lister.calls) == 1


async def test_forget_removes_by_channel_id():
    lister = make_lister([([ch("C1", "sms:+14165550123")], None), ([], None)])
    index = ChannelIndex(lister)
    await index.refresh()

    index.forget("C1")

    assert await index.lookup("+14165550123") is None


@pytest.mark.parametrize("event_type", ["channel_created", "channel_rename"])
async def test_create_and_rename_events_update_the_index(event_type):
    lister = make_lister([([], None)])
    index = ChannelIndex(lister)
    await index.refresh()

    index.apply_event({
        "type": event_type,
        "channel": {"id": "C7", "topic": {"value": "sms:+14165550177"}},
    })

    assert await index.lookup("+14165550177") == "C7"


async def test_archive_event_removes_the_channel():
    lister = make_lister([([ch("C1", "sms:+14165550123")], None), ([], None)])
    index = ChannelIndex(lister)
    await index.refresh()

    index.apply_event({"type": "channel_archive", "channel": "C1"})

    assert await index.lookup("+14165550123") is None
