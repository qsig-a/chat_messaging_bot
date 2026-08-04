"""Phone-number parsing and the topic-as-routing-table contract.

Channel topics are the only routing state in the system, so number_from_topic
has to keep working across renames and human annotation of topics.
"""

import pytest

from sms_bridge.routing import channel_name_for, normalise_number, number_from_topic, topic_for


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("4165550123", "+14165550123"),        # bare NANP
        ("14165550123", "+14165550123"),       # with country code, no plus
        ("+1 416-555-0123", "+14165550123"),   # formatted
        ("(416) 555-0123", "+14165550123"),    # punctuation
        ("+442071838750", "+442071838750"),    # non-NANP passes through
        ("555", None),                         # too short
        ("", None),                            # empty
        ("not a number", None),
    ],
)
def test_normalise_number(raw, expected):
    assert normalise_number(raw) == expected


@pytest.mark.parametrize(
    "topic,expected",
    [
        ("sms:+14165550123", "+14165550123"),
        ("Jane Doe sms:+14165550123 prefers text", "+14165550123"),  # annotated
        ("sms:12345", None),         # not E.164 - no leading plus
        ("sms:+123", None),          # too short for E.164
        ("smsx:+14165550123", None), # prefix must match exactly
        ("no token here", None),
        (None, None),
        ("", None),
    ],
)
def test_number_from_topic(topic, expected):
    assert number_from_topic(topic) == expected


def test_topic_round_trips_through_number_from_topic():
    number = "+14165550123"
    assert number_from_topic(topic_for(number)) == number


def test_channel_name_is_slug_safe():
    assert channel_name_for("+1 (416) 555-0123") == "sms-14165550123"
