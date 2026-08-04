"""Chunking, GSM-7 segmentation, and passcode detection."""

import pytest

from sms_bridge.text import chunk, looks_like_a_code, segment_count

MAX = 1500


def test_short_body_is_one_chunk():
    assert chunk("hello", size=MAX) == ["hello"]


def test_splits_on_word_boundaries():
    assert chunk("one two three four", size=10) == ["one two", "three four"]


def test_oversized_single_word_is_split_not_emptied():
    """A word longer than the limit must be hard-split.

    The pre-fix behaviour returned ['', 'aaaa...'] - an empty first SMS that
    SignalWire rejects, followed by one over the 1600-character hard cap.
    """
    pieces = chunk("a" * 25, size=10)
    assert "".join(pieces) == "a" * 25
    assert all(pieces), "no chunk may be empty"
    assert all(len(p) <= 10 for p in pieces)


def test_oversized_word_among_normal_words():
    pieces = chunk("hi " + "b" * 25 + " bye", size=10)
    assert all(pieces)
    assert all(len(p) <= 10 for p in pieces)
    assert "".join(pieces).replace(" ", "") == ("hi" + "b" * 25 + "bye")


def test_no_chunk_exceeds_size_for_long_prose():
    body = " ".join(["word"] * 2000)
    assert all(len(p) <= MAX for p in chunk(body, size=MAX))


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
def test_segment_count(body, expected):
    assert segment_count(body) == expected


def test_ucs2_drops_the_per_segment_limit():
    """A single emoji forces UCS-2, cutting the limit from 160 to 70."""
    gsm_only = "a" * 100
    with_emoji = "a" * 99 + "\U0001F600"
    assert segment_count(gsm_only) == 1
    assert segment_count(with_emoji) == 2


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
def test_looks_like_a_code(body, expected):
    assert looks_like_a_code(body) is expected


def test_word_of_exactly_size_does_not_emit_an_empty_chunk():
    """A word exactly `size` long needs no separator to start a chunk."""
    pieces = chunk("b" * 10 + " c", size=10)
    assert pieces == ["bbbbbbbbbb", "c"]


def test_oversized_word_that_is_an_exact_multiple_of_size():
    """The hard-split leaves a remainder of exactly `size`, then exactly 0."""
    pieces = chunk("a" * 20, size=10)
    assert pieces == ["aaaaaaaaaa", "aaaaaaaaaa"]
    assert all(pieces)


def test_no_empty_piece_at_the_real_sms_limit():
    """MAX_SMS_CHARS is 1500, so a 1500-character token is the production case."""
    pieces = chunk("a" * 1500 + " bye", size=1500)
    assert all(pieces)
    assert all(len(p) <= 1500 for p in pieces)
    assert "".join(pieces).replace(" ", "") == "a" * 1500 + "bye"


def test_consecutive_oversized_words():
    pieces = chunk("a" * 20 + " " + "c" * 20, size=10)
    assert all(pieces)
    assert all(len(p) <= 10 for p in pieces)
    assert "".join(pieces).replace(" ", "") == "a" * 20 + "c" * 20
