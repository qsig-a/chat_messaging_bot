"""Webhook de-duplication and outbound SID mapping."""

import time

import pytest

from sms_bridge.store import Store


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def test_first_sighting_is_not_a_duplicate(store):
    assert store.already_seen("SM1") is False


def test_second_sighting_is_a_duplicate(store):
    store.already_seen("SM1")
    assert store.already_seen("SM1") is True


def test_empty_sid_is_never_a_duplicate(store):
    assert store.already_seen("") is False
    assert store.already_seen("") is False


def test_outbound_round_trip(store):
    store.remember_outbound("SM9", "C123", "1699999999.000100")
    assert store.lookup_outbound("SM9") == ("C123", "1699999999.000100")


def test_outbound_accepts_integer_ids_as_text(store):
    """Discord IDs arrive as ints; Slack ts values are strings. Both store as TEXT."""
    store.remember_outbound("SM8", 4242, 9999)
    assert store.lookup_outbound("SM8") == ("4242", "9999")


def test_unknown_sid_returns_none(store):
    assert store.lookup_outbound("nope") is None


def test_prune_drops_old_rows_only(store):
    store.remember_outbound("recent", "C1", "M1")
    old = int(time.time()) - 40 * 86400
    store._db.execute(
        "INSERT INTO outbound (sid, channel_id, message_id, ts) VALUES (?,?,?,?)",
        ("ancient", "C2", "M2", old),
    )
    store._db.commit()

    store.prune(days=30)

    assert store.lookup_outbound("recent") is not None
    assert store.lookup_outbound("ancient") is None
