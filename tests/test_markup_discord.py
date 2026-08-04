"""Discord markup is literal text over SMS, so outbound bodies are flattened."""

import pytest

from sms_bridge.chat.discord import strip_markup


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("**bold**", "bold"),
        ("*italic*", "italic"),
        ("_italic_", "italic"),
        ("__underline__", "underline"),
        ("***bold italic***", "bold italic"),
        ("~~strike~~", "strike"),
        ("||spoiler||", "spoiler"),
        ("`code`", "code"),
        ("```py\nx = 1\n```", "x = 1"),
        ("```\nplain\n```", "plain"),
        ("> quoted", "quoted"),
        ("<@123456> hi", "hi"),
        ("<@!123456> hi", "hi"),
        ("<#123456> hi", "hi"),
        ("<@&123456> hi", "hi"),
        ("<:blob:123456>", ":blob:"),
        ("<a:blobdance:123456>", ":blobdance:"),
        ("plain text", "plain text"),
        ("", ""),
    ],
)
def test_strip_discord_markup(raw, expected):
    assert strip_markup(raw) == expected


def test_nested_emphasis():
    assert strip_markup("**bold _and italic_**") == "bold and italic"


def test_mixed_message():
    raw = "<@1> check **this** out: `run --now` ||secret||"
    assert strip_markup(raw) == "check this out: run --now secret"


def test_url_underscores_survive():
    """The emphasis rule needs word boundaries, so URLs pass through intact."""
    assert strip_markup("https://example.com/a_b_c") == "https://example.com/a_b_c"
    assert (
        strip_markup("https://en.wikipedia.org/wiki/Foo_bar_baz")
        == "https://en.wikipedia.org/wiki/Foo_bar_baz"
    )


def test_snake_case_identifiers_survive():
    """Env var names are the common case: people paste them to troubleshoot."""
    assert strip_markup("SIGNALWIRE_API_TOKEN") == "SIGNALWIRE_API_TOKEN"
    assert strip_markup("PUBLIC_BASE_URL") == "PUBLIC_BASE_URL"
    assert strip_markup("foo_bar") == "foo_bar"


def test_paired_asterisks_eat_multiplication_known_bug():
    """An even number of asterisks anywhere flattens the span between them."""
    assert strip_markup("5*x + 3*y") == "5x + 3y"


def test_a_single_underscore_or_asterisk_survives():
    assert strip_markup("foo_bar") == "foo_bar"
    assert strip_markup("3 * 4 = 12") == "3 * 4 = 12"
