"""Slack mrkdwn flattening.

Separate from the adapter so it is testable without slack_sdk installed.

Ordering matters: entity references are decoded last, otherwise a literal
"&lt;@U1&gt;" in a user's message would be decoded into a mention and then
stripped, silently deleting text the user typed.
"""

from __future__ import annotations

import re

_CODEBLOCK = re.compile(r"```(?:[a-zA-Z0-9+-]*\n)?(.*?)```", re.S)
_INLINE = re.compile(r"`([^`]*)`")
_BOLD = re.compile(r"\*(.+?)\*", re.S)
_ITALIC = re.compile(r"(?<![A-Za-z0-9_])_(.+?)_(?![A-Za-z0-9_])", re.S)
_STRIKE = re.compile(r"~(.+?)~", re.S)

# <@U123>, <@U123|name>, <#C123>, <#C123|name>, <!here>, <!subteam^S1|@team>
_MENTION = re.compile(r"<[@#!][^>|]*(?:\|[^>]*)?>")
# <url> and <url|label> - keep the URL, drop the label
_LINK = re.compile(r"<((?:https?|mailto):[^>|]+)(?:\|[^>]*)?>")

_ENTITIES = (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"))


def strip_markup(text: str) -> str:
    text = _CODEBLOCK.sub(lambda m: m.group(1).strip(), text)
    text = _INLINE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _MENTION.sub("", text)
    text = _BOLD.sub(r"\1", text)
    text = _ITALIC.sub(r"\1", text)
    text = _STRIKE.sub(r"\1", text)
    text = re.sub(r"^>\s?", "", text, flags=re.M)
    for entity, char in _ENTITIES:
        text = text.replace(entity, char)
    return re.sub(r"[ \t]{2,}", " ", text).strip()
