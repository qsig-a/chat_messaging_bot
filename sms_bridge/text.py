"""SMS body handling: chunking, GSM-7 segmentation, passcode detection.

segment_count is for logging only - non-GSM-7 characters (emoji, curly quotes)
drop the per-segment limit from 160 to 70, which is worth surfacing when a
message unexpectedly costs four segments instead of one.
"""

from __future__ import annotations

import re

MAX_SMS_CHARS = 1500          # SignalWire hard cap is 1600 per API call

CODE_KEYWORDS = re.compile(
    r"\b(code|otp|passcode|password|verif\w*|2fa|authenticat\w*|token|pin)\b", re.I
)
CODE_DIGITS = re.compile(r"\b\d{4,8}\b")


def looks_like_a_code(body: str) -> bool:
    return bool(CODE_KEYWORDS.search(body) and CODE_DIGITS.search(body))


GSM7 = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM7_EXT = set("^{}\\[~]|€")


def segment_count(body: str) -> int:
    if all(c in GSM7 or c in GSM7_EXT for c in body):
        length = sum(2 if c in GSM7_EXT else 1 for c in body)
        return 1 if length <= 160 else -(-length // 153)
    return 1 if len(body) <= 70 else -(-len(body) // 67)


def chunk(body: str, size: int = MAX_SMS_CHARS) -> list[str]:
    if len(body) <= size:
        return [body]
    out, cur = [], ""
    for word in body.split(" "):
        # A word longer than the whole budget can never fit a chunk; hard-split
        # it rather than emitting an empty piece and then an over-cap one.
        while len(word) > size:
            if cur:
                out.append(cur)
                cur = ""
            out.append(word[:size])
            word = word[size:]
        if not word:
            continue
        if not cur:
            # No separator is needed to start a chunk, so a word of exactly
            # `size` fits here. Reserving one anyway flushed an empty `cur`.
            cur = word
        elif len(cur) + 1 + len(word) > size:
            out.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}"
    if cur:
        out.append(cur)
    return out
