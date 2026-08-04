"""Signed, expiring tokens for the outbound media endpoint.

The HMAC and the expiry are the only guard on a public route, so tampering and
expiry get explicit coverage.
"""

import base64
import time

import pytest

from sms_bridge.media import MediaTokens

KEY = b"0123456789abcdef0123456789abcdef"


def test_round_trip():
    tokens = MediaTokens(KEY)
    assert tokens.verify(tokens.mint("F123")) == "F123"


def test_token_is_url_safe():
    token = MediaTokens(KEY).mint("F123")
    assert "/" not in token and "+" not in token and "=" not in token


def test_wrong_key_is_rejected():
    token = MediaTokens(KEY).mint("F123")
    assert MediaTokens(b"a different key entirely!!!!!!!!").verify(token) is None


def test_tampered_payload_is_rejected():
    tokens = MediaTokens(KEY)
    token = tokens.mint("F123")
    payload, _, signature = token.partition(".")
    forged = base64.urlsafe_b64encode(b"F999:9999999999").decode().rstrip("=")
    assert tokens.verify(f"{forged}.{signature}") is None


def test_tampered_signature_is_rejected():
    tokens = MediaTokens(KEY)
    payload, _, signature = tokens.mint("F123").partition(".")
    assert tokens.verify(f"{payload}.{signature[:-1]}x") is None


def test_expired_token_is_rejected():
    tokens = MediaTokens(KEY, ttl_seconds=-1)
    assert tokens.verify(tokens.mint("F123")) is None


def test_token_valid_within_ttl():
    tokens = MediaTokens(KEY, ttl_seconds=600)
    token = tokens.mint("F123")
    assert tokens.verify(token) == "F123"
    assert int(base64.urlsafe_b64decode(
        token.partition(".")[0] + "==").decode().split(":")[1]) > time.time()


@pytest.mark.parametrize("garbage", ["", ".", "no-dot", "!!!.???", "a.b.c"])
def test_malformed_tokens_are_rejected(garbage):
    assert MediaTokens(KEY).verify(garbage) is None


def test_file_ids_containing_colons_survive():
    """Slack ids are plain, but nothing should depend on that."""
    tokens = MediaTokens(KEY)
    assert tokens.verify(tokens.mint("a:b:c")) == "a:b:c"
