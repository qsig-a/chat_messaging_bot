"""Phone-number normalisation and the topic-as-routing-table helpers.

A chat channel belongs to a number iff its topic contains an `sms:+E164` token.
There is no contact database; topics are the routing table, which is why the
only persistent state in the system is disposable.
"""

from __future__ import annotations

import re

TOPIC_PREFIX = "sms:"
E164 = re.compile(r"^\+[1-9]\d{7,14}$")


def channel_name_for(number: str) -> str:
    return "sms-" + re.sub(r"\D", "", number)


def topic_for(number: str) -> str:
    return f"{TOPIC_PREFIX}{number}"


def number_from_topic(topic: str | None) -> str | None:
    if not topic:
        return None
    for token in topic.split():
        if token.startswith(TOPIC_PREFIX):
            candidate = token[len(TOPIC_PREFIX):]
            if E164.match(candidate):
                return candidate
    return None


def normalise_number(raw: str) -> str | None:
    """Accept 4165550123 / 14165550123 / +1 416-555-0123 -> +14165550123 (NANP)."""
    digits = re.sub(r"\D", "", raw)
    if raw.strip().startswith("+") and E164.match("+" + digits):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if 8 <= len(digits) <= 15:
        return "+" + digits
    return None
