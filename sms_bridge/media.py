"""Signed, expiring URLs for chat attachments travelling out as MMS.

SignalWire fetches MediaUrl during the send call, so the key may be ephemeral:
a restart merely expires in-flight URLs early, which fails closed.

The HMAC and the expiry are the only guard on this route. Never accept an
unsigned token, and never let the endpoint take a URL - it resolves opaque
platform file ids through the adapter's own credential, which is what keeps
this from being an SSRF proxy.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class MediaTokens:
    def __init__(self, key: bytes, ttl_seconds: int = 600) -> None:
        self._key = key
        self._ttl = ttl_seconds

    def mint(self, file_id: str) -> str:
        payload = f"{file_id}:{int(time.time()) + self._ttl}".encode()
        encoded = _b64(payload)
        signature = _b64(hmac.new(self._key, encoded.encode(), hashlib.sha256).digest())
        return f"{encoded}.{signature}"

    def verify(self, token: str) -> str | None:
        encoded, dot, signature = token.partition(".")
        if not dot or not encoded or not signature:
            return None

        expected = _b64(hmac.new(self._key, encoded.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(expected, signature):
            return None

        try:
            file_id, _, expiry = _unb64(encoded).decode().rpartition(":")
            if not file_id or time.time() > int(expiry):
                return None
        except (ValueError, UnicodeDecodeError):
            return None
        return file_id
