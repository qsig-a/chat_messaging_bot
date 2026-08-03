"""Chunking, GSM-7 segmentation, and passcode detection."""

import pytest

MAX = 1500


def test_short_body_is_one_chunk(bridge):
    assert bridge.chunk("hello", size=MAX) == ["hello"]


def test_splits_on_word_boundaries(bridge):
    assert bridge.chunk("one two three four", size=10) == ["one two", "three four"]


def test_oversized_single_word_is_split_not_emptied(bridge):
    """A word longer than the limit must be hard-split.

    The pre-fix behaviour returned ['', 'aaaa...'] - an empty first SMS that
    SignalWire rejects, followed by one over the 1600-character hard cap.
    """
    pieces = bridge.chunk("a" * 25, size=10)
    assert "".join(pieces) == "a" * 25
    assert all(pieces), "no chunk may be empty"
    assert all(len(p) <= 10 for p in pieces)


def test_oversized_word_among_normal_words(bridge):
    pieces = bridge.chunk("hi " + "b" * 25 + " bye", size=10)
    assert all(pieces)
    assert all(len(p) <= 10 for p in pieces)
    assert "".join(pieces).replace(" ", "") == ("hi" + "b" * 25 + "bye")


def test_no_chunk_exceeds_size_for_long_prose(bridge):
    body = " ".join(["word"] * 2000)
    assert all(len(p) <= MAX for p in bridge.chunk(body, size=MAX))


@pytest.mark.parametrize(
    "body,expected",
    [
        ("hi", 1),
        ("a" * 160, 1),      # GSM-7 single segment boundary
        ("a" * 161, 2),      # concatenated GSM-7 drops to 153/segment
        ("\U0001F600", 1),   # emoji forces UCS-2
        ("a" * 71, 1),       # still GSM-7, well under 160
    ],
)
def test_segment_count(bridge, body, expected):
    assert bridge.segment_count(body) == expected


def test_ucs2_drops_the_per_segment_limit(bridge):
    """A single emoji forces UCS-2, cutting the limit from 160 to 70."""
    gsm_only = "a" * 100
    with_emoji = "a" * 99 + "\U0001F600"
    assert bridge.segment_count(gsm_only) == 1
    assert bridge.segment_count(with_emoji) == 2


@pytest.mark.parametrize(
    "body,expected",
    [
        ("Your code is 123456", True),
        ("verification 4821 now", True),
        ("Your OTP: 9999", True),
        ("PIN 1234 for entry", True),
        ("code", False),            # keyword but no digits
        ("123456", False),          # digits but no keyword
        ("Your OTP: 12", False),    # too few digits
        ("call me at 4165550123", False),
        ("", False),
    ],
)
def test_looks_like_a_code(bridge, body, expected):
    assert bridge.looks_like_a_code(body) is expected
