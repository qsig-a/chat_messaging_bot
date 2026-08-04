"""SignalWire LaML client and webhook signature validation.

The signature scheme is Twilio's: HMAC-SHA1 over PUBLIC_BASE_URL + path plus
sorted form parameters, keyed by the project's signing key. It is the only
authentication on the webhook endpoints.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from typing import Sequence

import httpx

from .config import Config

log = logging.getLogger("bridge.signalwire")

MAX_UPLOAD_BYTES = 8 * 1024 * 1024

_EXTENSIONS = {
    "image/jpeg": "jpg", "image/png": "png", "image/gif": "gif",
    "image/webp": "webp", "video/mp4": "mp4", "audio/mpeg": "mp3",
    "audio/amr": "amr", "application/pdf": "pdf", "text/vcard": "vcf",
    "text/plain": "txt",
}


def valid_signature(
    url: str, params: dict[str, str], signature: str, key: str
) -> bool:
    """Twilio-compatible request signature, as used by SignalWire's LaML API."""
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(key.encode(), payload.encode("utf-8"), hashlib.sha1).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature or "")


class SignalWire:
    def __init__(self, config: Config, http: httpx.AsyncClient) -> None:
        self._c = config
        self._http = http

    async def send_sms(
        self, to: str, body: str, media_urls: Sequence[str] = ()
    ) -> str:
        # httpx's data= only builds an async-compatible request body for a Mapping;
        # a list of (key, value) pairs is instead treated as raw `content=` and
        # produces a sync-only iterator stream that an AsyncClient can't send. A
        # dict with a list value for the repeated key is the supported way to get
        # multiple MediaUrl fields in one urlencoded body.
        data: dict[str, str | list[str]] = {
            "From": self._c.sw_number,
            "To": to,
            "Body": body,
            "StatusCallback": f"{self._c.public_base_url}/sms/status",
        }
        if media_urls:
            data["MediaUrl"] = list(media_urls)
        r = await self._http.post(
            f"{self._c.sw_api_base}/Messages.json",
            data=data,
            auth=(self._c.sw_project, self._c.sw_token),
            timeout=20,
        )
        r.raise_for_status()
        return r.json()["sid"]

    async def fetch_media(self, url: str) -> tuple[bytes, str, str] | None:
        try:
            r = await self._http.get(
                url, auth=(self._c.sw_project, self._c.sw_token), follow_redirects=True
            )
            r.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            log.warning("media fetch failed %s: %s", url, exc)
            return None
        if len(r.content) > MAX_UPLOAD_BYTES:
            return None
        ctype = (
            r.headers.get("content-type", "application/octet-stream")
            .split(";")[0]
            .strip()
            .lower()
        )
        return r.content, f"mms.{_EXTENSIONS.get(ctype, 'bin')}", ctype

    def check(self, request, path: str, params: dict[str, str]) -> bool:
        signature = request.headers.get("X-Twilio-Signature", "")
        return valid_signature(
            f"{self._c.public_base_url}{path}", params, signature, self._c.sw_signing_key
        )

    def explain_bad_signature(self, request, path, params, signature) -> str:
        """Say *why* a signature failed, so a rejection points at the misconfiguration.

        SignalWire signs the webhook URL exactly as configured in its dashboard, so a
        mismatch is nearly always config drift rather than a forged request. Retrying
        the HMAC against each plausible variant identifies which knob is wrong: if a
        variant matches, the key is fine and the URL is wrong; if none matches, the URL
        is a dead end and the key is the suspect.

        Only parameter *names* are ever logged - values carry message bodies and
        passcodes.
        """
        c = self._c
        if not signature:
            ctype = request.headers.get("content-type", "none")
            seen = sorted(h for h in request.headers if "signature" in h.lower())
            return (
                f"no X-Twilio-Signature header (content-type={ctype!r}, "
                f"signature-ish headers present: {seen or 'none'}); the number is probably "
                "routed to a non-LaML handler, which signs differently"
            )

        base = f"{c.public_base_url}{path}"
        host = request.headers.get("host", "")
        observed = f"https://{host}{request.url.path}"
        if request.url.query:
            observed += f"?{request.url.query}"

        candidates = [
            (f"{base}/", "the configured webhook URL has a trailing slash"),
            (observed, "the webhook URL is the host/query the request arrived with"),
            (observed.replace("https://", "http://", 1), "the webhook is configured as http://"),
            (base.replace("https://", "http://", 1), "the webhook is configured as http://"),
        ]
        for url, hint in candidates:
            if url != base and valid_signature(url, params, signature, c.sw_signing_key):
                return f"signature matches {url!r} instead - {hint}"

        if c.sw_token != c.sw_signing_key and valid_signature(
            base, params, signature, c.sw_token
        ):
            return (
                "signature matches SIGNALWIRE_API_TOKEN, not SIGNALWIRE_SIGNING_KEY - "
                "this project signs with the API token, so set SIGNALWIRE_SIGNING_KEY to it"
            )

        return (
            f"no URL variant or known credential matched (signed url={base!r}, host={host!r}, "
            f"query={request.url.query!r}, params={sorted(params)}); the signing key is wrong "
            "- copy the signing key from the SignalWire dashboard for this project into "
            "SIGNALWIRE_SIGNING_KEY (a project can hold several tokens, and only one signs)"
        )
