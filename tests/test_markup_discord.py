"""Discord markup is literal text over SMS, so outbound bodies are flattened."""

import pytest


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
def test_strip_discord_markup(bridge, raw, expected):
    assert bridge.strip_discord_markup(raw) == expected


def test_nested_emphasis(bridge):
    assert bridge.strip_discord_markup("**bold _and italic_**") == "bold and italic"


def test_mixed_message(bridge):
    raw = "<@1> check **this** out: `run --now` ||secret||"
    assert bridge.strip_discord_markup(raw) == "check this out: run --now secret"


def test_url_underscores_are_stripped_known_bug(bridge):
    """Underscores inside URLs do NOT survive. This pins current behaviour.

    The emphasis regex pairs delimiters across the whole message with no
    concept of a URL or an identifier, so any even number of underscores
    flattens the span between them. Pinned rather than fixed: changing it
    is a behaviour change to a running deployment and is out of scope for
    this task. If a later change fixes the regex, this test SHOULD fail --
    update it then.
    """
    assert bridge.strip_discord_markup("https://example.com/a_b_c") == (
        "https://example.com/abc"
    )


def test_snake_case_identifiers_are_mangled_known_bug(bridge):
    """Env var names are the common case: users paste them to troubleshoot."""
    assert bridge.strip_discord_markup("SIGNALWIRE_API_TOKEN") == "SIGNALWIREAPITOKEN"
    assert bridge.strip_discord_markup("PUBLIC_BASE_URL") == "PUBLICBASEURL"


def test_paired_asterisks_eat_multiplication_known_bug(bridge):
    """An even number of asterisks anywhere flattens the span between them."""
    assert bridge.strip_discord_markup("5*x + 3*y") == "5x + 3y"


def test_a_single_underscore_or_asterisk_survives(bridge):
    assert bridge.strip_discord_markup("foo_bar") == "foo_bar"
    assert bridge.strip_discord_markup("3 * 4 = 12") == "3 * 4 = 12"
