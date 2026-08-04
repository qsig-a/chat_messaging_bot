"""Slack mrkdwn is literal text over SMS. Flatten it."""

import pytest

from sms_bridge.chat.slack_markup import strip_markup


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("*bold*", "bold"),                       # single asterisk is bold in Slack
        ("_italic_", "italic"),
        ("~strike~", "strike"),
        ("`code`", "code"),
        ("```\nx = 1\n```", "x = 1"),
        ("> quoted", "quoted"),
        ("plain text", "plain text"),
        ("", ""),
    ],
)
def test_basic_formatting(raw, expected):
    assert strip_markup(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("<@U12345>", ""),                        # user mention
        ("<@U12345|jane>", ""),
        ("<#C12345|general> hi", "hi"),           # channel reference
        ("<#C12345> hi", "hi"),
        ("<!here> hi", "hi"),                     # special mention
        ("<!channel> hi", "hi"),
        ("<!subteam^S123|@team> hi", "hi"),
    ],
)
def test_mentions_are_removed(raw, expected):
    assert strip_markup(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("<https://example.com>", "https://example.com"),
        ("<https://example.com|click here>", "https://example.com"),
        ("see <https://example.com|the docs>", "see https://example.com"),
        ("<mailto:a@b.com|a@b.com>", "mailto:a@b.com"),
    ],
)
def test_links_keep_the_url_not_the_label(raw, expected):
    """The recipient gets an SMS - a label with no URL is useless."""
    assert strip_markup(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("a &amp; b", "a & b"),
        ("a &lt; b", "a < b"),
        ("a &gt; b", "a > b"),
    ],
)
def test_html_entities_are_decoded(raw, expected):
    assert strip_markup(raw) == expected


def test_mixed_message():
    raw = "<@U1> check *this*: <https://example.com|docs> &amp; `run --now`"
    assert strip_markup(raw) == "check this: https://example.com & run --now"


def test_url_with_underscores_survives():
    assert strip_markup("https://example.com/a_b_c") == "https://example.com/a_b_c"
