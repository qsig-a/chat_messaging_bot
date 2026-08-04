# Unified Chat Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the single-file SignalWire ↔ Discord bridge into one application that targets either Discord or Slack, selected by `CHAT_PLATFORM`, with real MMS support and a test suite.

**Architecture:** A platform-agnostic core (`sms_bridge/`) owns all SignalWire logic and all policy decisions — especially passcode suppression. Chat platforms contribute adapters implementing a ~14-method `ChatAdapter` protocol; adapters report capability and execute instructions but never decide policy. One process serves one platform.

**Tech Stack:** Python 3.12, `discord.py`, `slack-sdk` (Socket Mode, no Bolt), `fastapi`, `uvicorn`, `httpx`, `pytest` + `pytest-asyncio`.

**Spec:** `docs/superpowers/specs/2026-08-03-unified-chat-bridge-design.md`

## Global Constraints

- **Never let a passcode reach a contact channel.** If the secure channel is unconfigured, invisible, or rejects the send, suppress the body entirely and post a placeholder. Missing access must never downgrade to posting somewhere less private.
- **Fetch and fold MMS media before the passcode check.** Carriers deliver captions as a separate `text/plain` part; a code sent as a caption must not skip redaction.
- **Do not publish the container's port.** `BIND_HOST=0.0.0.0` is only safe because nothing maps a port and `cloudflared` reaches `http://bridge:8080` privately.
- **`python-multipart >= 0.0.30`.** It parses bodies from unauthenticated callers before the signature check.
- **Third-party GitHub Actions are pinned to full commit SHAs** with the version in a trailing comment. Never replace with a mutable tag.
- **`requirements.txt` is a pip-compile lock with hashes.** Never hand-edit; regenerate via the documented Docker command.
- **`.env.example` is consumed by both Compose and systemd.** Plain `KEY=VALUE`, no quotes, no `export`, no trailing comments.
- **Conventional commit prefixes** (`feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`) — release notes are generated from them.
- **`MAX_SMS_CHARS = 1500`** (SignalWire hard cap 1600). Inbound chat posts chunk at the adapter's `max_post_chars`.
- **`SIGNALWIRE_SIGNING_KEY` never falls back to `SIGNALWIRE_API_TOKEN`.**
- All IDs crossing the adapter boundary are `str`. Discord ints are stringified; Slack `ts` values are already strings.

---

# Phase 1 — Baseline tests against the current single file

No behaviour changes except Task 2, which is an isolated bug fix. These tests must stay green through the entire refactor.

---

### Task 1: Test scaffolding and routing tests

**Files:**
- Create: `requirements-dev.in`, `requirements-dev.txt`, `pytest.ini`, `tests/conftest.py`, `tests/test_routing.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: nothing
- Produces: a working `pytest` run; `tests/conftest.py` sets the env vars needed to import `sms_discord_bridge` and exposes it as the `bridge` fixture.

**Why conftest matters here:** `sms_discord_bridge.py` reads config at *import* time and calls `sys.exit` on any missing required variable, and it opens the SQLite file at import time too. Tests must set environment variables before the import happens. `conftest.py` is imported by pytest before any test module, which makes it the correct place.

- [ ] **Step 1: Create the dev dependency input**

`requirements-dev.in`:
```
# Development-only dependencies. Not installed in the runtime image.
# Regenerate the lock after editing:
#
#   docker run --rm -v "$PWD":/w -w /w python:3.12-slim sh -c \
#     "pip install -q pip-tools && pip-compile --generate-hashes --strip-extras \
#      -o requirements-dev.txt requirements-dev.in"

pytest==8.4.2
pytest-asyncio==1.2.0
```

Resolve the exact current versions before pinning:
```bash
pip index versions pytest
pip index versions pytest-asyncio
```
Use the resolved versions rather than the ones above if they differ.

- [ ] **Step 2: Compile the dev lock**

```bash
docker run --rm -v "$PWD":/w -w /w python:3.12-slim sh -c \
  "pip install -q pip-tools && pip-compile --generate-hashes --strip-extras \
   -o requirements-dev.txt requirements-dev.in"
```

- [ ] **Step 3: Add pytest configuration**

`pytest.ini`:
```ini
[pytest]
testpaths = tests
asyncio_mode = auto
```

`asyncio_mode = auto` means `async def` tests run without needing an `@pytest.mark.asyncio` decorator on each one.

- [ ] **Step 4: Write conftest.py**

`tests/conftest.py`:
```python
"""Test environment bootstrap.

sms_discord_bridge.py resolves its configuration and opens SQLite at import
time, exiting on any missing required variable. These values must therefore be
in os.environ before the module is imported anywhere in the test run. pytest
imports conftest.py before collecting test modules, so this is the right place.
"""

import os

_TEST_ENV = {
    "DISCORD_TOKEN": "test-discord-token",
    "DISCORD_GUILD_ID": "1",
    "DISCORD_INBOX_CHANNEL_ID": "2",
    "SIGNALWIRE_SPACE_URL": "https://example.signalwire.com",
    "SIGNALWIRE_PROJECT_ID": "test-project",
    "SIGNALWIRE_API_TOKEN": "test-api-token",
    "SIGNALWIRE_SIGNING_KEY": "test-signing-key",
    "SIGNALWIRE_NUMBER": "+14165550100",
    "PUBLIC_BASE_URL": "https://sms.example.com",
    "DB_PATH": ":memory:",
}
for _key, _value in _TEST_ENV.items():
    os.environ.setdefault(_key, _value)

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def bridge():
    """The module under test. Imported lazily so the env above is applied first."""
    import sms_discord_bridge

    return sms_discord_bridge
```

- [ ] **Step 5: Write the failing routing tests**

`tests/test_routing.py`:
```python
"""Phone-number parsing and the topic-as-routing-table contract.

Channel topics are the only routing state in the system, so number_from_topic
has to keep working across renames and human annotation of topics.
"""

import pytest


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
def test_normalise_number(bridge, raw, expected):
    assert bridge.normalise_number(raw) == expected


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
def test_number_from_topic(bridge, topic, expected):
    assert bridge.number_from_topic(topic) == expected


def test_topic_round_trips_through_number_from_topic(bridge):
    number = "+14165550123"
    assert bridge.number_from_topic(bridge.topic_for(number)) == number


def test_channel_name_is_slug_safe(bridge):
    assert bridge.channel_name_for("+1 (416) 555-0123") == "sms-14165550123"
```

- [ ] **Step 6: Run the tests and watch them pass**

```bash
pip install --require-hashes -r requirements.txt -r requirements-dev.txt
pytest tests/test_routing.py -v
```
Expected: all PASS. These characterise existing correct behaviour rather than driving new code, so passing immediately is the correct outcome — the value is the regression guard through the refactor.

- [ ] **Step 7: Ignore pytest artifacts**

Append to `.gitignore`:
```
.pytest_cache/
```

- [ ] **Step 8: Commit**

```bash
git add requirements-dev.in requirements-dev.txt pytest.ini tests/ .gitignore
git commit -m "test: add pytest scaffolding and routing baseline tests"
```

---

### Task 2: Fix `chunk()` on oversized words

**Files:**
- Modify: `sms_discord_bridge.py:216-228`
- Create: `tests/test_text.py`

**Interfaces:**
- Consumes: the `bridge` fixture from Task 1
- Produces: `chunk(body, size)` never returns an empty string and never returns a piece longer than `size`.

**The bug:** `chunk("a" * 20, size=10)` currently returns `['', 'aaaaaaaaaaaaaaaaaaaa']`. When the first word already exceeds `size`, the loop appends the empty accumulator and then never splits the oversized word. In production a 1500+ character unbroken string — a long URL, a pasted token, a base64 blob — produces an empty SMS (rejected by SignalWire) followed by one over the 1600 hard cap (also rejected).

- [ ] **Step 1: Write the failing tests**

`tests/test_text.py`:
```python
"""Chunking, GSM-7 segmentation, and passcode detection."""

import pytest

MAX = 1500


def test_short_body_is_one_chunk(bridge):
    assert bridge.chunk("hello", size=MAX) == ["hello"]


def test_splits_on_word_boundaries(bridge):
    assert bridge.chunk("one two three four", size=10) == ["one two", "three four"]


def test_oversized_single_word_is_split_not_emptied(bridge):
    """A word longer than the limit must be hard-split.

    The pre-fix behaviour returned ['', 'aaaa...'] - an empty first SMS that
    SignalWire rejects, followed by one over the 1600-character hard cap.
    """
    pieces = bridge.chunk("a" * 25, size=10)
    assert "".join(pieces) == "a" * 25
    assert all(pieces), "no chunk may be empty"
    assert all(len(p) <= 10 for p in pieces)


def test_oversized_word_among_normal_words(bridge):
    pieces = bridge.chunk("hi " + "b" * 25 + " bye", size=10)
    assert all(pieces)
    assert all(len(p) <= 10 for p in pieces)
    assert "".join(pieces).replace(" ", "") == ("hi" + "b" * 25 + "bye")


def test_no_chunk_exceeds_size_for_long_prose(bridge):
    body = " ".join(["word"] * 2000)
    assert all(len(p) <= MAX for p in bridge.chunk(body, size=MAX))


def test_word_of_exactly_size_does_not_emit_an_empty_chunk(bridge):
    """A word exactly `size` long needs no separator to start a chunk."""
    assert bridge.chunk("b" * 10 + " c", size=10) == ["bbbbbbbbbb", "c"]


def test_oversized_word_that_is_an_exact_multiple_of_size(bridge):
    """The hard-split leaves a remainder of exactly `size`, then exactly 0."""
    pieces = bridge.chunk("a" * 20, size=10)
    assert pieces == ["aaaaaaaaaa", "aaaaaaaaaa"]
    assert all(pieces)


def test_no_empty_piece_at_the_real_sms_limit(bridge):
    """MAX_SMS_CHARS is 1500, so a 1500-character token is the production case."""
    pieces = bridge.chunk("a" * 1500 + " bye", size=1500)
    assert all(pieces)
    assert all(len(p) <= 1500 for p in pieces)
    assert "".join(pieces).replace(" ", "") == "a" * 1500 + "bye"


def test_consecutive_oversized_words(bridge):
    pieces = bridge.chunk("a" * 20 + " " + "c" * 20, size=10)
    assert all(pieces)
    assert all(len(p) <= 10 for p in pieces)
    assert "".join(pieces).replace(" ", "") == "a" * 20 + "c" * 20


@pytest.mark.parametrize(
    "body,expected",
    [
        ("hi", 1),
        ("a" * 160, 1),      # GSM-7 single segment boundary
        ("a" * 161, 2),      # concatenated GSM-7 drops to 153/segment
        ("\U0001F600", 1),   # emoji forces UCS-2
        ("a" * 71, 1),       # still GSM-7, well under 160
    ],
)
def test_segment_count(bridge, body, expected):
    assert bridge.segment_count(body) == expected


def test_ucs2_drops_the_per_segment_limit(bridge):
    """A single emoji forces UCS-2, cutting the limit from 160 to 70."""
    gsm_only = "a" * 100
    with_emoji = "a" * 99 + "\U0001F600"
    assert bridge.segment_count(gsm_only) == 1
    assert bridge.segment_count(with_emoji) == 2


@pytest.mark.parametrize(
    "body,expected",
    [
        ("Your code is 123456", True),
        ("verification 4821 now", True),
        ("Your OTP: 9999", True),
        ("PIN 1234 for entry", True),
        ("code", False),            # keyword but no digits
        ("123456", False),          # digits but no keyword
        ("Your OTP: 12", False),    # too few digits
        ("call me at 4165550123", False),
        ("", False),
    ],
)
def test_looks_like_a_code(bridge, body, expected):
    assert bridge.looks_like_a_code(body) is expected
```

- [ ] **Step 2: Run the tests to verify the two chunk tests fail**

```bash
pytest tests/test_text.py -v
```
Expected: `test_oversized_single_word_is_split_not_emptied` and `test_oversized_word_among_normal_words` FAIL. Everything else PASSes.

- [ ] **Step 3: Fix `chunk`**

Replace `sms_discord_bridge.py:216-228` with:
```python
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
            # `size` fits here. Reserving one anyway flushes an empty `cur`.
            cur = word
        elif len(cur) + 1 + len(word) > size:
            out.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}"
    if cur:
        out.append(cur)
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
pytest tests/test_text.py -v
```
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add sms_discord_bridge.py tests/test_text.py
git commit -m "fix: split words longer than the SMS chunk size

chunk() emitted an empty first piece and left the oversized word intact when
a single word exceeded the limit, producing one SMS SignalWire rejects for an
empty body and another over the 1600-character cap."
```

---

### Task 3: Signature validation tests

**Files:**
- Create: `tests/test_signature.py`

**Interfaces:**
- Consumes: `bridge.valid_signature(url, params, signature, token="")`
- Produces: regression coverage for the only authentication on the webhook endpoints.

**Verified test vector:** the algorithm matches Twilio's documented worked example exactly — URL `https://mycompany.com/myapp.php?foo=1&bar=2` with those five parameters and auth token `12345` produces `RSOYDt4T1cUTdK1PDd93/VVr8B8=`. This has been confirmed against the current implementation.

- [ ] **Step 1: Write the tests**

`tests/test_signature.py`:
```python
"""Twilio-scheme request signing, as used by SignalWire's LaML API.

This is the only authentication on the webhook endpoints, so it gets a known
external vector rather than a self-computed one.
"""

TWILIO_URL = "https://mycompany.com/myapp.php?foo=1&bar=2"
TWILIO_PARAMS = {
    "CallSid": "CA1234567890ABCDE",
    "Caller": "+14158675309",
    "Digits": "1234",
    "From": "+14158675309",
    "To": "+18005551212",
}
TWILIO_TOKEN = "12345"
TWILIO_SIGNATURE = "RSOYDt4T1cUTdK1PDd93/VVr8B8="


def test_accepts_documented_twilio_vector(bridge):
    assert bridge.valid_signature(
        TWILIO_URL, TWILIO_PARAMS, TWILIO_SIGNATURE, TWILIO_TOKEN
    )


def test_rejects_wrong_token(bridge):
    assert not bridge.valid_signature(
        TWILIO_URL, TWILIO_PARAMS, TWILIO_SIGNATURE, "not-the-token"
    )


def test_rejects_tampered_url(bridge):
    assert not bridge.valid_signature(
        TWILIO_URL + "&extra=1", TWILIO_PARAMS, TWILIO_SIGNATURE, TWILIO_TOKEN
    )


def test_rejects_tampered_param(bridge):
    tampered = dict(TWILIO_PARAMS, Digits="9999")
    assert not bridge.valid_signature(
        TWILIO_URL, tampered, TWILIO_SIGNATURE, TWILIO_TOKEN
    )


def test_rejects_added_param(bridge):
    extra = dict(TWILIO_PARAMS, Body="surprise")
    assert not bridge.valid_signature(
        TWILIO_URL, extra, TWILIO_SIGNATURE, TWILIO_TOKEN
    )


def test_rejects_empty_signature(bridge):
    assert not bridge.valid_signature(TWILIO_URL, TWILIO_PARAMS, "", TWILIO_TOKEN)


def test_param_order_does_not_matter(bridge):
    """Params are sorted before hashing, so dict order must be irrelevant."""
    reordered = dict(reversed(list(TWILIO_PARAMS.items())))
    assert bridge.valid_signature(
        TWILIO_URL, reordered, TWILIO_SIGNATURE, TWILIO_TOKEN
    )


def test_empty_params_signs_url_alone(bridge):
    """A status callback with no form body still has to validate."""
    import base64
    import hashlib
    import hmac

    url = "https://sms.example.com/sms/status"
    expected = base64.b64encode(
        hmac.new(b"key", url.encode(), hashlib.sha1).digest()
    ).decode()
    assert bridge.valid_signature(url, {}, expected, "key")
```

- [ ] **Step 2: Run the tests**

```bash
pytest tests/test_signature.py -v
```
Expected: all PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_signature.py
git commit -m "test: cover Twilio-scheme webhook signature validation"
```

---

### Task 4: Discord markup stripping tests

**Files:**
- Create: `tests/test_markup_discord.py`

**Interfaces:**
- Consumes: `bridge.strip_discord_markup(text)`
- Produces: the contract the Discord adapter's `strip_markup` must preserve in Task 10.

Note: `test_url_is_left_alone` as written below **fails** — the emphasis regex has no
word-boundary rule, so any even number of underscores flattens the span between them
(`SIGNALWIRE_API_TOKEN` → `SIGNALWIREAPITOKEN`). Pin the actual behaviour here and leave
the source alone; Task 10 fixes it when the function moves into the adapter.

- [ ] **Step 1: Write the tests**

`tests/test_markup_discord.py`:
````python
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


def test_url_is_left_alone(bridge):
    """Underscores and asterisks inside URLs must survive."""
    url = "https://example.com/a_b_c"
    assert bridge.strip_discord_markup(url) == url
````

- [ ] **Step 2: Run the tests**

```bash
pytest tests/test_markup_discord.py -v
```
Expected: all PASS. If `test_url_is_left_alone` fails, the emphasis regex is eating URL underscores — record the actual behaviour in the test and open a follow-up rather than changing behaviour in this task.

- [ ] **Step 3: Commit**

```bash
git add tests/test_markup_discord.py
git commit -m "test: cover Discord markup stripping"
```

---

# Phase 2 — Extract core and the Discord adapter

Behaviour must stay identical. Tests from Phase 1 are re-pointed at the new modules and must remain green.

---

### Task 5: Package skeleton and config

**Files:**
- Create: `sms_bridge/__init__.py`, `sms_bridge/config.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `sms_bridge.config.Config` — frozen dataclass with fields: `platform: str`, `discord_token: str`, `discord_guild_id: int`, `discord_category_id: int`, `discord_inbox_channel_id: int`, `discord_secure_channel_id: int`, `slack_bot_token: str`, `slack_app_token: str`, `slack_inbox_channel_id: str`, `slack_secure_channel_id: str`, `slack_invite_users: tuple[str, ...]`, `sw_space: str`, `sw_project: str`, `sw_token: str`, `sw_signing_key: str`, `sw_number: str`, `public_base_url: str`, `bind_host: str`, `bind_port: int`, `verify_signature: bool`, `redact_codes: bool`, `db_path: str`, `heartbeat_url: str`, `command_prefix: str`, `note_prefix: str`, `media_signing_key: bytes`, `max_mms_bytes: int`
  - `sms_bridge.config.load(env: Mapping[str, str]) -> Config`
  - `sms_bridge.config.ConfigError(Exception)` with a `.message` attribute

`load` raises `ConfigError` rather than calling `sys.exit`, so it is testable; `__main__` catches it and exits in Task 11.

- [ ] **Step 1: Write the failing tests**

`tests/test_config.py`:
```python
"""Environment parsing and per-platform requirements."""

import pytest

from sms_bridge.config import ConfigError, load

COMMON = {
    "SIGNALWIRE_SPACE_URL": "https://example.signalwire.com",
    "SIGNALWIRE_PROJECT_ID": "proj",
    "SIGNALWIRE_API_TOKEN": "tok",
    "SIGNALWIRE_SIGNING_KEY": "sign",
    "SIGNALWIRE_NUMBER": "+14165550100",
    "PUBLIC_BASE_URL": "https://sms.example.com",
}
DISCORD = {
    "DISCORD_TOKEN": "dt",
    "DISCORD_GUILD_ID": "1",
    "DISCORD_INBOX_CHANNEL_ID": "2",
}
SLACK = {
    "SLACK_BOT_TOKEN": "xoxb-1",
    "SLACK_APP_TOKEN": "xapp-1",
    "SLACK_INBOX_CHANNEL_ID": "C123",
}


def test_platform_defaults_to_discord():
    cfg = load({**COMMON, **DISCORD})
    assert cfg.platform == "discord"


def test_slack_platform_requires_slack_vars_only():
    cfg = load({**COMMON, **SLACK, "CHAT_PLATFORM": "slack"})
    assert cfg.platform == "slack"
    assert cfg.slack_bot_token == "xoxb-1"
    assert cfg.discord_token == ""


def test_missing_common_var_raises():
    env = {**COMMON, **DISCORD}
    del env["SIGNALWIRE_SIGNING_KEY"]
    with pytest.raises(ConfigError) as exc:
        load(env)
    assert "SIGNALWIRE_SIGNING_KEY" in str(exc.value)


def test_missing_platform_var_names_the_platform():
    """The error has to say which platform demanded the variable."""
    env = {**COMMON, **DISCORD}
    del env["DISCORD_GUILD_ID"]
    with pytest.raises(ConfigError) as exc:
        load(env)
    assert "DISCORD_GUILD_ID" in str(exc.value)
    assert "discord" in str(exc.value).lower()


def test_slack_vars_not_required_under_discord():
    load({**COMMON, **DISCORD})  # must not raise


def test_discord_vars_not_required_under_slack():
    load({**COMMON, **SLACK, "CHAT_PLATFORM": "slack"})  # must not raise


def test_unknown_platform_rejected():
    with pytest.raises(ConfigError) as exc:
        load({**COMMON, "CHAT_PLATFORM": "irc"})
    assert "irc" in str(exc.value)


def test_space_url_is_normalised():
    cfg = load({**COMMON, **DISCORD, "SIGNALWIRE_SPACE_URL": "https://a.signalwire.com/"})
    assert cfg.sw_space == "a.signalwire.com"


def test_public_base_url_loses_trailing_slash():
    cfg = load({**COMMON, **DISCORD, "PUBLIC_BASE_URL": "https://sms.example.com/"})
    assert cfg.public_base_url == "https://sms.example.com"


def test_invite_users_parsed_and_trimmed():
    cfg = load({**COMMON, **SLACK, "CHAT_PLATFORM": "slack",
                "SLACK_INVITE_USERS": "U1, U2 ,U3"})
    assert cfg.slack_invite_users == ("U1", "U2", "U3")


def test_invite_users_empty_by_default():
    cfg = load({**COMMON, **SLACK, "CHAT_PLATFORM": "slack"})
    assert cfg.slack_invite_users == ()


def test_media_signing_key_is_random_when_unset():
    a = load({**COMMON, **DISCORD})
    b = load({**COMMON, **DISCORD})
    assert a.media_signing_key != b.media_signing_key
    assert len(a.media_signing_key) >= 32


def test_media_signing_key_honoured_when_set():
    cfg = load({**COMMON, **DISCORD, "MEDIA_SIGNING_KEY": "shared-secret"})
    assert cfg.media_signing_key == b"shared-secret"


@pytest.mark.parametrize("raw,expected", [("true", True), ("1", True), ("yes", True),
                                          ("false", False), ("0", False), ("no", False)])
def test_boolean_parsing(raw, expected):
    cfg = load({**COMMON, **DISCORD, "REDACT_CODES": raw})
    assert cfg.redact_codes is expected


def test_defaults():
    cfg = load({**COMMON, **DISCORD})
    assert cfg.bind_host == "0.0.0.0"
    assert cfg.bind_port == 8080
    assert cfg.verify_signature is True
    assert cfg.redact_codes is True
    assert cfg.command_prefix == "!sms"
    assert cfg.note_prefix == "//"
    assert cfg.max_mms_bytes == 1048576
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_config.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'sms_bridge'`.

- [ ] **Step 3: Create the package and config module**

`sms_bridge/__init__.py`:
```python
"""SignalWire <-> chat bridge. See docs/ for architecture."""
```

`sms_bridge/config.py`:
```python
"""Environment parsing.

Required variables depend on CHAT_PLATFORM: only the selected platform's
variables are demanded, and the error names the platform that wanted them.

CHAT_PLATFORM defaults to "discord" so pre-split .env files keep working. This
does not contradict the rule that SIGNALWIRE_SIGNING_KEY must never default: a
wrong signing key fails asymmetrically and silently (sending works, receiving
403s), whereas a wrong platform exits at startup naming the missing variable.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from typing import Mapping

PLATFORMS = ("discord", "slack")
DEFAULT_MAX_MMS_BYTES = 1024 * 1024


class ConfigError(Exception):
    """Configuration is unusable. __main__ turns this into a startup exit."""


@dataclass(frozen=True)
class Config:
    platform: str

    discord_token: str
    discord_guild_id: int
    discord_category_id: int
    discord_inbox_channel_id: int
    discord_secure_channel_id: int

    slack_bot_token: str
    slack_app_token: str
    slack_inbox_channel_id: str
    slack_secure_channel_id: str
    slack_invite_users: tuple[str, ...]

    sw_space: str
    sw_project: str
    sw_token: str
    sw_signing_key: str
    sw_number: str

    public_base_url: str
    bind_host: str
    bind_port: int
    verify_signature: bool
    redact_codes: bool
    db_path: str
    heartbeat_url: str
    command_prefix: str
    note_prefix: str
    media_signing_key: bytes
    max_mms_bytes: int

    @property
    def sw_api_base(self) -> str:
        return f"https://{self.sw_space}/api/laml/2010-04-01/Accounts/{self.sw_project}"


def _flag(env: Mapping[str, str], name: str, default: str = "true") -> bool:
    return env.get(name, default).strip().lower() in ("1", "true", "yes")


def load(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    missing: list[str] = []

    platform = env.get("CHAT_PLATFORM", "discord").strip().lower()
    if platform not in PLATFORMS:
        raise ConfigError(
            f"CHAT_PLATFORM={platform!r} is not supported; expected one of "
            f"{', '.join(PLATFORMS)}"
        )

    def need(name: str, owner: str = "") -> str:
        value = (env.get(name) or "").strip()
        if not value:
            missing.append(f"{name} (required when CHAT_PLATFORM={owner})" if owner else name)
        return value

    sw_space = need("SIGNALWIRE_SPACE_URL").replace("https://", "").strip("/")
    sw_project = need("SIGNALWIRE_PROJECT_ID")
    sw_token = need("SIGNALWIRE_API_TOKEN")
    sw_signing_key = need("SIGNALWIRE_SIGNING_KEY")
    sw_number = need("SIGNALWIRE_NUMBER")
    public_base_url = need("PUBLIC_BASE_URL").rstrip("/")

    discord_token = need("DISCORD_TOKEN", "discord") if platform == "discord" else ""
    discord_guild = need("DISCORD_GUILD_ID", "discord") if platform == "discord" else "0"
    discord_inbox = need("DISCORD_INBOX_CHANNEL_ID", "discord") if platform == "discord" else "0"

    slack_bot = need("SLACK_BOT_TOKEN", "slack") if platform == "slack" else ""
    slack_app = need("SLACK_APP_TOKEN", "slack") if platform == "slack" else ""
    slack_inbox = need("SLACK_INBOX_CHANNEL_ID", "slack") if platform == "slack" else ""

    if missing:
        raise ConfigError("Missing required env vars: " + ", ".join(missing))

    raw_key = (env.get("MEDIA_SIGNING_KEY") or "").strip()
    media_key = raw_key.encode() if raw_key else secrets.token_bytes(32)

    invite = tuple(
        u.strip() for u in (env.get("SLACK_INVITE_USERS") or "").split(",") if u.strip()
    )

    return Config(
        platform=platform,
        discord_token=discord_token,
        discord_guild_id=int(discord_guild or 0),
        discord_category_id=int(env.get("DISCORD_CATEGORY_ID") or 0),
        discord_inbox_channel_id=int(discord_inbox or 0),
        discord_secure_channel_id=int(env.get("DISCORD_SECURE_CHANNEL_ID") or 0),
        slack_bot_token=slack_bot,
        slack_app_token=slack_app,
        slack_inbox_channel_id=slack_inbox,
        slack_secure_channel_id=(env.get("SLACK_SECURE_CHANNEL_ID") or "").strip(),
        slack_invite_users=invite,
        sw_space=sw_space,
        sw_project=sw_project,
        sw_token=sw_token,
        sw_signing_key=sw_signing_key,
        sw_number=sw_number,
        public_base_url=public_base_url,
        bind_host=env.get("BIND_HOST", "0.0.0.0"),
        bind_port=int(env.get("BIND_PORT") or 8080),
        verify_signature=_flag(env, "VERIFY_SIGNATURE"),
        redact_codes=_flag(env, "REDACT_CODES"),
        db_path=env.get("DB_PATH", "bridge.sqlite3"),
        heartbeat_url=(env.get("HEARTBEAT_URL") or "").strip(),
        command_prefix=env.get("COMMAND_PREFIX", "!sms"),
        note_prefix=env.get("NOTE_PREFIX", "//"),
        media_signing_key=media_key,
        max_mms_bytes=int(env.get("MAX_MMS_BYTES") or DEFAULT_MAX_MMS_BYTES),
    )
```

- [ ] **Step 4: Run to verify pass**

```bash
pytest tests/test_config.py -v
```
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add sms_bridge/ tests/test_config.py
git commit -m "feat: add sms_bridge package with platform-aware config loading"
```

---

### Task 6: Extract routing, text, and store modules

**Files:**
- Create: `sms_bridge/routing.py`, `sms_bridge/text.py`, `sms_bridge/store.py`, `tests/test_store.py`
- Modify: `tests/conftest.py`, `tests/test_routing.py`, `tests/test_text.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `routing`: `E164` (compiled pattern), `normalise_number(raw) -> str | None`, `channel_name_for(number) -> str`, `topic_for(number) -> str`, `number_from_topic(topic) -> str | None`, `TOPIC_PREFIX = "sms:"`
  - `text`: `MAX_SMS_CHARS = 1500`, `chunk(body, size=MAX_SMS_CHARS) -> list[str]`, `segment_count(body) -> int`, `looks_like_a_code(body) -> bool`
  - `store`: `class Store(path: str)` with `already_seen(sid) -> bool`, `remember_outbound(sid, channel_id: str, message_id: str) -> None`, `lookup_outbound(sid) -> tuple[str, str] | None`, `prune(days=30) -> None`, `close() -> None`

**Schema note:** `outbound.channel_id` and `message_id` become `TEXT` so Slack `ts` values fit. SQLite is dynamically typed, so existing rows still read correctly, and the table is a 30-day-pruned cache regardless.

- [ ] **Step 1: Create `sms_bridge/routing.py`**

```python
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
```

- [ ] **Step 2: Create `sms_bridge/text.py`**

Copy `GSM7`, `GSM7_EXT`, `segment_count`, the fixed `chunk` from Task 2, and `looks_like_a_code` with `CODE_KEYWORDS` / `CODE_DIGITS` verbatim out of `sms_discord_bridge.py`, with `MAX_SMS_CHARS = 1500` at the top and this module docstring:

```python
"""SMS body handling: chunking, GSM-7 segmentation, passcode detection.

segment_count is for logging only - non-GSM-7 characters (emoji, curly quotes)
drop the per-segment limit from 160 to 70, which is worth surfacing when a
message unexpectedly costs four segments instead of one.
"""
```

- [ ] **Step 3: Create `sms_bridge/store.py`**

```python
"""SQLite cache - not a source of truth.

Two tables: `seen` de-duplicates retried webhooks, `outbound` maps SignalWire
SIDs to the chat message whose reaction a delivery-status callback updates.
Deleting the file loses only in-flight reaction updates.
"""

from __future__ import annotations

import sqlite3
import time


class Store:
    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS seen (sid TEXT PRIMARY KEY, ts INTEGER)")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS outbound ("
            " sid TEXT PRIMARY KEY, channel_id TEXT, message_id TEXT, ts INTEGER)"
        )
        self._db.commit()

    def already_seen(self, sid: str) -> bool:
        """Insert-or-report. SignalWire retries webhooks; this makes them idempotent."""
        if not sid:
            return False
        try:
            self._db.execute(
                "INSERT INTO seen (sid, ts) VALUES (?, ?)", (sid, int(time.time()))
            )
            self._db.commit()
            return False
        except sqlite3.IntegrityError:
            return True

    def remember_outbound(self, sid: str, channel_id: str, message_id: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO outbound (sid, channel_id, message_id, ts)"
            " VALUES (?,?,?,?)",
            (sid, str(channel_id), str(message_id), int(time.time())),
        )
        self._db.commit()

    def lookup_outbound(self, sid: str) -> tuple[str, str] | None:
        row = self._db.execute(
            "SELECT channel_id, message_id FROM outbound WHERE sid = ?", (sid,)
        ).fetchone()
        return (row[0], row[1]) if row else None

    def prune(self, days: int = 30) -> None:
        cutoff = int(time.time()) - days * 86400
        self._db.execute("DELETE FROM seen WHERE ts < ?", (cutoff,))
        self._db.execute("DELETE FROM outbound WHERE ts < ?", (cutoff,))
        self._db.commit()

    def close(self) -> None:
        self._db.close()
```

- [ ] **Step 4: Write store tests**

`tests/test_store.py`:
```python
"""Webhook de-duplication and outbound SID mapping."""

import time

import pytest

from sms_bridge.store import Store


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def test_first_sighting_is_not_a_duplicate(store):
    assert store.already_seen("SM1") is False


def test_second_sighting_is_a_duplicate(store):
    store.already_seen("SM1")
    assert store.already_seen("SM1") is True


def test_empty_sid_is_never_a_duplicate(store):
    assert store.already_seen("") is False
    assert store.already_seen("") is False


def test_outbound_round_trip(store):
    store.remember_outbound("SM9", "C123", "1699999999.000100")
    assert store.lookup_outbound("SM9") == ("C123", "1699999999.000100")


def test_outbound_accepts_integer_ids_as_text(store):
    """Discord IDs arrive as ints; Slack ts values are strings. Both store as TEXT."""
    store.remember_outbound("SM8", 4242, 9999)
    assert store.lookup_outbound("SM8") == ("4242", "9999")


def test_unknown_sid_returns_none(store):
    assert store.lookup_outbound("nope") is None


def test_prune_drops_old_rows_only(store):
    store.remember_outbound("recent", "C1", "M1")
    old = int(time.time()) - 40 * 86400
    store._db.execute(
        "INSERT INTO outbound (sid, channel_id, message_id, ts) VALUES (?,?,?,?)",
        ("ancient", "C2", "M2", old),
    )
    store._db.commit()

    store.prune(days=30)

    assert store.lookup_outbound("recent") is not None
    assert store.lookup_outbound("ancient") is None
```

- [ ] **Step 5: Re-point the Phase 1 tests**

In `tests/test_routing.py` and `tests/test_text.py`, delete the `bridge` fixture parameter from every test and import directly instead:

```python
from sms_bridge.routing import channel_name_for, normalise_number, number_from_topic, topic_for
```
```python
from sms_bridge.text import chunk, looks_like_a_code, segment_count
```

Replace each `bridge.foo(...)` call with `foo(...)`. Leave `tests/test_signature.py` and `tests/test_markup_discord.py` on the `bridge` fixture — they move in Tasks 7 and 12.

- [ ] **Step 6: Run the full suite**

```bash
pytest -v
```
Expected: all PASS. The routing and text tests now exercise `sms_bridge`, proving the extraction preserved behaviour.

- [ ] **Step 7: Commit**

```bash
git add sms_bridge/routing.py sms_bridge/text.py sms_bridge/store.py tests/
git commit -m "refactor: extract routing, text, and store modules into sms_bridge"
```

---

### Task 7: Extract the SignalWire client

**Files:**
- Create: `sms_bridge/signalwire.py`
- Modify: `tests/test_signature.py`
- Create: `tests/test_signalwire_client.py`

**Interfaces:**
- Consumes: `sms_bridge.config.Config`
- Produces:
  - `valid_signature(url: str, params: dict[str, str], signature: str, key: str) -> bool` — module-level, `key` now required rather than defaulting to a global
  - `class SignalWire(config: Config, http: httpx.AsyncClient)` with:
    - `check(request, path, params) -> bool`
    - `explain_bad_signature(request, path, params, signature) -> str`
    - `async send_sms(to: str, body: str, media_urls: Sequence[str] = ()) -> str` (returns SID)
    - `async fetch_media(url: str) -> tuple[bytes, str, str] | None` (data, filename, content_type)
  - `MAX_UPLOAD_BYTES = 8 * 1024 * 1024`

- [ ] **Step 1: Write the failing client tests**

`tests/test_signalwire_client.py`:
```python
"""SignalWire REST calls, exercised against an httpx MockTransport."""

import httpx
import pytest

from sms_bridge.config import load
from sms_bridge.signalwire import SignalWire

ENV = {
    "SIGNALWIRE_SPACE_URL": "https://example.signalwire.com",
    "SIGNALWIRE_PROJECT_ID": "proj",
    "SIGNALWIRE_API_TOKEN": "tok",
    "SIGNALWIRE_SIGNING_KEY": "sign",
    "SIGNALWIRE_NUMBER": "+14165550100",
    "PUBLIC_BASE_URL": "https://sms.example.com",
    "DISCORD_TOKEN": "dt",
    "DISCORD_GUILD_ID": "1",
    "DISCORD_INBOX_CHANNEL_ID": "2",
}


def make(handler):
    cfg = load(ENV)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return cfg, SignalWire(cfg, client)


async def test_send_sms_posts_expected_fields():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        return httpx.Response(201, json={"sid": "SM123"})

    _, sw = make(handler)
    sid = await sw.send_sms("+14165550199", "hello")

    assert sid == "SM123"
    assert seen["url"].endswith("/Messages.json")
    assert "From=%2B14165550100" in seen["body"]
    assert "To=%2B14165550199" in seen["body"]
    assert "Body=hello" in seen["body"]
    assert "StatusCallback=" in seen["body"]


async def test_send_sms_includes_media_urls():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode()
        return httpx.Response(201, json={"sid": "SM124"})

    _, sw = make(handler)
    await sw.send_sms("+14165550199", "look", ["https://sms.example.com/media/abc"])

    assert "MediaUrl=https%3A%2F%2Fsms.example.com%2Fmedia%2Fabc" in seen["body"]


async def test_send_sms_raises_on_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "bad"})

    _, sw = make(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await sw.send_sms("+14165550199", "hello")


async def test_fetch_media_returns_bytes_and_extension():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})

    _, sw = make(handler)
    got = await sw.fetch_media("https://media.example/1")

    assert got == (b"\x89PNG", "mms.png", "image/png")


async def test_fetch_media_rejects_oversized():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"x" * (9 * 1024 * 1024), headers={"content-type": "image/png"}
        )

    _, sw = make(handler)
    assert await sw.fetch_media("https://media.example/big") is None


async def test_fetch_media_returns_none_on_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    _, sw = make(handler)
    assert await sw.fetch_media("https://media.example/gone") is None


async def test_unknown_content_type_falls_back_to_bin():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"..", headers={"content-type": "application/x-weird"})

    _, sw = make(handler)
    data, filename, ctype = await sw.fetch_media("https://media.example/2")
    assert filename == "mms.bin"
    assert ctype == "application/x-weird"
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_signalwire_client.py -v
```
Expected: FAIL — `No module named 'sms_bridge.signalwire'`.

- [ ] **Step 3: Create `sms_bridge/signalwire.py`**

```python
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
        data: list[tuple[str, str]] = [
            ("From", self._c.sw_number),
            ("To", to),
            ("Body", body),
            ("StatusCallback", f"{self._c.public_base_url}/sms/status"),
        ]
        data.extend(("MediaUrl", url) for url in media_urls)
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
        ctype = r.headers.get("content-type", "application/octet-stream").split(";")[0]
        return r.content, f"mms.{_EXTENSIONS.get(ctype, 'bin')}", ctype

    def check_signature(self, path: str, params: dict[str, str], signature: str) -> bool:
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
```

- [ ] **Step 4: Re-point the signature tests**

In `tests/test_signature.py`, replace the `bridge` fixture with a direct import and pass the key positionally:

```python
from sms_bridge.signalwire import valid_signature
```

Every call becomes `valid_signature(url, params, signature, token)`. The `key` parameter is now required, so `test_empty_params_signs_url_alone` passes `"key"` as before.

- [ ] **Step 5: Run the suite**

```bash
pytest -v
```
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add sms_bridge/signalwire.py tests/
git commit -m "refactor: extract SignalWire client and signature validation"
```

---

### Task 8: Adapter protocol and the fake adapter

**Files:**
- Create: `sms_bridge/chat/__init__.py`, `sms_bridge/chat/base.py`, `tests/fakes.py`

**Interfaces:**
- Consumes: nothing
- Produces the vocabulary every later task uses:
  - `ChannelRef(id: str)`
  - `MessageRef(channel_id: str, message_id: str)`
  - `InboundFile(filename: str, content_type: str, data: bytes)` — SMS → chat
  - `Attachment(file_id: str, filename: str, size: int)` — chat → SMS
  - `OutboundMessage(channel: ChannelRef, message: MessageRef, text: str, channel_topic: str | None, attachments: tuple[Attachment, ...])`
  - `Reaction` enum: `PENDING`, `OK`, `FAIL`
  - `SecureResult` enum: `DELIVERED`, `NOT_CONFIGURED`, `UNAVAILABLE`
  - `ChatAdapter` protocol
  - `tests/fakes.py`: `FakeAdapter`

- [ ] **Step 1: Create `sms_bridge/chat/__init__.py`**

```python
"""Chat platform adapters.

An adapter reports capability and executes instructions. It never decides
policy - in particular it never decides what happens when the secure channel
is unreachable. That decision lives in sms_bridge.delivery so it exists once.
"""
```

- [ ] **Step 2: Create `sms_bridge/chat/base.py`**

```python
"""The contract between the core and a chat platform."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Awaitable, Protocol, Sequence


class Reaction(Enum):
    """Delivery status. Adapters map these to platform emoji."""

    PENDING = "pending"
    OK = "ok"
    FAIL = "fail"


class SecureResult(Enum):
    """What happened when the adapter tried to use the secure channel.

    The adapter reports; sms_bridge.delivery decides the consequence.
    """

    DELIVERED = "delivered"
    NOT_CONFIGURED = "not_configured"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ChannelRef:
    id: str


@dataclass(frozen=True)
class MessageRef:
    channel_id: str
    message_id: str


@dataclass(frozen=True)
class InboundFile:
    """Media travelling SMS -> chat. Already downloaded."""

    filename: str
    content_type: str
    data: bytes


@dataclass(frozen=True)
class Attachment:
    """Media travelling chat -> SMS. Referenced by opaque platform file id."""

    file_id: str
    filename: str
    size: int


@dataclass(frozen=True)
class OutboundMessage:
    """A user-authored chat message that may need sending as SMS."""

    channel: ChannelRef
    message: MessageRef
    text: str
    channel_topic: str | None
    attachments: tuple[Attachment, ...] = field(default_factory=tuple)


OutboundHandler = Callable[[OutboundMessage], Awaitable[None]]


class ChatAdapter(Protocol):
    name: str
    max_post_chars: int

    async def start(self, on_outbound: OutboundHandler) -> None: ...
    async def close(self) -> None: ...
    def is_ready(self) -> bool: ...
    def latency_ms(self) -> float: ...

    async def find_channel(self, number: str) -> ChannelRef | None: ...
    async def create_channel(self, number: str) -> ChannelRef: ...
    async def post(
        self, channel: ChannelRef, text: str, files: Sequence[InboundFile] = ()
    ) -> MessageRef: ...
    async def reply(self, ref: MessageRef, text: str) -> None: ...
    async def react(self, ref: MessageRef, reaction: Reaction) -> None: ...
    async def unreact(self, ref: MessageRef, reaction: Reaction) -> None: ...

    async def post_secure(self, text: str) -> tuple[SecureResult, str]:
        """Try the secure channel. Return the outcome and an access hint.

        Must never fall back to any other channel. The hint is human-readable
        text naming what to check; it is empty when the result is DELIVERED.
        """
        ...

    async def fetch_attachment(self, file_id: str) -> tuple[bytes, str]: ...
    async def notify_inbox(self, text: str) -> None: ...
    async def check_access(self) -> None: ...
    def strip_markup(self, text: str) -> str: ...
```

- [ ] **Step 3: Create `tests/fakes.py`**

```python
"""In-memory ChatAdapter for testing delivery policy without a network."""

from __future__ import annotations

from sms_bridge.chat.base import (
    Attachment,
    ChannelRef,
    InboundFile,
    MessageRef,
    OutboundMessage,
    Reaction,
    SecureResult,
)


class FakeAdapter:
    name = "fake"
    max_post_chars = 100

    def __init__(
        self,
        secure_result: SecureResult = SecureResult.NOT_CONFIGURED,
        secure_hint: str = "",
    ) -> None:
        self.secure_result = secure_result
        self.secure_hint = secure_hint

        self.channels: dict[str, ChannelRef] = {}
        self.created: list[str] = []
        self.posts: list[tuple[str, str, tuple[InboundFile, ...]]] = []
        self.secure_posts: list[str] = []
        self.replies: list[tuple[MessageRef, str]] = []
        self.reactions: list[tuple[MessageRef, Reaction]] = []
        self.unreactions: list[tuple[MessageRef, Reaction]] = []
        self.inbox: list[str] = []
        self.attachments: dict[str, tuple[bytes, str]] = {}
        self._counter = 0

    # -- lifecycle -------------------------------------------------------
    async def start(self, on_outbound): self.on_outbound = on_outbound
    async def close(self): pass
    def is_ready(self): return True
    def latency_ms(self): return 1.0

    # -- channels --------------------------------------------------------
    async def find_channel(self, number: str) -> ChannelRef | None:
        return self.channels.get(number)

    async def create_channel(self, number: str) -> ChannelRef:
        ref = ChannelRef(id=f"chan-{number}")
        self.channels[number] = ref
        self.created.append(number)
        return ref

    # -- messages --------------------------------------------------------
    async def post(self, channel: ChannelRef, text: str, files=()) -> MessageRef:
        self._counter += 1
        self.posts.append((channel.id, text, tuple(files)))
        return MessageRef(channel_id=channel.id, message_id=str(self._counter))

    async def reply(self, ref: MessageRef, text: str) -> None:
        self.replies.append((ref, text))

    async def react(self, ref: MessageRef, reaction: Reaction) -> None:
        self.reactions.append((ref, reaction))

    async def unreact(self, ref: MessageRef, reaction: Reaction) -> None:
        self.unreactions.append((ref, reaction))

    # -- secure channel --------------------------------------------------
    async def post_secure(self, text: str) -> tuple[SecureResult, str]:
        if self.secure_result is SecureResult.DELIVERED:
            self.secure_posts.append(text)
        return self.secure_result, self.secure_hint

    # -- misc ------------------------------------------------------------
    async def fetch_attachment(self, file_id: str) -> tuple[bytes, str]:
        return self.attachments[file_id]

    async def notify_inbox(self, text: str) -> None:
        self.inbox.append(text)

    async def check_access(self) -> None:
        pass

    def strip_markup(self, text: str) -> str:
        return text

    # -- test helpers ----------------------------------------------------
    def posted_text(self) -> str:
        return "\n".join(text for _, text, _ in self.posts)

    def make_outbound(self, text: str, topic: str | None, attachments=()) -> OutboundMessage:
        return OutboundMessage(
            channel=ChannelRef(id="chan-1"),
            message=MessageRef(channel_id="chan-1", message_id="m1"),
            text=text,
            channel_topic=topic,
            attachments=tuple(attachments),
        )
```

- [ ] **Step 4: Verify the package imports cleanly**

```bash
python -c "from sms_bridge.chat.base import ChatAdapter, Reaction, SecureResult; print('ok')"
pytest -v
```
Expected: `ok`, and the existing suite still all PASS.

- [ ] **Step 5: Commit**

```bash
git add sms_bridge/chat/ tests/fakes.py
git commit -m "feat: define the ChatAdapter protocol and an in-memory fake"
```

---

### Task 9: Delivery core and the passcode decision table

**Files:**
- Create: `sms_bridge/delivery.py`, `tests/test_delivery.py`

**Interfaces:**
- Consumes: `Config`, `Store`, `SignalWire`, `ChatAdapter`, `MediaTokens` (Task 12 — passed as `None` until then)
- Produces:
  - `InboundSms(sender: str, body: str, media_urls: tuple[str, ...], sid: str)`
  - `class Delivery(config, adapter, store, signalwire, media=None)` with:
    - `async handle_inbound(sms: InboundSms) -> None`
    - `async handle_outbound(msg: OutboundMessage) -> None`
    - `async update_status(sid: str, status: str, error_code: str) -> None`
    - `async run_worker(queue: asyncio.Queue) -> None`
  - `SUPPRESSED_NOTICE` constant

This is the task that carries the safety-critical logic. Every branch of the passcode decision table gets a test.

- [ ] **Step 1: Write the failing tests**

`tests/test_delivery.py`:
```python
"""Delivery policy: passcode suppression, caption folding, chunking, dispatch.

These tests own the safety-critical rule: a passcode must never reach a contact
channel. Every branch of the decision table is covered.
"""

import httpx
import pytest

from sms_bridge.chat.base import Attachment, Reaction, SecureResult
from sms_bridge.config import load
from sms_bridge.delivery import Delivery, InboundSms
from sms_bridge.signalwire import SignalWire
from sms_bridge.store import Store
from tests.fakes import FakeAdapter

ENV = {
    "SIGNALWIRE_SPACE_URL": "https://example.signalwire.com",
    "SIGNALWIRE_PROJECT_ID": "proj",
    "SIGNALWIRE_API_TOKEN": "tok",
    "SIGNALWIRE_SIGNING_KEY": "sign",
    "SIGNALWIRE_NUMBER": "+14165550100",
    "PUBLIC_BASE_URL": "https://sms.example.com",
    "DISCORD_TOKEN": "dt",
    "DISCORD_GUILD_ID": "1",
    "DISCORD_INBOX_CHANNEL_ID": "2",
}

CONTACT = "+14165550123"


def build(adapter=None, media_bodies=None, env_extra=None, send_handler=None):
    """Wire a Delivery against fakes.

    media_bodies maps a media URL to (bytes, content-type) so caption folding
    can be exercised without a network.
    """
    cfg = load({**ENV, **(env_extra or {})})
    adapter = adapter or FakeAdapter()
    media_bodies = media_bodies or {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in media_bodies:
            data, ctype = media_bodies[url]
            return httpx.Response(200, content=data, headers={"content-type": ctype})
        if url.startswith("https://media.example/"):
            return httpx.Response(404)  # an unregistered media URL is unfetchable
        if send_handler is not None:
            return send_handler(request)
        return httpx.Response(201, json={"sid": "SM-sent"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sw = SignalWire(cfg, http)
    store = Store(":memory:")
    return Delivery(cfg, adapter, store, sw), adapter, store


# --------------------------------------------------------------------------
# Passcode decision table
# --------------------------------------------------------------------------

async def test_passcode_with_no_secure_channel_is_suppressed():
    delivery, adapter, _ = build(FakeAdapter(secure_result=SecureResult.NOT_CONFIGURED))

    await delivery.handle_inbound(
        InboundSms(sender=CONTACT, body="Your code is 445566", media_urls=(), sid="SM1")
    )

    assert "445566" not in adapter.posted_text()
    assert adapter.secure_posts == []
    assert len(adapter.posts) == 1, "a placeholder still goes to the contact channel"
    assert "suppressed" in adapter.posts[0][1].lower()


async def test_passcode_with_unavailable_secure_channel_is_suppressed_and_reported():
    adapter = FakeAdapter(
        secure_result=SecureResult.UNAVAILABLE,
        secure_hint="check that the bot can view #secure",
    )
    delivery, adapter, _ = build(adapter)

    await delivery.handle_inbound(
        InboundSms(sender=CONTACT, body="Your code is 445566", media_urls=(), sid="SM2")
    )

    assert "445566" not in adapter.posted_text()
    assert "445566" not in "".join(adapter.inbox)
    assert len(adapter.inbox) == 1
    assert "check that the bot can view #secure" in adapter.inbox[0]
    assert CONTACT in adapter.inbox[0]
    assert "suppressed" in adapter.posts[0][1].lower()


async def test_passcode_with_working_secure_channel_goes_there_only():
    delivery, adapter, _ = build(FakeAdapter(secure_result=SecureResult.DELIVERED))

    await delivery.handle_inbound(
        InboundSms(sender=CONTACT, body="Your code is 445566", media_urls=(), sid="SM3")
    )

    assert adapter.secure_posts and "445566" in adapter.secure_posts[0]
    assert CONTACT in adapter.secure_posts[0]
    assert adapter.posts == [], "nothing at all goes to the contact channel"
    assert adapter.created == [], "no contact channel is created for a delivered code"


async def test_redaction_disabled_lets_the_code_through():
    delivery, adapter, _ = build(env_extra={"REDACT_CODES": "false"})

    await delivery.handle_inbound(
        InboundSms(sender=CONTACT, body="Your code is 445566", media_urls=(), sid="SM4")
    )

    assert "445566" in adapter.posted_text()


async def test_non_code_message_is_posted_normally():
    delivery, adapter, _ = build()

    await delivery.handle_inbound(
        InboundSms(sender=CONTACT, body="see you at 8", media_urls=(), sid="SM5")
    )

    assert "see you at 8" in adapter.posted_text()
    assert adapter.created == [CONTACT]


# --------------------------------------------------------------------------
# MMS caption folding - ordering is load-bearing
# --------------------------------------------------------------------------

async def test_text_plain_media_part_is_folded_into_the_body():
    url = "https://media.example/caption"
    delivery, adapter, _ = build(media_bodies={url: (b"look at this", "text/plain")})

    await delivery.handle_inbound(
        InboundSms(sender=CONTACT, body="", media_urls=(url,), sid="SM6")
    )

    assert "look at this" in adapter.posted_text()
    assert adapter.posts[0][2] == (), "the caption must not upload as a stray mms.bin"


async def test_passcode_sent_as_an_mms_caption_is_still_redacted():
    """The reason media is fetched before the passcode check."""
    url = "https://media.example/codecap"
    delivery, adapter, _ = build(
        FakeAdapter(secure_result=SecureResult.NOT_CONFIGURED),
        media_bodies={url: (b"Your code is 998877", "text/plain")},
    )

    await delivery.handle_inbound(
        InboundSms(sender=CONTACT, body="", media_urls=(url,), sid="SM7")
    )

    assert "998877" not in adapter.posted_text()
    assert "suppressed" in adapter.posts[0][1].lower()


async def test_binary_media_is_attached_not_folded():
    url = "https://media.example/pic"
    delivery, adapter, _ = build(media_bodies={url: (b"\x89PNG", "image/png")})

    await delivery.handle_inbound(
        InboundSms(sender=CONTACT, body="pic", media_urls=(url,), sid="SM8")
    )

    files = adapter.posts[0][2]
    assert len(files) == 1
    assert files[0].content_type == "image/png"
    assert files[0].data == b"\x89PNG"


async def test_unfetchable_media_is_noted_in_the_body():
    """A 404 on media must not lose the text that did arrive."""
    delivery, adapter, _ = build()  # not registered -> the transport returns 404

    await delivery.handle_inbound(
        InboundSms(sender=CONTACT, body="hi", media_urls=("https://media.example/x",),
                   sid="SM9")
    )

    posted = adapter.posted_text()
    assert "hi" in posted
    assert "unfetchable" in posted
    assert adapter.posts[0][2] == ()


# --------------------------------------------------------------------------
# Chunking on the way in
# --------------------------------------------------------------------------

async def test_long_inbound_body_is_chunked_at_adapter_limit():
    delivery, adapter, _ = build()
    body = "x" * 250  # FakeAdapter.max_post_chars is 100

    await delivery.handle_inbound(
        InboundSms(sender=CONTACT, body=body, media_urls=(), sid="SM10")
    )

    assert len(adapter.posts) == 3
    assert all(len(text) <= 100 for _, text, _ in adapter.posts)


async def test_files_attach_only_to_the_first_chunk():
    url = "https://media.example/pic"
    delivery, adapter, _ = build(media_bodies={url: (b"\x89PNG", "image/png")})

    await delivery.handle_inbound(
        InboundSms(sender=CONTACT, body="y" * 250, media_urls=(url,), sid="SM11")
    )

    assert len(adapter.posts[0][2]) == 1
    assert all(files == () for _, _, files in adapter.posts[1:])


async def test_empty_body_gets_a_placeholder():
    delivery, adapter, _ = build()

    await delivery.handle_inbound(
        InboundSms(sender=CONTACT, body="", media_urls=(), sid="SM12")
    )

    assert adapter.posts[0][1] != ""


async def test_existing_channel_is_reused():
    delivery, adapter, _ = build()
    await adapter.create_channel(CONTACT)
    adapter.created.clear()

    await delivery.handle_inbound(
        InboundSms(sender=CONTACT, body="hello", media_urls=(), sid="SM13")
    )

    assert adapter.created == []


# --------------------------------------------------------------------------
# Outbound
# --------------------------------------------------------------------------

async def test_outbound_from_contact_channel_sends_and_marks_pending():
    delivery, adapter, store = build()
    msg = adapter.make_outbound("hi there", topic=f"sms:{CONTACT}")

    await delivery.handle_outbound(msg)

    assert (msg.message, Reaction.PENDING) in adapter.reactions
    assert store.lookup_outbound("SM-sent") == ("chan-1", "m1")


async def test_outbound_ignored_without_a_topic_number():
    delivery, adapter, store = build()

    await delivery.handle_outbound(adapter.make_outbound("hi", topic="just a channel"))

    assert adapter.reactions == []
    assert store.lookup_outbound("SM-sent") is None


async def test_note_prefix_is_not_sent():
    delivery, adapter, store = build()

    await delivery.handle_outbound(
        adapter.make_outbound("// remember to call back", topic=f"sms:{CONTACT}")
    )

    assert adapter.reactions == []
    assert store.lookup_outbound("SM-sent") is None


async def test_command_opens_a_channel_without_sending():
    delivery, adapter, _ = build()

    await delivery.handle_outbound(adapter.make_outbound("!sms 4165550123", topic=None))

    assert adapter.created == [CONTACT]
    assert adapter.replies, "the user is told the channel is ready"


async def test_command_with_body_sends_immediately():
    delivery, adapter, store = build()

    await delivery.handle_outbound(
        adapter.make_outbound("!sms 4165550123 on my way", topic=None)
    )

    assert adapter.created == [CONTACT]
    assert store.lookup_outbound("SM-sent") is not None


async def test_command_with_bad_number_replies_and_sends_nothing():
    delivery, adapter, store = build()

    await delivery.handle_outbound(adapter.make_outbound("!sms banana hi", topic=None))

    assert adapter.created == []
    assert store.lookup_outbound("SM-sent") is None
    assert adapter.replies


async def test_send_failure_marks_fail_and_replies():
    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "nope"})

    delivery, adapter, _ = build(send_handler=boom)

    await delivery.handle_outbound(adapter.make_outbound("hi", topic=f"sms:{CONTACT}"))

    assert (adapter.reactions[-1][1]) is Reaction.FAIL
    assert adapter.replies


# --------------------------------------------------------------------------
# Status callbacks
# --------------------------------------------------------------------------

async def test_delivered_status_swaps_pending_for_ok():
    delivery, adapter, store = build()
    store.remember_outbound("SM-x", "chan-1", "m1")

    await delivery.update_status("SM-x", "delivered", "")

    assert any(r is Reaction.OK for _, r in adapter.reactions)
    assert any(r is Reaction.PENDING for _, r in adapter.unreactions)


async def test_failed_status_swaps_pending_for_fail_and_explains():
    delivery, adapter, store = build()
    store.remember_outbound("SM-y", "chan-1", "m1")

    await delivery.update_status("SM-y", "failed", "30008")

    assert any(r is Reaction.FAIL for _, r in adapter.reactions)
    assert adapter.replies and "30008" in adapter.replies[0][1]


async def test_unknown_sid_is_ignored():
    delivery, adapter, _ = build()

    await delivery.update_status("never-seen", "delivered", "")

    assert adapter.reactions == []


async def test_intermediate_status_does_nothing():
    delivery, adapter, store = build()
    store.remember_outbound("SM-z", "chan-1", "m1")

    await delivery.update_status("SM-z", "sent", "")

    assert adapter.reactions == []
    assert adapter.unreactions == []
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_delivery.py -v
```
Expected: FAIL — `No module named 'sms_bridge.delivery'`.

- [ ] **Step 3: Create `sms_bridge/delivery.py`**

````python
"""Platform-agnostic message handling.

This module owns every policy decision. Adapters report what they can do; the
rules about what happens next live here, so they exist once and are testable
against a fake.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from .chat.base import (
    Attachment,
    ChannelRef,
    InboundFile,
    MessageRef,
    OutboundMessage,
    Reaction,
    SecureResult,
)
from .config import Config
from .routing import normalise_number, number_from_topic
from .signalwire import SignalWire
from .store import Store
from .text import chunk, looks_like_a_code, segment_count

log = logging.getLogger("bridge.delivery")

SUPPRESSED_NOTICE = (
    "_(message contained a passcode - suppressed. Read it in the "
    "SignalWire message logs.)_"
)
NO_BODY_NOTICE = "_(no text body)_"


@dataclass(frozen=True)
class InboundSms:
    sender: str
    body: str
    media_urls: tuple[str, ...] = field(default_factory=tuple)
    sid: str = ""


class Delivery:
    def __init__(
        self,
        config: Config,
        adapter,
        store: Store,
        signalwire: SignalWire,
        media=None,
    ) -> None:
        self._c = config
        self._adapter = adapter
        self._store = store
        self._sw = signalwire
        self._media = media

    # -- inbound ---------------------------------------------------------

    async def handle_inbound(self, sms: InboundSms) -> None:
        body = sms.body
        files: list[InboundFile] = []
        captions: list[str] = []

        # Resolve media first. Carriers routinely deliver an MMS caption as its
        # own text/plain part instead of in Body, and that text has to be folded
        # back in *before* the passcode check below - otherwise a code sent as a
        # caption skips redaction and lands in the contact channel.
        for url in sms.media_urls:
            got = await self._sw.fetch_media(url)
            if not got:
                body = (body + f"\n_(attachment too large or unfetchable: {url})_").strip()
                continue
            data, filename, ctype = got
            if ctype == "text/plain":
                caption = data.decode("utf-8", "replace").strip()
                if caption:
                    captions.append(caption)
                continue
            files.append(InboundFile(filename=filename, content_type=ctype, data=data))

        if captions:
            body = "\n".join([body, *captions]).strip()

        if self._c.redact_codes and looks_like_a_code(body):
            await self._handle_passcode(sms.sender, body)
            return

        channel = await self._channel_for(sms.sender)
        content = body or NO_BODY_NOTICE
        limit = self._adapter.max_post_chars
        pending = files
        for piece in [content[i:i + limit] for i in range(0, len(content), limit)]:
            await self._adapter.post(channel, piece, pending)
            pending = []  # only attach to the first chunk

    async def _handle_passcode(self, sender: str, body: str) -> None:
        """The one rule that must never bend: a code never reaches a contact channel."""
        result, hint = await self._adapter.post_secure(f"**{sender}**\n```{body}```")

        if result is SecureResult.DELIVERED:
            return

        if result is SecureResult.UNAVAILABLE:
            # Configured but unusable. Without this the code silently takes the
            # "no secure channel" path and nobody learns it went nowhere.
            log.error("passcode not delivered: %s", hint)
            await self._adapter.notify_inbox(
                f"Passcode from **{sender}** was suppressed: {hint}."
            )

        channel = await self._channel_for(sender)
        await self._adapter.post(channel, SUPPRESSED_NOTICE)

    async def _channel_for(self, number: str) -> ChannelRef:
        existing = await self._adapter.find_channel(number)
        if existing is not None:
            return existing
        channel = await self._adapter.create_channel(number)
        log.info("created channel for %s", number)
        return channel

    # -- outbound --------------------------------------------------------

    async def handle_outbound(self, msg: OutboundMessage) -> None:
        text = msg.text or ""

        if text.startswith(self._c.note_prefix):
            return  # internal note: visible in chat, never sent

        if text.startswith(self._c.command_prefix):
            await self._handle_command(msg, text)
            return

        to = number_from_topic(msg.channel_topic)
        if not to:
            return  # not a contact channel

        await self._send(msg, to, text)

    async def _handle_command(self, msg: OutboundMessage, text: str) -> None:
        parts = text[len(self._c.command_prefix):].strip().split(None, 1)
        if not parts:
            await self._adapter.reply(
                msg.message, f"Usage: `{self._c.command_prefix} +14165550123 message`"
            )
            return

        number = normalise_number(parts[0])
        if not number:
            await self._adapter.reply(msg.message, "That doesn't look like a phone number.")
            return

        channel = await self._channel_for(number)
        if len(parts) > 1 and parts[1].strip():
            await self._send(msg, number, parts[1])
        else:
            await self._adapter.reply(msg.message, f"Channel ready: {channel.id}")

    async def _send(self, msg: OutboundMessage, to: str, raw: str) -> None:
        body = self._adapter.strip_markup(raw)
        media_urls = await self._media_urls_for(msg)

        if not body and not media_urls:
            return

        segments = segment_count(body)
        if segments > 1:
            log.info("outbound to %s is %d segments", to, segments)

        await self._adapter.react(msg.message, Reaction.PENDING)
        try:
            pieces = chunk(body) if body else [""]
            for index, piece in enumerate(pieces):
                sid = await self._sw.send_sms(
                    to, piece, media_urls if index == 0 else ()
                )
                self._store.remember_outbound(
                    sid, msg.message.channel_id, msg.message.message_id
                )
        except Exception as exc:  # noqa: BLE001
            log.exception("send failed")
            await self._adapter.unreact(msg.message, Reaction.PENDING)
            await self._adapter.react(msg.message, Reaction.FAIL)
            await self._adapter.reply(msg.message, f"Send failed: `{exc}`")

    async def _media_urls_for(self, msg: OutboundMessage) -> list[str]:
        """Overridden in Task 14 to mint signed URLs. Empty until then."""
        return []

    # -- status ----------------------------------------------------------

    async def update_status(self, sid: str, status: str, error_code: str) -> None:
        ref = self._store.lookup_outbound(sid)
        if not ref:
            return
        channel_id, message_id = ref
        message = MessageRef(channel_id=channel_id, message_id=message_id)

        if status == "delivered":
            await self._adapter.unreact(message, Reaction.PENDING)
            await self._adapter.react(message, Reaction.OK)
        elif status in ("failed", "undelivered"):
            await self._adapter.unreact(message, Reaction.PENDING)
            await self._adapter.react(message, Reaction.FAIL)
            detail = f" (error {error_code})" if error_code else ""
            await self._adapter.reply(message, f"Carrier reported `{status}`{detail}")

    # -- worker ----------------------------------------------------------

    async def run_worker(self, queue: asyncio.Queue) -> None:
        while True:
            sms: InboundSms = await queue.get()
            try:
                await self.handle_inbound(sms)
            except Exception:  # noqa: BLE001
                log.exception("failed to deliver inbound message")
                await self._adapter.notify_inbox(
                    f"Failed to deliver inbound SMS from {sms.sender}. Check logs."
                )
            finally:
                queue.task_done()
````

- [ ] **Step 4: Run the tests**

```bash
pytest tests/test_delivery.py -v
```
Expected: all PASS. If `test_command_opens_a_channel_without_sending` fails on the reply text, the adapter returns a bare channel id rather than a mention — that is correct at this layer; adapters add their own mention formatting in Tasks 10 and 17.

- [ ] **Step 5: Run the whole suite**

```bash
pytest -v
```
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add sms_bridge/delivery.py tests/test_delivery.py
git commit -m "feat: add platform-agnostic delivery core with passcode policy

Every branch of the passcode decision table now lives in one place and is
covered by tests driving a fake adapter."
```

---

### Task 10: Discord adapter

**Files:**
- Create: `sms_bridge/chat/discord.py`
- Modify: `tests/test_markup_discord.py`

**Interfaces:**
- Consumes: `ChatAdapter` protocol, `Config`
- Produces: `class DiscordAdapter(config: Config)` implementing `ChatAdapter`, plus module-level `strip_markup(text) -> str` and `access_hint(channel_id, label) -> str`.

- [ ] **Step 1: Create `sms_bridge/chat/discord.py`**

````python
"""Discord adapter.

Channels are found by scanning guild.text_channels for an `sms:+E164` topic
token. That scan is free: discord.py keeps the channel list in a local cache
maintained by the gateway.
"""

from __future__ import annotations

import io
import logging
import re
from typing import Sequence

import discord

from ..config import Config
from ..routing import channel_name_for, number_from_topic, topic_for
from .base import (
    Attachment,
    ChannelRef,
    InboundFile,
    MessageRef,
    OutboundMessage,
    Reaction,
    SecureResult,
)

log = logging.getLogger("bridge.discord")

_EMOJI = {
    Reaction.PENDING: "\N{HOURGLASS WITH FLOWING SAND}",
    Reaction.OK: "\N{WHITE HEAVY CHECK MARK}",
    Reaction.FAIL: "\N{CROSS MARK}",
}

_CODEBLOCK = re.compile(r"```(?:[a-zA-Z0-9+-]*\n)?(.*?)```", re.S)
_INLINE = re.compile(r"`([^`]*)`")
# Asterisk, tilde and spoiler delimiters pair freely, which matches how Discord
# renders them - it really does italicise across `5*x + 3*y`.
_EMPHASIS = re.compile(r"(\*\*\*|\*\*|\*|~~|\|\|)(.+?)\1", re.S)
# Underscores need word boundaries, which Discord also requires: it does not
# italicise snake_case_name. Without this guard any even number of underscores
# flattened the span between them, so `SIGNALWIRE_API_TOKEN` reached the handset
# as `SIGNALWIREAPITOKEN` - env var names are exactly what people paste when
# troubleshooting this bridge.
_UNDERSCORE = re.compile(r"(?<![A-Za-z0-9_])(___|__|_)(.+?)\1(?![A-Za-z0-9_])", re.S)
_CUSTOM_EMOJI = re.compile(r"<a?:([A-Za-z0-9_]+):\d+>")
_MENTION = re.compile(r"<[@#][!&]?\d+>")


def strip_markup(text: str) -> str:
    """Discord formatting is literal text over SMS. Flatten it."""
    text = _CODEBLOCK.sub(lambda m: m.group(1).strip(), text)
    text = _INLINE.sub(r"\1", text)
    for _ in range(3):  # nested emphasis
        text = _EMPHASIS.sub(r"\2", text)
        text = _UNDERSCORE.sub(r"\2", text)
    text = _CUSTOM_EMOJI.sub(r":\1:", text)
    text = _MENTION.sub("", text)
    text = re.sub(r"^>\s?", "", text, flags=re.M)
    return text.strip()


def access_hint(channel_id: int, label: str) -> str:
    """Actionable text for a channel the bot cannot use.

    Discord reports "channel deleted", "bot was never given access" and "a
    channel-level override denies it" all as 50001 Missing Access, so name the
    places worth checking rather than repeating the API's wording.
    """
    return (
        f"cannot post in the {label} channel ({channel_id}) - check that the channel "
        "still exists, that the bot's role has View Channel + Send Messages on it, and "
        "that no channel-level permission override denies either"
    )


class DiscordAdapter:
    name = "discord"
    max_post_chars = 1900  # Discord's hard cap is 2000

    def __init__(self, config: Config) -> None:
        self._c = config
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        self._client = discord.Client(intents=intents)
        self._on_outbound = None
        self._ready = False

        @self._client.event
        async def on_ready() -> None:
            log.info("logged in as %s", self._client.user)
            self._ready = True

        @self._client.event
        async def on_message(message: discord.Message) -> None:
            if message.author.bot or message.guild is None:
                return
            if message.guild.id != self._c.discord_guild_id:
                return
            if self._on_outbound is None:
                return
            await self._on_outbound(self._to_outbound(message))

    # -- lifecycle -------------------------------------------------------

    async def start(self, on_outbound) -> None:
        self._on_outbound = on_outbound
        await self._client.start(self._c.discord_token)

    async def close(self) -> None:
        await self._client.close()

    def is_ready(self) -> bool:
        return self._client.is_ready()

    def latency_ms(self) -> float:
        return round((self._client.latency or 0) * 1000, 1)

    # -- translation -----------------------------------------------------

    def _to_outbound(self, message: discord.Message) -> OutboundMessage:
        return OutboundMessage(
            channel=ChannelRef(id=str(message.channel.id)),
            message=MessageRef(
                channel_id=str(message.channel.id), message_id=str(message.id)
            ),
            text=message.content or "",
            channel_topic=getattr(message.channel, "topic", None),
            attachments=tuple(
                Attachment(file_id=str(a.id), filename=a.filename, size=a.size)
                for a in message.attachments
            ),
        )

    @property
    def _guild(self) -> discord.Guild:
        guild = self._client.get_guild(self._c.discord_guild_id)
        if guild is None:
            raise RuntimeError(f"Bot is not in guild {self._c.discord_guild_id}")
        return guild

    # -- channels --------------------------------------------------------

    async def find_channel(self, number: str) -> ChannelRef | None:
        for ch in self._guild.text_channels:
            if number_from_topic(ch.topic) == number:
                return ChannelRef(id=str(ch.id))
        return None

    async def create_channel(self, number: str) -> ChannelRef:
        category = (
            self._guild.get_channel(self._c.discord_category_id)
            if self._c.discord_category_id
            else None
        )
        channel = await self._guild.create_text_channel(
            name=channel_name_for(number),
            topic=topic_for(number),
            category=category if isinstance(category, discord.CategoryChannel) else None,
            reason="New SMS contact",
        )
        await self.notify_inbox(f"New contact **{number}** -> {channel.mention}")
        log.info("created channel %s for %s", channel.name, number)
        return ChannelRef(id=str(channel.id))

    # -- messages --------------------------------------------------------

    async def post(
        self, channel: ChannelRef, text: str, files: Sequence[InboundFile] = ()
    ) -> MessageRef:
        target = self._client.get_channel(int(channel.id))
        if target is None:
            raise RuntimeError(access_hint(int(channel.id), "contact"))
        sent = await target.send(
            text,
            files=[
                discord.File(io.BytesIO(f.data), filename=f.filename) for f in files
            ],
        )
        return MessageRef(channel_id=channel.id, message_id=str(sent.id))

    async def _message(self, ref: MessageRef) -> discord.Message | None:
        channel = self._client.get_channel(int(ref.channel_id))
        if channel is None:
            return None
        try:
            return await channel.fetch_message(int(ref.message_id))
        except discord.NotFound:
            return None

    async def reply(self, ref: MessageRef, text: str) -> None:
        message = await self._message(ref)
        if message is not None:
            await message.reply(text, mention_author=False)

    async def react(self, ref: MessageRef, reaction: Reaction) -> None:
        message = await self._message(ref)
        if message is not None:
            await message.add_reaction(_EMOJI[reaction])

    async def unreact(self, ref: MessageRef, reaction: Reaction) -> None:
        message = await self._message(ref)
        if message is None:
            return
        try:
            await message.remove_reaction(_EMOJI[reaction], self._client.user)
        except discord.HTTPException:
            pass

    # -- secure channel --------------------------------------------------

    async def post_secure(self, text: str) -> tuple[SecureResult, str]:
        channel_id = self._c.discord_secure_channel_id
        if not channel_id:
            return SecureResult.NOT_CONFIGURED, ""

        channel = self._client.get_channel(channel_id)
        if channel is None:
            return SecureResult.UNAVAILABLE, access_hint(channel_id, "secure")

        try:
            await channel.send(text)
        except discord.Forbidden:
            return SecureResult.UNAVAILABLE, access_hint(channel_id, "secure")
        return SecureResult.DELIVERED, ""

    # -- misc ------------------------------------------------------------

    async def fetch_attachment(self, file_id: str) -> tuple[bytes, str]:
        raise NotImplementedError("implemented in Task 13")

    async def notify_inbox(self, text: str) -> None:
        """Best-effort operator notice. Never raises: callers use it on error paths."""
        channel_id = self._c.discord_inbox_channel_id
        inbox = self._client.get_channel(channel_id)
        if inbox is None:
            log.error("%s (wanted to report: %s)", access_hint(channel_id, "inbox"), text)
            return
        try:
            await inbox.send(text)
        except discord.Forbidden:
            log.error("%s (wanted to report: %s)", access_hint(channel_id, "inbox"), text)

    async def check_access(self) -> None:
        """Report unusable channels at startup instead of when a message needs them.

        The secure channel is the one that matters: nothing routine writes to it,
        so a permissions mistake there stays invisible until a passcode arrives.
        """
        targets = [(self._c.discord_inbox_channel_id, "inbox")]
        if self._c.discord_secure_channel_id:
            targets.append((self._c.discord_secure_channel_id, "secure"))
        for channel_id, label in targets:
            channel = self._client.get_channel(channel_id)
            if channel is None:
                log.error("startup check: %s", access_hint(channel_id, label))
                continue
            try:
                perms = channel.permissions_for(channel.guild.me)
            except Exception:  # noqa: BLE001
                log.warning("startup check: could not read permissions on %s channel", label)
                continue
            if perms.view_channel and perms.send_messages:
                log.info("startup check: %s channel #%s is writable", label, channel.name)
            else:
                log.error("startup check: %s", access_hint(channel_id, label))

    def strip_markup(self, text: str) -> str:
        return strip_markup(text)
````

- [ ] **Step 2: Re-point the markup tests**

In `tests/test_markup_discord.py`, drop the `bridge` fixture and import directly:

```python
from sms_bridge.chat.discord import strip_markup
```

Replace `bridge.strip_discord_markup(...)` with `strip_markup(...)` throughout.

**Then flip the four known-bug tests, because this task fixes the bug they pinned.**
Task 4 pinned the broken underscore behaviour deliberately; the `_UNDERSCORE` rule above
corrects it. Delete `test_url_underscores_are_stripped_known_bug` and
`test_snake_case_identifiers_are_mangled_known_bug`, and replace them with:

```python
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
```

Keep `test_paired_asterisks_eat_multiplication_known_bug` and
`test_a_single_underscore_or_asterisk_survives` exactly as they are — asterisk pairing is
**not** a bug. Discord genuinely italicises across `5*x + 3*y`, so flattening it is correct
behaviour for SMS, and that test still documents it accurately.

Verify the underscore forms that *are* emphasis still flatten: `_italic_` → `italic` and
`__underline__` → `underline` are already covered by the parametrized table.

- [ ] **Step 3: Run the suite**

```bash
pytest -v
```
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add sms_bridge/chat/discord.py tests/test_markup_discord.py
git commit -m "feat: add Discord chat adapter implementing ChatAdapter"
```

---

### Task 11: Webhook server, entry point, and removal of the old module

**Files:**
- Create: `sms_bridge/server.py`, `sms_bridge/__main__.py`, `tests/test_server.py`
- Delete: `sms_discord_bridge.py`
- Modify: `Dockerfile`, `sms-bridge.service`, `tests/conftest.py`

**Interfaces:**
- Consumes: everything from Tasks 5–10
- Produces:
  - `sms_bridge.server.create_app(config, signalwire, store, delivery, queue, adapter) -> FastAPI`
  - `sms_bridge.__main__.build_adapter(config) -> ChatAdapter`
  - `sms_bridge.__main__.main()`

- [ ] **Step 1: Write the failing server tests**

`tests/test_server.py`:
```python
"""Webhook endpoints: signature enforcement, dedup, enqueueing."""

import asyncio
import base64
import hashlib
import hmac

import httpx
import pytest
from fastapi.testclient import TestClient

from sms_bridge.config import load
from sms_bridge.delivery import Delivery
from sms_bridge.server import create_app
from sms_bridge.signalwire import SignalWire
from sms_bridge.store import Store
from tests.fakes import FakeAdapter

ENV = {
    "SIGNALWIRE_SPACE_URL": "https://example.signalwire.com",
    "SIGNALWIRE_PROJECT_ID": "proj",
    "SIGNALWIRE_API_TOKEN": "tok",
    "SIGNALWIRE_SIGNING_KEY": "sign",
    "SIGNALWIRE_NUMBER": "+14165550100",
    "PUBLIC_BASE_URL": "https://sms.example.com",
    "DISCORD_TOKEN": "dt",
    "DISCORD_GUILD_ID": "1",
    "DISCORD_INBOX_CHANNEL_ID": "2",
}


def sign(path: str, params: dict[str, str], key: str = "sign") -> str:
    payload = f"https://sms.example.com{path}" + "".join(
        f"{k}{params[k]}" for k in sorted(params)
    )
    return base64.b64encode(
        hmac.new(key.encode(), payload.encode(), hashlib.sha1).digest()
    ).decode()


@pytest.fixture
def app_bits():
    cfg = load(ENV)
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(201, json={"sid": "SM1"}))
    )
    sw = SignalWire(cfg, http)
    store = Store(":memory:")
    adapter = FakeAdapter()
    queue: asyncio.Queue = asyncio.Queue()
    delivery = Delivery(cfg, adapter, store, sw)
    app = create_app(cfg, sw, store, delivery, queue, adapter)
    return app, queue, store, adapter


def test_inbound_rejects_bad_signature(app_bits):
    app, queue, _, _ = app_bits
    with TestClient(app) as client:
        r = client.post(
            "/sms/inbound",
            data={"From": "+14165550123", "Body": "hi", "MessageSid": "SM1"},
            headers={"X-Twilio-Signature": "wrong"},
        )
    assert r.status_code == 403
    assert queue.qsize() == 0


def test_inbound_accepts_good_signature_and_enqueues(app_bits):
    app, queue, _, _ = app_bits
    params = {"From": "+14165550123", "Body": "hi", "MessageSid": "SM1", "NumMedia": "0"}
    with TestClient(app) as client:
        r = client.post(
            "/sms/inbound", data=params, headers={"X-Twilio-Signature": sign("/sms/inbound", params)}
        )
    assert r.status_code == 200
    assert queue.qsize() == 1
    sms = queue.get_nowait()
    assert sms.sender == "+14165550123"
    assert sms.body == "hi"
    assert sms.sid == "SM1"


def test_inbound_collects_media_urls(app_bits):
    app, queue, _, _ = app_bits
    params = {
        "From": "+14165550123", "Body": "", "MessageSid": "SM2", "NumMedia": "2",
        "MediaUrl0": "https://m/0", "MediaUrl1": "https://m/1",
    }
    with TestClient(app) as client:
        client.post(
            "/sms/inbound", data=params, headers={"X-Twilio-Signature": sign("/sms/inbound", params)}
        )
    sms = queue.get_nowait()
    assert sms.media_urls == ("https://m/0", "https://m/1")


def test_duplicate_sid_is_not_enqueued_twice(app_bits):
    app, queue, _, _ = app_bits
    params = {"From": "+14165550123", "Body": "hi", "MessageSid": "SM3", "NumMedia": "0"}
    headers = {"X-Twilio-Signature": sign("/sms/inbound", params)}
    with TestClient(app) as client:
        client.post("/sms/inbound", data=params, headers=headers)
        r = client.post("/sms/inbound", data=params, headers=headers)
    assert r.status_code == 200
    assert queue.qsize() == 1


def test_signature_check_can_be_disabled(app_bits):
    cfg = load({**ENV, "VERIFY_SIGNATURE": "false"})
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(201, json={"sid": "S"}))
    )
    sw = SignalWire(cfg, http)
    store = Store(":memory:")
    queue: asyncio.Queue = asyncio.Queue()
    adapter = FakeAdapter()
    app = create_app(cfg, sw, store, Delivery(cfg, adapter, store, sw), queue, adapter)
    with TestClient(app) as client:
        r = client.post("/sms/inbound", data={"From": "+1", "MessageSid": "SM4"})
    assert r.status_code == 200


def test_status_rejects_bad_signature(app_bits):
    app, _, _, _ = app_bits
    with TestClient(app) as client:
        r = client.post(
            "/sms/status",
            data={"MessageSid": "SM1", "MessageStatus": "delivered"},
            headers={"X-Twilio-Signature": "wrong"},
        )
    assert r.status_code == 403


def test_healthz_reports_adapter_state(app_bits):
    app, _, _, _ = app_bits
    with TestClient(app) as client:
        r = client.get("/healthz")
    body = r.json()
    assert body["ok"] is True
    assert body["queued"] == 0
    assert body["platform"] == "fake"


def test_docs_are_disabled(app_bits):
    app, _, _, _ = app_bits
    with TestClient(app) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_server.py -v
```
Expected: FAIL — `No module named 'sms_bridge.server'`.

- [ ] **Step 3: Create `sms_bridge/server.py`**

```python
"""SignalWire webhook endpoints.

Handlers return within milliseconds - SignalWire retries on slow responses - so
inbound messages are enqueued and the slow chat work happens in the worker.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI, Request, Response

from .config import Config
from .delivery import Delivery, InboundSms
from .signalwire import SignalWire
from .store import Store

log = logging.getLogger("bridge.server")


def create_app(
    config: Config,
    signalwire: SignalWire,
    store: Store,
    delivery: Delivery,
    queue: asyncio.Queue,
    adapter,
) -> FastAPI:
    api = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    async def check(request: Request, path: str) -> dict[str, str] | None:
        form = await request.form()
        params = {k: str(v) for k, v in form.items()}
        if config.verify_signature:
            signature = request.headers.get("X-Twilio-Signature", "")
            if not signalwire.check_signature(path, params, signature):
                log.warning(
                    "rejected request with bad signature on %s: %s",
                    path,
                    signalwire.explain_bad_signature(request, path, params, signature),
                )
                return None
        return params

    @api.post("/sms/inbound")
    async def inbound(request: Request) -> Response:
        params = await check(request, "/sms/inbound")
        if params is None:
            return Response(status_code=403)

        sid = params.get("MessageSid", "")
        if store.already_seen(sid):
            log.info("duplicate webhook for %s, ignoring", sid)
            return Response(content="<Response/>", media_type="application/xml")

        media = []
        for i in range(int(params.get("NumMedia", "0") or 0)):
            url = params.get(f"MediaUrl{i}")
            if url:
                media.append(url)

        await queue.put(
            InboundSms(
                sender=params.get("From", ""),
                body=(params.get("Body") or "").strip(),
                media_urls=tuple(media),
                sid=sid,
            )
        )
        return Response(content="<Response/>", media_type="application/xml")

    @api.post("/sms/status")
    async def status(request: Request) -> Response:
        params = await check(request, "/sms/status")
        if params is None:
            return Response(status_code=403)
        asyncio.create_task(
            delivery.update_status(
                params.get("MessageSid", ""),
                (params.get("MessageStatus") or "").lower(),
                params.get("ErrorCode", ""),
            )
        )
        return Response(status_code=204)

    @api.get("/healthz")
    async def healthz() -> dict:
        return {
            "ok": adapter.is_ready(),
            "platform": adapter.name,
            "queued": queue.qsize(),
            "latency_ms": adapter.latency_ms(),
        }

    return api
```

- [ ] **Step 4: Create `sms_bridge/__main__.py`**

```python
"""Entry point. Runs the chat adapter and the webhook server on one event loop."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

import httpx
import uvicorn

from .config import Config, ConfigError, load
from .delivery import Delivery
from .server import create_app
from .signalwire import SignalWire
from .store import Store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("bridge")


def build_adapter(config: Config):
    if config.platform == "discord":
        from .chat.discord import DiscordAdapter

        return DiscordAdapter(config)
    if config.platform == "slack":
        from .chat.slack import SlackAdapter

        return SlackAdapter(config)
    raise ConfigError(f"no adapter for platform {config.platform!r}")


async def heartbeat_loop(http: httpx.AsyncClient, url: str) -> None:
    while True:
        try:
            await http.get(url, timeout=10)
        except Exception as exc:  # noqa: BLE001
            log.warning("heartbeat failed: %s", exc)
        await asyncio.sleep(300)


async def run(config: Config) -> None:
    http = httpx.AsyncClient()
    store = Store(config.db_path)
    store.prune()

    signalwire = SignalWire(config, http)
    adapter = build_adapter(config)
    queue: asyncio.Queue = asyncio.Queue()
    delivery = Delivery(config, adapter, store, signalwire)

    app = create_app(config, signalwire, store, delivery, queue, adapter)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.bind_host,
            port=config.bind_port,
            log_level="warning",
            access_log=False,
        )
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # e.g. Windows
            pass

    log.info("platform=%s", config.platform)
    asyncio.create_task(server.serve())
    log.info(
        "webhook listening on %s:%s (public: %s)",
        config.bind_host,
        config.bind_port,
        config.public_base_url,
    )
    asyncio.create_task(delivery.run_worker(queue))
    if config.heartbeat_url:
        asyncio.create_task(heartbeat_loop(http, config.heartbeat_url))
    asyncio.create_task(adapter.start(delivery.handle_outbound))

    await stop.wait()
    log.info("shutdown signal received, closing")
    server.should_exit = True
    await adapter.close()
    await http.aclose()
    store.close()


def main() -> None:
    try:
        config = load()
    except ConfigError as exc:
        sys.exit(str(exc))
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run the server tests**

```bash
pytest tests/test_server.py -v
```
Expected: all PASS.

- [ ] **Step 6: Delete the old module and simplify conftest**

```bash
git rm sms_discord_bridge.py
```

`tests/conftest.py` no longer needs the env bootstrap or the `bridge` fixture — every test now imports `sms_bridge` directly and passes config explicitly. Replace the whole file with:

```python
"""Shared test configuration.

sms_bridge modules take configuration as parameters rather than reading the
environment at import time, so no bootstrap is needed here.
"""
```

- [ ] **Step 7: Update the Dockerfile and systemd unit**

In `Dockerfile`, replace `COPY sms_discord_bridge.py .` with:
```dockerfile
COPY sms_bridge/ ./sms_bridge/
```
and replace the final `ENTRYPOINT` line (currently `ENTRYPOINT ["python", "sms_discord_bridge.py"]`) with:
```dockerfile
ENTRYPOINT ["python", "-m", "sms_bridge"]
```
Leave the `ENV` block, the non-root user, `VOLUME`, `EXPOSE`, and `HEALTHCHECK` untouched.

In `sms-bridge.service`, change `ExecStart` to:
```ini
ExecStart=/opt/sms-bridge/venv/bin/python -m sms_bridge
```

- [ ] **Step 8: Run the full suite and a smoke start**

```bash
pytest -v
python -m sms_bridge 2>&1 | head -5   # expect the missing-env-var exit, not a traceback
```
Expected: all tests PASS; the smoke run exits with `Missing required env vars: ...`.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "refactor: replace the single-file bridge with the sms_bridge package

Entry point becomes 'python -m sms_bridge'. Behaviour is unchanged; the Phase 1
tests now exercise the extracted modules."
```

- [ ] **Step 10: Verify against the live deployment**

Build the image, deploy it, and confirm before continuing:
1. `GET /healthz` returns `{"ok": true, "platform": "discord", ...}`
2. An inbound SMS creates or reuses a contact channel and posts
3. A reply in a contact channel sends, shows ⏳, then ✅
4. An inbound MMS with a caption folds the caption into the body
5. An inbound passcode is suppressed or routed to the secure channel per config

Do not proceed to Phase 3 until all five behave as they did before.

---

# Phase 3 — Real MMS

---

### Task 12: Signed media tokens

**Files:**
- Create: `sms_bridge/media.py`, `tests/test_media_token.py`

**Interfaces:**
- Consumes: `Config.media_signing_key`
- Produces: `class MediaTokens(key: bytes, ttl_seconds: int = 600)` with `mint(file_id: str) -> str` and `verify(token: str) -> str | None` (returns the file id, or `None` when invalid, tampered, or expired).

- [ ] **Step 1: Write the failing tests**

`tests/test_media_token.py`:
```python
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
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_media_token.py -v
```
Expected: FAIL — `No module named 'sms_bridge.media'`.

- [ ] **Step 3: Create `sms_bridge/media.py`**

```python
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
```

- [ ] **Step 4: Run to verify pass**

```bash
pytest tests/test_media_token.py -v
```
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add sms_bridge/media.py tests/test_media_token.py
git commit -m "feat: add signed expiring tokens for outbound media URLs"
```

---

### Task 13: The `/media/{token}` endpoint and Discord attachment fetching

**Files:**
- Modify: `sms_bridge/server.py`, `sms_bridge/chat/discord.py`, `sms_bridge/__main__.py`, `tests/fakes.py`, `tests/test_server.py`

**Interfaces:**
- Consumes: `MediaTokens`, `ChatAdapter.fetch_attachment`
- Produces: `create_app(config, signalwire, store, delivery, queue, adapter, media)` — signature gains a trailing `media: MediaTokens` parameter; `GET /media/{token}` returns 200 with the bytes, or 404 for any invalid, expired, or unresolvable token.

**Why 404 and not 403:** an invalid token and a missing file are indistinguishable to a caller, which is the point — the route reveals nothing about which file ids exist.

- [ ] **Step 1: Add the failing endpoint tests**

Append to `tests/test_server.py`:
```python
def test_media_endpoint_serves_a_valid_token():
    from sms_bridge.media import MediaTokens

    cfg = load(ENV)
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(201, json={"sid": "S"}))
    )
    sw = SignalWire(cfg, http)
    store = Store(":memory:")
    adapter = FakeAdapter()
    adapter.attachments["F1"] = (b"\x89PNG-data", "image/png")
    media = MediaTokens(cfg.media_signing_key)
    queue: asyncio.Queue = asyncio.Queue()
    app = create_app(cfg, sw, store, Delivery(cfg, adapter, store, sw), queue, adapter, media)

    with TestClient(app) as client:
        r = client.get(f"/media/{media.mint('F1')}")

    assert r.status_code == 200
    assert r.content == b"\x89PNG-data"
    assert r.headers["content-type"].startswith("image/png")
    assert r.headers["cache-control"] == "no-store"


def test_media_endpoint_rejects_a_forged_token():
    from sms_bridge.media import MediaTokens

    cfg = load(ENV)
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(201, json={"sid": "S"}))
    )
    sw = SignalWire(cfg, http)
    store = Store(":memory:")
    adapter = FakeAdapter()
    adapter.attachments["F1"] = (b"data", "image/png")
    queue: asyncio.Queue = asyncio.Queue()
    app = create_app(
        cfg, sw, store, Delivery(cfg, adapter, store, sw), queue, adapter,
        MediaTokens(cfg.media_signing_key),
    )
    forged = MediaTokens(b"attacker key attacker key attac").mint("F1")

    with TestClient(app) as client:
        assert client.get(f"/media/{forged}").status_code == 404
        assert client.get("/media/garbage").status_code == 404


def test_media_endpoint_404s_for_an_unknown_file():
    from sms_bridge.media import MediaTokens

    cfg = load(ENV)
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(201, json={"sid": "S"}))
    )
    sw = SignalWire(cfg, http)
    store = Store(":memory:")
    adapter = FakeAdapter()  # no attachments registered
    media = MediaTokens(cfg.media_signing_key)
    queue: asyncio.Queue = asyncio.Queue()
    app = create_app(cfg, sw, store, Delivery(cfg, adapter, store, sw), queue, adapter, media)

    with TestClient(app) as client:
        assert client.get(f"/media/{media.mint('missing')}").status_code == 404
```

Update the existing `app_bits` fixture and `test_signature_check_can_be_disabled` to pass a `MediaTokens(cfg.media_signing_key)` as the new final argument to `create_app`.

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_server.py -v
```
Expected: FAIL — `create_app() takes 6 positional arguments but 7 were given`.

- [ ] **Step 3: Add the route**

In `sms_bridge/server.py`, add `from .media import MediaTokens` and extend the signature:
```python
def create_app(
    config: Config,
    signalwire: SignalWire,
    store: Store,
    delivery: Delivery,
    queue: asyncio.Queue,
    adapter,
    media: MediaTokens,
) -> FastAPI:
```

Then add this route before `return api`:
```python
    @api.get("/media/{token}")
    async def outbound_media(token: str) -> Response:
        """Serve a chat attachment to SignalWire for MMS.

        Guarded solely by the token's HMAC and expiry. Every failure returns 404
        so the route never discloses which file ids exist.
        """
        file_id = media.verify(token)
        if file_id is None:
            log.warning("rejected media request with an invalid or expired token")
            return Response(status_code=404)
        try:
            data, content_type = await adapter.fetch_attachment(file_id)
        except Exception:  # noqa: BLE001
            log.exception("could not fetch attachment %s", file_id)
            return Response(status_code=404)
        return Response(
            content=data,
            media_type=content_type,
            headers={"Cache-Control": "no-store"},
        )
```

- [ ] **Step 4: Make `FakeAdapter.fetch_attachment` raise for unknown ids**

In `tests/fakes.py`, change the method to:
```python
    async def fetch_attachment(self, file_id: str) -> tuple[bytes, str]:
        if file_id not in self.attachments:
            raise KeyError(file_id)
        return self.attachments[file_id]
```

- [ ] **Step 5: Implement Discord attachment fetching**

In `sms_bridge/chat/discord.py`, replace the `NotImplementedError` stub. Discord attachment URLs are not stable identifiers, so the adapter resolves the attachment through the message it belongs to. Store the URL at translation time instead, which is simpler and avoids a second API round trip — change `_to_outbound` to use the attachment URL as the `file_id`:

```python
            attachments=tuple(
                Attachment(file_id=a.url, filename=a.filename, size=a.size)
                for a in message.attachments
            ),
```

and implement:
```python
    async def fetch_attachment(self, file_id: str) -> tuple[bytes, str]:
        """file_id is the CDN URL captured when the message arrived.

        Those URLs are signed and expire in roughly 24 hours, which is far longer
        than a token's 10-minute life, so no refresh is needed.
        """
        async with httpx.AsyncClient() as http:
            r = await http.get(file_id, follow_redirects=True, timeout=20)
            r.raise_for_status()
        ctype = r.headers.get("content-type", "application/octet-stream").split(";")[0]
        return r.content, ctype
```

Add `import httpx` to the module imports.

- [ ] **Step 6: Wire it in `__main__.py`**

Add `from .media import MediaTokens`, then in `run()`:
```python
    media = MediaTokens(config.media_signing_key)
    delivery = Delivery(config, adapter, store, signalwire, media)
    app = create_app(config, signalwire, store, delivery, queue, adapter, media)
```

- [ ] **Step 7: Run the suite**

```bash
pytest -v
```
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add sms_bridge/ tests/
git commit -m "feat: serve chat attachments over a signed media endpoint"
```

---

### Task 14: Send real MMS

**Files:**
- Modify: `sms_bridge/delivery.py`, `tests/test_delivery.py`

**Interfaces:**
- Consumes: `MediaTokens`, `Config.max_mms_bytes`, `Config.public_base_url`
- Produces: `Delivery._media_urls_for(msg)` mints a signed URL per attachment, skipping and reporting anything over `max_mms_bytes`.

- [ ] **Step 1: Add the failing tests**

Append to `tests/test_delivery.py`:
```python
# --------------------------------------------------------------------------
# Outbound media
# --------------------------------------------------------------------------

def build_with_media(adapter=None, env_extra=None):
    from sms_bridge.media import MediaTokens

    cfg = load({**ENV, **(env_extra or {})})
    adapter = adapter or FakeAdapter()
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.content.decode())
        return httpx.Response(201, json={"sid": "SM-sent"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sw = SignalWire(cfg, http)
    store = Store(":memory:")
    media = MediaTokens(cfg.media_signing_key)
    return Delivery(cfg, adapter, store, sw, media), adapter, sent, media


async def test_attachment_becomes_a_signed_media_url():
    delivery, adapter, sent, media = build_with_media()
    msg = adapter.make_outbound(
        "look", topic=f"sms:{CONTACT}",
        attachments=[Attachment(file_id="F1", filename="a.png", size=100)],
    )

    await delivery.handle_outbound(msg)

    assert "MediaUrl=" in sent[0]
    assert "sms.example.com%2Fmedia%2F" in sent[0]


async def test_oversized_attachment_is_skipped_and_reported():
    delivery, adapter, sent, _ = build_with_media(env_extra={"MAX_MMS_BYTES": "50"})
    msg = adapter.make_outbound(
        "look", topic=f"sms:{CONTACT}",
        attachments=[Attachment(file_id="F1", filename="big.png", size=100)],
    )

    await delivery.handle_outbound(msg)

    assert "MediaUrl=" not in sent[0]
    assert adapter.replies and "big.png" in adapter.replies[0][1]


async def test_media_attaches_only_to_the_first_chunk():
    delivery, adapter, sent, _ = build_with_media()
    msg = adapter.make_outbound(
        "z" * 4000, topic=f"sms:{CONTACT}",
        attachments=[Attachment(file_id="F1", filename="a.png", size=10)],
    )

    await delivery.handle_outbound(msg)

    assert len(sent) > 1
    assert "MediaUrl=" in sent[0]
    assert all("MediaUrl=" not in body for body in sent[1:])


async def test_attachment_with_no_text_still_sends():
    delivery, adapter, sent, _ = build_with_media()
    msg = adapter.make_outbound(
        "", topic=f"sms:{CONTACT}",
        attachments=[Attachment(file_id="F1", filename="a.png", size=10)],
    )

    await delivery.handle_outbound(msg)

    assert len(sent) == 1
    assert "MediaUrl=" in sent[0]


async def test_no_media_tokens_means_no_media_urls():
    """Delivery constructed without a MediaTokens must not crash on attachments."""
    delivery, adapter, _ = build()
    msg = adapter.make_outbound(
        "look", topic=f"sms:{CONTACT}",
        attachments=[Attachment(file_id="F1", filename="a.png", size=10)],
    )

    await delivery.handle_outbound(msg)  # must not raise
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_delivery.py -k media -v
```
Expected: FAIL — no `MediaUrl` in the request body, because `_media_urls_for` still returns `[]`.

- [ ] **Step 3: Implement `_media_urls_for`**

Replace the stub in `sms_bridge/delivery.py`:
```python
    async def _media_urls_for(self, msg: OutboundMessage) -> list[str]:
        """Mint a short-lived signed URL per attachment.

        Anything over max_mms_bytes is skipped with a visible reply rather than
        failing silently: carriers commonly reject large MMS regardless of what
        the API accepts.
        """
        if self._media is None or not msg.attachments:
            return []

        urls: list[str] = []
        for attachment in msg.attachments:
            if attachment.size > self._c.max_mms_bytes:
                await self._adapter.reply(
                    msg.message,
                    f"`{attachment.filename}` is {attachment.size} bytes, over the "
                    f"{self._c.max_mms_bytes}-byte MMS limit - not sent.",
                )
                continue
            token = self._media.mint(attachment.file_id)
            urls.append(f"{self._c.public_base_url}/media/{token}")
        return urls
```

- [ ] **Step 4: Run the suite**

```bash
pytest -v
```
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add sms_bridge/delivery.py tests/test_delivery.py
git commit -m "feat: send chat attachments as real MMS instead of a link in the body

Discord CDN links are signed and expire after roughly 24 hours, so appending
the URL degraded for anyone reading the SMS a day later."
```

- [ ] **Step 6: Verify against the live deployment**

Redeploy and confirm: sending an image in a Discord contact channel delivers it as a picture message, not a URL. Check the bridge log for a `/media/` request from SignalWire.

---

# Phase 4 — Slack adapter

---

### Task 15: Slack markup stripping

**Files:**
- Create: `tests/test_markup_slack.py`
- Create: `sms_bridge/chat/slack_markup.py`

**Interfaces:**
- Consumes: nothing
- Produces: `sms_bridge.chat.slack_markup.strip_markup(text: str) -> str`

Kept in its own module so the pure function is testable without importing `slack_sdk`.

**Slack mrkdwn differs from Discord's markdown in ways that are easy to get wrong:** `*bold*` is bold in Slack but *italic* in Discord; links are `<url|label>`; channel references are `<#C123|name>`; user mentions are `<@U123>`; and `&`, `<`, `>` arrive HTML-escaped.

- [ ] **Step 1: Write the failing tests**

`tests/test_markup_slack.py`:
````python
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
````

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_markup_slack.py -v
```
Expected: FAIL — `No module named 'sms_bridge.chat.slack_markup'`.

- [ ] **Step 3: Create `sms_bridge/chat/slack_markup.py`**

````python
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
````

- [ ] **Step 4: Run to verify pass**

```bash
pytest tests/test_markup_slack.py -v
```
Expected: all PASS. If `test_mixed_message` fails on spacing, the mention removal left a double space — the trailing `re.sub` collapses runs of spaces, so check that it ran.

- [ ] **Step 5: Commit**

```bash
git add sms_bridge/chat/slack_markup.py tests/test_markup_slack.py
git commit -m "feat: add Slack mrkdwn stripping for outbound SMS bodies"
```

---

### Task 16: Slack channel index

**Files:**
- Create: `sms_bridge/chat/slack_index.py`, `tests/test_slack_index.py`

**Interfaces:**
- Consumes: nothing (takes an async lister callable, so it is testable without `slack_sdk`)
- Produces: `class ChannelIndex(list_conversations: Callable[[str | None], Awaitable[dict]])` with:
  - `async refresh() -> None`
  - `async lookup(number: str) -> str | None` (channel id)
  - `remember(number: str, channel_id: str) -> None`
  - `forget(channel_id: str) -> None`
  - `apply_event(event: dict) -> None`

**Why this exists:** Discord keeps channels in a local gateway-maintained cache, so scanning per message is free. Slack has no equivalent, and `conversations.list` is tier-2 rate limited at roughly 20 requests per minute — a per-message scan would exhaust the budget immediately. The index is derived, in-memory, disposable, and rebuildable from channel topics at any time, so the topic-as-routing-table design is preserved and nothing is persisted.

- [ ] **Step 1: Write the failing tests**

`tests/test_slack_index.py`:
```python
"""In-memory topic -> channel index for Slack."""

import pytest

from sms_bridge.chat.slack_index import ChannelIndex


def make_lister(pages):
    """pages: list of (channels, next_cursor) tuples."""
    calls = []

    async def lister(cursor=None):
        calls.append(cursor)
        channels, next_cursor = pages[len(calls) - 1]
        return {
            "channels": channels,
            "response_metadata": {"next_cursor": next_cursor or ""},
        }

    lister.calls = calls
    return lister


def ch(cid, topic):
    return {"id": cid, "topic": {"value": topic}}


async def test_refresh_indexes_topics():
    lister = make_lister([([ch("C1", "sms:+14165550123")], None)])
    index = ChannelIndex(lister)

    await index.refresh()

    assert await index.lookup("+14165550123") == "C1"


async def test_refresh_follows_pagination():
    lister = make_lister([
        ([ch("C1", "sms:+14165550101")], "cursor2"),
        ([ch("C2", "sms:+14165550102")], None),
    ])
    index = ChannelIndex(lister)

    await index.refresh()

    assert await index.lookup("+14165550101") == "C1"
    assert await index.lookup("+14165550102") == "C2"
    assert lister.calls == [None, "cursor2"]


async def test_channels_without_a_topic_token_are_ignored():
    lister = make_lister([([ch("C1", "general chat"), ch("C2", "")], None)])
    index = ChannelIndex(lister)

    await index.refresh()

    assert await index.lookup("+14165550123") is None


async def test_lookup_miss_triggers_exactly_one_refresh():
    lister = make_lister([
        ([], None),
        ([ch("C9", "sms:+14165550199")], None),
    ])
    index = ChannelIndex(lister)
    await index.refresh()          # call 1: empty

    found = await index.lookup("+14165550199")  # call 2: refresh on miss

    assert found == "C9"
    assert len(lister.calls) == 2


async def test_second_consecutive_miss_does_not_refresh_again():
    """A number with no channel must not cost a conversations.list per message."""
    lister = make_lister([([], None), ([], None), ([], None)])
    index = ChannelIndex(lister)
    await index.refresh()

    assert await index.lookup("+14165550199") is None
    calls_after_first_miss = len(lister.calls)
    assert await index.lookup("+14165550199") is None

    assert len(lister.calls) == calls_after_first_miss


async def test_remember_makes_a_new_channel_findable_without_a_refresh():
    lister = make_lister([([], None)])
    index = ChannelIndex(lister)
    await index.refresh()

    index.remember("+14165550123", "C5")

    assert await index.lookup("+14165550123") == "C5"
    assert len(lister.calls) == 1


async def test_forget_removes_by_channel_id():
    lister = make_lister([([ch("C1", "sms:+14165550123")], None), ([], None)])
    index = ChannelIndex(lister)
    await index.refresh()

    index.forget("C1")

    assert await index.lookup("+14165550123") is None


@pytest.mark.parametrize("event_type", ["channel_created", "channel_rename"])
async def test_create_and_rename_events_update_the_index(event_type):
    lister = make_lister([([], None)])
    index = ChannelIndex(lister)
    await index.refresh()

    index.apply_event({
        "type": event_type,
        "channel": {"id": "C7", "topic": {"value": "sms:+14165550177"}},
    })

    assert await index.lookup("+14165550177") == "C7"


async def test_archive_event_removes_the_channel():
    lister = make_lister([([ch("C1", "sms:+14165550123")], None), ([], None)])
    index = ChannelIndex(lister)
    await index.refresh()

    index.apply_event({"type": "channel_archive", "channel": "C1"})

    assert await index.lookup("+14165550123") is None
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_slack_index.py -v
```
Expected: FAIL — `No module named 'sms_bridge.chat.slack_index'`.

- [ ] **Step 3: Create `sms_bridge/chat/slack_index.py`**

```python
"""Topic -> channel index for Slack.

Slack has no local channel cache and conversations.list is tier-2 rate limited
(around 20 requests per minute), so the per-message scan the Discord adapter
does for free would exhaust the budget here.

This index is derived state: in-memory, never persisted, and rebuildable from
channel topics at any moment. Channel topics remain the routing table.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from ..routing import number_from_topic

log = logging.getLogger("bridge.slack.index")

Lister = Callable[..., Awaitable[dict]]


class ChannelIndex:
    def __init__(self, list_conversations: Lister) -> None:
        self._list = list_conversations
        self._by_number: dict[str, str] = {}
        self._missing: set[str] = set()

    async def refresh(self) -> None:
        found: dict[str, str] = {}
        cursor: str | None = None
        while True:
            page = await self._list(cursor)
            for channel in page.get("channels", []):
                topic = (channel.get("topic") or {}).get("value", "")
                number = number_from_topic(topic)
                if number:
                    found[number] = channel["id"]
            cursor = (page.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                break
        self._by_number = found
        self._missing.clear()
        log.info("channel index rebuilt: %d contact channels", len(found))

    async def lookup(self, number: str) -> str | None:
        hit = self._by_number.get(number)
        if hit is not None:
            return hit

        # A miss may mean the index is stale, so refresh once - but remember the
        # miss, or every message from an unknown number would cost a full
        # paginated conversations.list.
        if number in self._missing:
            return None

        await self.refresh()
        hit = self._by_number.get(number)
        if hit is None:
            self._missing.add(number)
        return hit

    def remember(self, number: str, channel_id: str) -> None:
        self._by_number[number] = channel_id
        self._missing.discard(number)

    def forget(self, channel_id: str) -> None:
        for number, cid in list(self._by_number.items()):
            if cid == channel_id:
                del self._by_number[number]

    def apply_event(self, event: dict) -> None:
        """Keep the index current from channel_* events."""
        kind = event.get("type")
        channel = event.get("channel")

        if kind in ("channel_created", "channel_rename") and isinstance(channel, dict):
            number = number_from_topic((channel.get("topic") or {}).get("value", ""))
            if number:
                self.remember(number, channel["id"])
            return

        if kind in ("channel_archive", "channel_deleted"):
            cid = channel if isinstance(channel, str) else (channel or {}).get("id")
            if cid:
                self.forget(cid)
```

- [ ] **Step 4: Run to verify pass**

```bash
pytest tests/test_slack_index.py -v
```
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add sms_bridge/chat/slack_index.py tests/test_slack_index.py
git commit -m "feat: add rate-limit-aware Slack channel index"
```

---

### Task 17: Slack adapter

**Files:**
- Create: `sms_bridge/chat/slack.py`
- Modify: `requirements.in`, `requirements.txt`

**Interfaces:**
- Consumes: `ChannelIndex`, `slack_markup.strip_markup`, `Config`
- Produces: `class SlackAdapter(config: Config)` implementing `ChatAdapter`.

- [ ] **Step 1: Add the dependency**

Resolve the current release and pin it exactly:
```bash
pip index versions slack-sdk
```

Append to `requirements.in`:
```
# Slack adapter. Raw slack_sdk rather than Bolt: the bridge has no slash
# commands or interactivity, so Bolt's app framework would be dead weight.
# Its Socket Mode client uses aiohttp, which discord.py already brings in.
slack-sdk==<resolved version>
```

Regenerate the lock:
```bash
docker run --rm -v "$PWD":/w -w /w python:3.12-slim sh -c \
  "pip install -q pip-tools && pip-compile --generate-hashes --strip-extras \
   -o requirements.txt requirements.in"
```

- [ ] **Step 2: Create `sms_bridge/chat/slack.py`**

```python
"""Slack adapter, over Socket Mode.

Socket Mode rather than the Events API: it mirrors discord.py's gateway model,
needs no second public endpoint, and keeps the FastAPI app exclusively
SignalWire's - so "the signature check is the only auth on the webhook" stays
true.

Contact channels are created private. Slack workspaces are usually shared,
unlike the single private guild the Discord deployment assumes, so
workspace-visible SMS threads would be the wrong default.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Sequence

import httpx
from slack_sdk.errors import SlackApiError
from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

from ..config import Config
from ..routing import channel_name_for, topic_for
from .base import (
    Attachment,
    ChannelRef,
    InboundFile,
    MessageRef,
    OutboundMessage,
    Reaction,
    SecureResult,
)
from .slack_index import ChannelIndex
from .slack_markup import strip_markup

log = logging.getLogger("bridge.slack")

_EMOJI = {
    Reaction.PENDING: "hourglass_flowing_sand",
    Reaction.OK: "white_check_mark",
    Reaction.FAIL: "x",
}

# Subtypes that are edits, deletions, joins and the like rather than a user
# sending something new.
_IGNORED_SUBTYPES = {
    "message_changed", "message_deleted", "message_replied",
    "channel_join", "channel_leave", "channel_topic", "channel_purpose",
    "bot_message",
}


def access_hint(channel_id: str, label: str, error: str) -> str:
    """Actionable text for a Slack channel the bot cannot use.

    Slack names its failures precisely, unlike Discord's catch-all 50001, so
    each one gets its own remedy rather than a list of things to check.
    """
    remedies = {
        "channel_not_found": "the channel id is wrong, or the bot cannot see it",
        "not_in_channel": "invite the bot to the channel with /invite @yourbot",
        "is_archived": "the channel is archived; unarchive it or point at another",
        "missing_scope": "the app is missing an OAuth scope; reinstall it after adding one",
    }
    remedy = remedies.get(error, f"Slack reported {error!r}")
    return f"cannot post in the {label} channel ({channel_id}) - {remedy}"


class SlackAdapter:
    name = "slack"
    max_post_chars = 3800  # Slack's practical text limit is around 4000

    def __init__(self, config: Config) -> None:
        self._c = config
        self._web = AsyncWebClient(token=config.slack_bot_token)
        self._socket = SocketModeClient(
            app_token=config.slack_app_token, web_client=self._web
        )
        self._index = ChannelIndex(self._list_conversations)
        self._on_outbound = None
        self._bot_user_id = ""
        self._ready = False

    # -- lifecycle -------------------------------------------------------

    async def _list_conversations(self, cursor: str | None = None) -> dict:
        response = await self._web.conversations_list(
            types="public_channel,private_channel",
            exclude_archived=True,
            limit=200,
            cursor=cursor or None,
        )
        return response.data

    async def start(self, on_outbound) -> None:
        self._on_outbound = on_outbound

        auth = await self._web.auth_test()
        self._bot_user_id = auth["user_id"]
        log.info("logged in as %s (%s)", auth.get("user"), self._bot_user_id)

        await self._index.refresh()

        self._socket.socket_mode_request_listeners.append(self._handle_request)
        await self._socket.connect()
        self._ready = True

    async def close(self) -> None:
        self._ready = False
        await self._socket.close()

    def is_ready(self) -> bool:
        return self._ready

    def latency_ms(self) -> float:
        return 0.0  # Socket Mode exposes no round-trip metric

    # -- event handling --------------------------------------------------

    async def _handle_request(self, client: SocketModeClient, req: SocketModeRequest) -> None:
        # Acknowledge first: Slack disconnects a client that is slow to ack.
        await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        if req.type != "events_api":
            return
        event = (req.payload or {}).get("event") or {}
        try:
            await self._dispatch(event)
        except Exception:  # noqa: BLE001
            log.exception("failed handling %s event", event.get("type"))

    async def _dispatch(self, event: dict) -> None:
        kind = event.get("type")

        if kind in ("channel_created", "channel_rename", "channel_archive", "channel_deleted"):
            self._index.apply_event(event)
            return

        if kind != "message" or self._on_outbound is None:
            return
        if event.get("subtype") in _IGNORED_SUBTYPES:
            return
        if event.get("bot_id") or event.get("user") == self._bot_user_id:
            return
        # Thread replies stay inside the workspace: they are the discussion
        # space, not outbound traffic.
        if event.get("thread_ts") and event["thread_ts"] != event.get("ts"):
            return

        channel_id = event.get("channel", "")
        topic = await self._topic_of(channel_id)
        await self._on_outbound(
            OutboundMessage(
                channel=ChannelRef(id=channel_id),
                message=MessageRef(channel_id=channel_id, message_id=event["ts"]),
                text=event.get("text") or "",
                channel_topic=topic,
                attachments=tuple(
                    Attachment(
                        file_id=f["id"],
                        filename=f.get("name", "file"),
                        size=int(f.get("size", 0)),
                    )
                    for f in event.get("files", [])
                ),
            )
        )

    async def _topic_of(self, channel_id: str) -> str | None:
        try:
            info = await self._web.conversations_info(channel=channel_id)
        except SlackApiError:
            return None
        return ((info["channel"].get("topic") or {}).get("value")) or None

    # -- channels --------------------------------------------------------

    async def find_channel(self, number: str) -> ChannelRef | None:
        channel_id = await self._index.lookup(number)
        return ChannelRef(id=channel_id) if channel_id else None

    async def create_channel(self, number: str) -> ChannelRef:
        created = await self._web.conversations_create(
            name=channel_name_for(number), is_private=True
        )
        channel_id = created["channel"]["id"]
        await self._web.conversations_setTopic(channel=channel_id, topic=topic_for(number))
        self._index.remember(number, channel_id)

        if self._c.slack_invite_users:
            try:
                await self._web.conversations_invite(
                    channel=channel_id, users=",".join(self._c.slack_invite_users)
                )
            except SlackApiError as exc:
                log.warning("could not invite operators to %s: %s", channel_id, exc)

        await self.notify_inbox(f"New contact *{number}* -> <#{channel_id}>")
        log.info("created channel %s for %s", channel_id, number)
        return ChannelRef(id=channel_id)

    # -- messages --------------------------------------------------------

    async def post(
        self, channel: ChannelRef, text: str, files: Sequence[InboundFile] = ()
    ) -> MessageRef:
        if files:
            first = files[0]
            uploaded = await self._web.files_upload_v2(
                channel=channel.id,
                file=first.data,
                filename=first.filename,
                initial_comment=text or None,
            )
            for extra in files[1:]:
                await self._web.files_upload_v2(
                    channel=channel.id, file=extra.data, filename=extra.filename
                )
            ts = uploaded.get("file", {}).get("shares", {})
            return MessageRef(channel_id=channel.id, message_id=_first_share_ts(ts) or "")

        sent = await self._web.chat_postMessage(channel=channel.id, text=text)
        return MessageRef(channel_id=channel.id, message_id=sent["ts"])

    async def reply(self, ref: MessageRef, text: str) -> None:
        await self._web.chat_postMessage(
            channel=ref.channel_id, text=text, thread_ts=ref.message_id
        )

    async def react(self, ref: MessageRef, reaction: Reaction) -> None:
        try:
            await self._web.reactions_add(
                channel=ref.channel_id, timestamp=ref.message_id, name=_EMOJI[reaction]
            )
        except SlackApiError as exc:
            if exc.response.get("error") != "already_reacted":
                raise

    async def unreact(self, ref: MessageRef, reaction: Reaction) -> None:
        try:
            await self._web.reactions_remove(
                channel=ref.channel_id, timestamp=ref.message_id, name=_EMOJI[reaction]
            )
        except SlackApiError as exc:
            if exc.response.get("error") not in ("no_reaction", "message_not_found"):
                raise

    # -- secure channel --------------------------------------------------

    async def post_secure(self, text: str) -> tuple[SecureResult, str]:
        channel_id = self._c.slack_secure_channel_id
        if not channel_id:
            return SecureResult.NOT_CONFIGURED, ""
        try:
            await self._web.chat_postMessage(channel=channel_id, text=text)
        except SlackApiError as exc:
            return SecureResult.UNAVAILABLE, access_hint(
                channel_id, "secure", exc.response.get("error", "unknown")
            )
        return SecureResult.DELIVERED, ""

    # -- misc ------------------------------------------------------------

    async def fetch_attachment(self, file_id: str) -> tuple[bytes, str]:
        """Slack file URLs are private, so this fetch carries the bot token."""
        info = await self._web.files_info(file=file_id)
        url = info["file"]["url_private"]
        async with httpx.AsyncClient() as http:
            r = await http.get(
                url,
                headers={"Authorization": f"Bearer {self._c.slack_bot_token}"},
                follow_redirects=True,
                timeout=20,
            )
            r.raise_for_status()
        ctype = info["file"].get("mimetype") or r.headers.get(
            "content-type", "application/octet-stream"
        ).split(";")[0]
        return r.content, ctype

    async def notify_inbox(self, text: str) -> None:
        """Best-effort operator notice. Never raises: callers use it on error paths."""
        channel_id = self._c.slack_inbox_channel_id
        try:
            await self._web.chat_postMessage(channel=channel_id, text=text)
        except SlackApiError as exc:
            log.error(
                "%s (wanted to report: %s)",
                access_hint(channel_id, "inbox", exc.response.get("error", "unknown")),
                text,
            )

    async def check_access(self) -> None:
        """Report unusable channels at startup instead of when a message needs them.

        The secure channel is the one that matters: nothing routine writes to it,
        so a permissions mistake there stays invisible until a passcode arrives.
        """
        targets = [(self._c.slack_inbox_channel_id, "inbox")]
        if self._c.slack_secure_channel_id:
            targets.append((self._c.slack_secure_channel_id, "secure"))
        for channel_id, label in targets:
            try:
                info = await self._web.conversations_info(channel=channel_id)
            except SlackApiError as exc:
                log.error(
                    "startup check: %s",
                    access_hint(channel_id, label, exc.response.get("error", "unknown")),
                )
                continue
            if not info["channel"].get("is_member"):
                log.error("startup check: %s", access_hint(channel_id, label, "not_in_channel"))
            else:
                log.info(
                    "startup check: %s channel #%s is writable",
                    label,
                    info["channel"].get("name", channel_id),
                )

    def strip_markup(self, text: str) -> str:
        return strip_markup(text)


def _first_share_ts(shares: dict) -> str | None:
    for scope in ("public", "private"):
        for entries in (shares.get(scope) or {}).values():
            if entries:
                return entries[0].get("ts")
    return None
```

- [ ] **Step 3: Verify it imports and the suite still passes**

```bash
pip install --require-hashes -r requirements.txt -r requirements-dev.txt
python -c "from sms_bridge.chat.slack import SlackAdapter; print('ok')"
pytest -v
```
Expected: `ok`, all tests PASS.

- [ ] **Step 4: Commit**

```bash
git add requirements.in requirements.txt sms_bridge/chat/slack.py
git commit -m "feat: add Slack adapter over Socket Mode"
```

---

### Task 18: Slack adapter unit tests

**Files:**
- Modify: `sms_bridge/chat/slack.py`
- Create: `tests/test_slack_adapter.py`

**Interfaces:**
- Consumes: `SlackAdapter`, `ChannelIndex`, `SecureResult`, `Reaction`
- Produces: `SlackAdapter(config, web=None, socket=None)` — the two clients become injectable so the adapter is testable without a network. Production callers keep using `SlackAdapter(config)`.

The adapter is thin SDK glue by design — `slack_markup` and `slack_index` were split out precisely so the logic-heavy parts test independently. What remains is still branch-heavy in two places that matter: the event filter decides what becomes an SMS, and `post_secure` feeds the passcode decision table. Neither should rest on a manual checklist alone.

- [ ] **Step 1: Make the clients injectable**

In `sms_bridge/chat/slack.py`, change the constructor:
```python
    def __init__(self, config: Config, web=None, socket=None) -> None:
        self._c = config
        self._web = web or AsyncWebClient(token=config.slack_bot_token)
        self._socket = socket or SocketModeClient(
            app_token=config.slack_app_token, web_client=self._web
        )
        self._index = ChannelIndex(self._list_conversations)
        self._on_outbound = None
        self._bot_user_id = ""
        self._ready = False
```

- [ ] **Step 2: Write the failing tests**

`tests/test_slack_adapter.py`:
```python
"""Slack adapter behaviour that does not need a network.

Two areas earn coverage here: the event filter, which decides what becomes an
outbound SMS, and post_secure, which feeds the passcode decision table.
"""

import pytest
from slack_sdk.errors import SlackApiError

from sms_bridge.chat.base import Reaction, SecureResult
from sms_bridge.chat.slack import SlackAdapter, access_hint
from sms_bridge.config import load

ENV = {
    "CHAT_PLATFORM": "slack",
    "SLACK_BOT_TOKEN": "xoxb-test",
    "SLACK_APP_TOKEN": "xapp-test",
    "SLACK_INBOX_CHANNEL_ID": "C-inbox",
    "SIGNALWIRE_SPACE_URL": "https://example.signalwire.com",
    "SIGNALWIRE_PROJECT_ID": "proj",
    "SIGNALWIRE_API_TOKEN": "tok",
    "SIGNALWIRE_SIGNING_KEY": "sign",
    "SIGNALWIRE_NUMBER": "+14165550100",
    "PUBLIC_BASE_URL": "https://sms.example.com",
}

BOT_USER = "U-bot"


class FakeResponse(dict):
    """Slack responses support both mapping access and .data."""

    @property
    def data(self):
        return dict(self)


class FakeWeb:
    """Records calls and returns canned responses."""

    def __init__(self, **errors: str):
        self.errors = errors           # method name -> Slack error code to raise
        self.calls: list[tuple[str, dict]] = []
        self.topics: dict[str, str] = {}
        self.posted: list[tuple[str, str]] = []

    def _maybe_raise(self, method: str) -> None:
        if method in self.errors:
            raise SlackApiError(
                f"{method} failed", FakeResponse({"ok": False, "error": self.errors[method]})
            )

    async def auth_test(self):
        return FakeResponse({"user_id": BOT_USER, "user": "smsbot"})

    async def chat_postMessage(self, channel, text, thread_ts=None):
        self.calls.append(("chat_postMessage", {"channel": channel, "text": text}))
        self._maybe_raise("chat_postMessage")
        self.posted.append((channel, text))
        return FakeResponse({"ts": "1699999999.000100", "channel": channel})

    async def conversations_create(self, name, is_private):
        self.calls.append(("conversations_create", {"name": name, "is_private": is_private}))
        self._maybe_raise("conversations_create")
        return FakeResponse({"channel": {"id": "C-new"}})

    async def conversations_setTopic(self, channel, topic):
        self.calls.append(("conversations_setTopic", {"channel": channel, "topic": topic}))
        self.topics[channel] = topic
        return FakeResponse({"ok": True})

    async def conversations_invite(self, channel, users):
        self.calls.append(("conversations_invite", {"channel": channel, "users": users}))
        self._maybe_raise("conversations_invite")
        return FakeResponse({"ok": True})

    async def conversations_info(self, channel):
        self.calls.append(("conversations_info", {"channel": channel}))
        self._maybe_raise("conversations_info")
        return FakeResponse(
            {"channel": {"id": channel, "name": "sms-x", "is_member": True,
                         "topic": {"value": self.topics.get(channel, "")}}}
        )

    async def conversations_list(self, **kwargs):
        self.calls.append(("conversations_list", kwargs))
        return FakeResponse({"channels": [], "response_metadata": {"next_cursor": ""}})

    async def reactions_add(self, channel, timestamp, name):
        self.calls.append(("reactions_add", {"name": name, "timestamp": timestamp}))
        self._maybe_raise("reactions_add")
        return FakeResponse({"ok": True})

    async def reactions_remove(self, channel, timestamp, name):
        self.calls.append(("reactions_remove", {"name": name, "timestamp": timestamp}))
        self._maybe_raise("reactions_remove")
        return FakeResponse({"ok": True})


def build(env_extra=None, **errors):
    cfg = load({**ENV, **(env_extra or {})})
    web = FakeWeb(**errors)
    adapter = SlackAdapter(cfg, web=web, socket=object())
    adapter._bot_user_id = BOT_USER
    return adapter, web


def collector():
    seen = []

    async def on_outbound(msg):
        seen.append(msg)

    return seen, on_outbound


# --------------------------------------------------------------------------
# Event filtering - what becomes an outbound SMS
# --------------------------------------------------------------------------

async def test_top_level_user_message_is_dispatched():
    adapter, web = build()
    seen, handler = collector()
    adapter._on_outbound = handler
    web.topics["C1"] = "sms:+14165550123"

    await adapter._dispatch(
        {"type": "message", "channel": "C1", "user": "U-human",
         "ts": "1.1", "text": "hello"}
    )

    assert len(seen) == 1
    assert seen[0].text == "hello"
    assert seen[0].channel_topic == "sms:+14165550123"
    assert seen[0].message.message_id == "1.1"


async def test_bot_messages_are_ignored():
    adapter, _ = build()
    seen, handler = collector()
    adapter._on_outbound = handler

    await adapter._dispatch(
        {"type": "message", "channel": "C1", "bot_id": "B1", "ts": "1.1", "text": "hi"}
    )

    assert seen == []


async def test_the_apps_own_messages_are_ignored():
    adapter, _ = build()
    seen, handler = collector()
    adapter._on_outbound = handler

    await adapter._dispatch(
        {"type": "message", "channel": "C1", "user": BOT_USER, "ts": "1.1", "text": "hi"}
    )

    assert seen == []


@pytest.mark.parametrize(
    "subtype", ["message_changed", "message_deleted", "channel_join", "bot_message"]
)
async def test_ignored_subtypes(subtype):
    adapter, _ = build()
    seen, handler = collector()
    adapter._on_outbound = handler

    await adapter._dispatch(
        {"type": "message", "subtype": subtype, "channel": "C1",
         "user": "U-human", "ts": "1.1", "text": "hi"}
    )

    assert seen == []


async def test_thread_replies_are_never_sent():
    """Threads are the in-workspace discussion space, not outbound traffic."""
    adapter, _ = build()
    seen, handler = collector()
    adapter._on_outbound = handler

    await adapter._dispatch(
        {"type": "message", "channel": "C1", "user": "U-human",
         "ts": "2.2", "thread_ts": "1.1", "text": "just a note"}
    )

    assert seen == []


async def test_a_thread_parent_is_still_sent():
    """A message that started a thread has thread_ts == ts and must still send."""
    adapter, web = build()
    seen, handler = collector()
    adapter._on_outbound = handler
    web.topics["C1"] = "sms:+14165550123"

    await adapter._dispatch(
        {"type": "message", "channel": "C1", "user": "U-human",
         "ts": "1.1", "thread_ts": "1.1", "text": "hi"}
    )

    assert len(seen) == 1


async def test_files_become_attachments():
    adapter, web = build()
    seen, handler = collector()
    adapter._on_outbound = handler
    web.topics["C1"] = "sms:+14165550123"

    await adapter._dispatch(
        {"type": "message", "channel": "C1", "user": "U-human", "ts": "1.1",
         "text": "look", "files": [{"id": "F1", "name": "a.png", "size": 4096}]}
    )

    attachment = seen[0].attachments[0]
    assert (attachment.file_id, attachment.filename, attachment.size) == ("F1", "a.png", 4096)


async def test_channel_events_update_the_index_not_the_handler():
    adapter, _ = build()
    seen, handler = collector()
    adapter._on_outbound = handler

    await adapter._dispatch(
        {"type": "channel_created",
         "channel": {"id": "C9", "topic": {"value": "sms:+14165550199"}}}
    )

    assert seen == []
    assert await adapter._index.lookup("+14165550199") == "C9"


# --------------------------------------------------------------------------
# post_secure - feeds the passcode decision table
# --------------------------------------------------------------------------

async def test_post_secure_not_configured():
    adapter, web = build()

    result, hint = await adapter.post_secure("code 1234")

    assert result is SecureResult.NOT_CONFIGURED
    assert hint == ""
    assert web.posted == []


async def test_post_secure_delivered():
    adapter, web = build(env_extra={"SLACK_SECURE_CHANNEL_ID": "C-secure"})

    result, hint = await adapter.post_secure("code 1234")

    assert result is SecureResult.DELIVERED
    assert hint == ""
    assert web.posted == [("C-secure", "code 1234")]


async def test_post_secure_unavailable_carries_an_actionable_hint():
    adapter, _ = build(
        env_extra={"SLACK_SECURE_CHANNEL_ID": "C-secure"},
        chat_postMessage="not_in_channel",
    )

    result, hint = await adapter.post_secure("code 1234")

    assert result is SecureResult.UNAVAILABLE
    assert "/invite" in hint
    assert "C-secure" in hint


async def test_post_secure_never_raises_on_a_slack_error():
    """Delivery relies on a returned result, not an exception."""
    adapter, _ = build(
        env_extra={"SLACK_SECURE_CHANNEL_ID": "C-secure"},
        chat_postMessage="missing_scope",
    )

    result, _ = await adapter.post_secure("code")

    assert result is SecureResult.UNAVAILABLE


@pytest.mark.parametrize(
    "error,expected",
    [
        ("channel_not_found", "the channel id is wrong"),
        ("not_in_channel", "/invite"),
        ("is_archived", "archived"),
        ("missing_scope", "OAuth scope"),
        ("something_new", "something_new"),
    ],
)
def test_access_hint_maps_slack_errors(error, expected):
    assert expected in access_hint("C1", "secure", error)


# --------------------------------------------------------------------------
# Channels and reactions
# --------------------------------------------------------------------------

async def test_create_channel_is_private_sets_topic_and_indexes():
    adapter, web = build()

    ref = await adapter.create_channel("+14165550123")

    created = dict(web.calls)["conversations_create"]
    assert created["is_private"] is True
    assert created["name"] == "sms-14165550123"
    assert web.topics["C-new"] == "sms:+14165550123"
    assert await adapter._index.lookup("+14165550123") == "C-new"
    assert ref.id == "C-new"


async def test_create_channel_invites_configured_operators():
    adapter, web = build(env_extra={"SLACK_INVITE_USERS": "U1,U2"})

    await adapter.create_channel("+14165550123")

    assert dict(web.calls)["conversations_invite"]["users"] == "U1,U2"


async def test_create_channel_survives_a_failed_invite():
    """A bad member id must not cost us the channel."""
    adapter, web = build(
        env_extra={"SLACK_INVITE_USERS": "U-bogus"}, conversations_invite="user_not_found"
    )

    ref = await adapter.create_channel("+14165550123")

    assert ref.id == "C-new"


async def test_react_ignores_already_reacted():
    adapter, _ = build(reactions_add="already_reacted")
    from sms_bridge.chat.base import MessageRef

    await adapter.react(MessageRef("C1", "1.1"), Reaction.OK)  # must not raise


async def test_react_reraises_unexpected_errors():
    adapter, _ = build(reactions_add="missing_scope")
    from sms_bridge.chat.base import MessageRef

    with pytest.raises(SlackApiError):
        await adapter.react(MessageRef("C1", "1.1"), Reaction.OK)


async def test_unreact_ignores_no_reaction():
    adapter, _ = build(reactions_remove="no_reaction")
    from sms_bridge.chat.base import MessageRef

    await adapter.unreact(MessageRef("C1", "1.1"), Reaction.PENDING)  # must not raise


async def test_notify_inbox_never_raises():
    """Callers use it on error paths, so it must swallow everything."""
    adapter, _ = build(chat_postMessage="channel_not_found")

    await adapter.notify_inbox("something broke")  # must not raise
```

- [ ] **Step 3: Run to verify failure**

```bash
pytest tests/test_slack_adapter.py -v
```
Expected: FAIL — `SlackAdapter.__init__() got an unexpected keyword argument 'web'` until Step 1 is applied; after that, any genuine behaviour gaps surface individually.

- [ ] **Step 4: Run to verify pass**

```bash
pytest tests/test_slack_adapter.py -v
```
Expected: all PASS.

- [ ] **Step 5: Run the whole suite**

```bash
pytest -v
```
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add sms_bridge/chat/slack.py tests/test_slack_adapter.py
git commit -m "test: cover Slack event filtering and secure-channel outcomes"
```

---

### Task 19: Startup access checks and end-to-end Slack verification

**Files:**
- Modify: `sms_bridge/__main__.py`

**Interfaces:**
- Consumes: `ChatAdapter.check_access`
- Produces: `check_access()` runs once after the adapter connects, on both platforms.

The Discord adapter's `check_access` was carried over in Task 10 but nothing calls it yet — the original code ran it inside `on_ready` behind the `_tasks_started` guard. Now that adapter startup is explicit and happens once, the guard is unnecessary.

- [ ] **Step 1: Call it after the adapter starts**

In `sms_bridge/__main__.py`, replace the bare `asyncio.create_task(adapter.start(...))` line with:
```python
    async def start_chat() -> None:
        await adapter.start(delivery.handle_outbound)

    async def check_when_ready() -> None:
        # adapter.start blocks for the lifetime of the connection on Discord, so
        # the access check waits for readiness rather than for start() to return.
        for _ in range(60):
            if adapter.is_ready():
                await adapter.check_access()
                return
            await asyncio.sleep(1)
        log.warning("adapter never became ready; skipping the startup access check")

    asyncio.create_task(start_chat())
    asyncio.create_task(check_when_ready())
```

- [ ] **Step 2: Run the suite**

```bash
pytest -v
```
Expected: all PASS.

- [ ] **Step 3: Commit**

```bash
git add sms_bridge/__main__.py
git commit -m "feat: run the startup channel access check on both platforms"
```

- [ ] **Step 4: Create the Slack app and verify end to end**

Create a Slack app with these bot scopes — `chat:write`, `groups:write`, `groups:read`, `files:read`, `reactions:write` — plus the app-level scope `connections:write`, and enable Socket Mode. Subscribe to bot events: `message.groups`, `channel_created`, `channel_rename`, `channel_archive`.

Run against a second SignalWire number with `CHAT_PLATFORM=slack`, then confirm:

1. `GET /healthz` returns `{"ok": true, "platform": "slack", ...}`
2. Startup logs report the inbox channel as writable
3. An inbound SMS creates a **private** channel named `sms-<digits>` with topic `sms:+E164`, invites the configured operators, and posts the message
4. A second SMS from the same number reuses that channel without a further `conversations.list`
5. A top-level reply sends as SMS and shows ⏳ then ✅
6. A **thread** reply sends nothing
7. A message starting with `//` sends nothing
8. `!sms <number> hello` opens a channel and sends
9. An image sent in a contact channel arrives as a picture MMS
10. An inbound passcode is suppressed, or routed to the secure channel when one is set
11. With `SLACK_SECURE_CHANNEL_ID` pointing at a channel the bot is *not* in, a passcode is suppressed and the inbox reports `not_in_channel`

Item 11 is the one worth being deliberate about — it is the safety-critical branch, and the only way to confirm it is to break the secure channel on purpose.

---

# Phase 5 — Documentation, manifests, and CI

---

### Task 20: Deployment manifests

**Files:**
- Modify: `.env.example`, `docker-compose.yml`, `stack.portainer.yml`, `sms-bridge.service`

**Interfaces:**
- Consumes: the config surface from Task 5
- Produces: all three deployment paths carrying `CHAT_PLATFORM` and the Slack variables.

Remember the syntax constraint: `.env.example` feeds both Compose (`env_file`) and systemd (`EnvironmentFile`). Plain `KEY=VALUE`, no quotes, no `export`, no trailing comments — systemd strips quotes and Compose does not, so a quoted value silently means two different things.

- [ ] **Step 1: Add the platform block to `.env.example`**

Insert near the top, above the Discord section:
```
# Which chat platform to bridge to: discord or slack.
# One platform per process. Only that platform's variables below are required.
CHAT_PLATFORM=discord
```

- [ ] **Step 2: Add the Slack section to `.env.example`**

After the Discord block:
```
# --- Slack (required when CHAT_PLATFORM=slack) ---
# Bot User OAuth Token. Scopes: chat:write, groups:write, groups:read,
# files:read, reactions:write
#SLACK_BOT_TOKEN=xoxb-your-bot-token
# App-Level Token with connections:write, for Socket Mode
#SLACK_APP_TOKEN=xapp-your-app-token
# Channel that receives new-contact notices and delivery failures
#SLACK_INBOX_CHANNEL_ID=C0123456789
# Optional: passcodes go here instead of the contact channel
#SLACK_SECURE_CHANNEL_ID=C0123456780
# Optional: member IDs invited to each new contact channel, comma-separated
#SLACK_INVITE_USERS=U0123456789,U0123456788
```

- [ ] **Step 3: Add the shared optional variables to `.env.example`**

```
# Optional: signs outbound media URLs. Defaults to a random per-process key,
# which is fine - SignalWire fetches media during the send call, so a restart
# only expires URLs that were already in flight.
#MEDIA_SIGNING_KEY=
# Optional: largest attachment forwarded as MMS, in bytes. Carriers commonly
# reject anything much larger.
#MAX_MMS_BYTES=1048576
```

- [ ] **Step 4: Confirm both compose files need no structural change**

`docker-compose.yml` and `stack.portainer.yml` both pass the environment through wholesale, so new variables flow without edits. Verify by reading each file and checking that neither enumerates variables individually. If either does, add the new names in the same style.

Confirm neither file has gained a `ports:` mapping. The bridge binds `0.0.0.0` inside the container and is reached privately by `cloudflared`; publishing a port would expose an endpoint guarded only by the signature check.

- [ ] **Step 5: Verify a Slack config starts**

```bash
CHAT_PLATFORM=slack SLACK_BOT_TOKEN=x SLACK_APP_TOKEN=y SLACK_INBOX_CHANNEL_ID=C1 \
SIGNALWIRE_SPACE_URL=https://x.signalwire.com SIGNALWIRE_PROJECT_ID=p \
SIGNALWIRE_API_TOKEN=t SIGNALWIRE_SIGNING_KEY=k SIGNALWIRE_NUMBER=+14165550100 \
PUBLIC_BASE_URL=https://sms.example.com DB_PATH=/tmp/t.sqlite3 \
timeout 5 python -m sms_bridge 2>&1 | head -5
```
Expected: `platform=slack` and a webhook-listening line, then a Slack auth failure (the token is fake) — not a config error.

- [ ] **Step 6: Commit**

```bash
git add .env.example docker-compose.yml stack.portainer.yml sms-bridge.service
git commit -m "docs: add CHAT_PLATFORM and Slack variables to deployment manifests"
```

---

### Task 21: README and CLAUDE.md

**Files:**
- Modify: `README.md`, `CLAUDE.md`

**Interfaces:**
- Consumes: everything
- Produces: documentation matching the shipped code.

- [ ] **Step 1: Retitle and restructure the README**

Change the title to `# SignalWire ↔ Slack/Discord SMS bridge`. Under "What it does", state that one process bridges one platform, chosen with `CHAT_PLATFORM`.

Split "Setup" into `### 1a. Discord` (existing content) and `### 1b. Slack`, the latter covering: creating the app, the six scopes, enabling Socket Mode, the four event subscriptions, installing to the workspace, and finding channel IDs.

Update every `python sms_discord_bridge.py` to `python -m sms_bridge`.

In "Known limits", replace any statement that attachments are sent as links with: attachments are forwarded as real MMS via a signed, ten-minute media URL, capped at `MAX_MMS_BYTES`; and note that Slack thread replies are never sent as SMS.

Add a "Tests" section:
````markdown
## Tests

```bash
pip install --require-hashes -r requirements.txt -r requirements-dev.txt
pytest
```

No network access is required. The suite covers the pure helpers and drives the
delivery core through a fake adapter, including every branch of the passcode
decision table.
````

- [ ] **Step 2: Rewrite CLAUDE.md**

Replace the "What this is" section with:
```markdown
## What this is

A SignalWire ↔ chat SMS/MMS bridge that targets **either Discord or Slack**,
selected by `CHAT_PLATFORM`. One process bridges one platform.

Application code lives in `sms_bridge/`. The core owns all SignalWire logic and,
critically, all policy — adapters report capability and execute instructions but
never decide what happens when something is unavailable. That is what keeps the
passcode-suppression rules in one testable place.

- `config.py` — env parsing; required variables vary by platform
- `routing.py` — E.164 handling and the topic-as-routing-table helpers
- `text.py` — chunking, GSM-7 segmentation, passcode detection
- `store.py` — the SQLite cache
- `signalwire.py` — LaML client and webhook signature validation
- `media.py` — signed, expiring tokens for outbound MMS URLs
- `delivery.py` — the platform-agnostic brain; imports no platform SDK
- `server.py` — FastAPI webhook and media endpoints
- `chat/base.py` — the `ChatAdapter` protocol
- `chat/discord.py`, `chat/slack.py` — the adapters
```

Update "Commands" to `python -m sms_bridge` and add the pytest invocation. Note that `requirements-dev.txt` is a separate lock regenerated the same way.

Keep the entire "Constraints to preserve when editing" section, updating it as follows:
- Replace the description of outbound attachments as links with the signed media endpoint
- Add: **the media endpoint's HMAC and expiry are its only guard** — never accept an unsigned token, never let it take a URL rather than a file id
- Add: **the Slack channel index is derived state** — in-memory, never persisted, rebuildable from topics; do not turn it into a contacts table
- Add: **Slack thread replies are never sent as SMS**
- Replace the `on_ready` reconnect-guard note with: adapter startup is explicit and happens once, so no guard is needed
- Keep verbatim: passcode redaction, the unpublished-port rule, MMS caption ordering, the `python-multipart` floor, `SIGNALWIRE_SIGNING_KEY` never defaulting

Update "Configuration" to list common, Discord-only, and Slack-only variables separately, and record that `CHAT_PLATFORM` defaults to `discord` along with the reason that default is safe when the signing key's is not.

Add a "Tests" section stating that the suite exists, runs without network, and that the passcode decision table is covered by `tests/test_delivery.py` — and that changes to suppression logic must keep those tests green.

- [ ] **Step 3: Check for stale references**

```bash
grep -rn "sms_discord_bridge" --include="*.md" --include="*.yml" --include="*.service" --include="Dockerfile" .
```
Expected: no matches outside `docs/superpowers/`.

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: document the unified Discord/Slack bridge"
```

---

### Task 22: Run tests in CI

**Files:**
- Create: `.github/workflows/test.yml`

**Interfaces:**
- Consumes: the test suite
- Produces: pytest on pull requests and on demand.

This deliberately breaks the repo's manual-first workflow convention: a test suite nobody runs is not a safety net. It uses the runner's preinstalled Python and the `actions/checkout` SHA already pinned elsewhere in the repo, so it adds no new third-party action to track.

- [ ] **Step 1: Create the workflow**

`.github/workflows/test.yml`:
```yaml
name: tests

# Unlike release.yml and publish-image.yml, this one is not manual-first: tests
# that only run locally stop being a safety net the moment someone forgets.
on:
  pull_request:
  workflow_dispatch:

permissions:
  contents: read

jobs:
  pytest:
    runs-on: ubuntu-latest
    steps:
      # Pinned to a full commit SHA, like every third-party action here. A
      # mutable tag can be repointed at new code by whoever owns the action.
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1

      - name: Install dependencies
        run: |
          python3 -m pip install --upgrade pip
          python3 -m pip install --require-hashes -r requirements.txt -r requirements-dev.txt

      - name: Run tests
        run: python3 -m pytest -v
```

- [ ] **Step 2: Verify the YAML parses**

```bash
python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/test.yml')); print('ok')"
```
Expected: `ok`. If PyYAML is unavailable, push the branch and confirm the workflow appears in the Actions tab instead.

- [ ] **Step 3: Confirm the runner's Python is 3.12 or newer**

The workflow relies on the preinstalled interpreter. If the run fails on a syntax error from `X | None` annotations, add `actions/setup-python` — resolve its SHA first rather than trusting the tag:
```bash
curl -s https://api.github.com/repos/actions/setup-python/commits/v6.0.0 | jq -r .sha
```
and pin it with the version in a trailing comment, matching the existing style.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/test.yml
git commit -m "chore: run pytest on pull requests"
```

---

### Task 23: Release

**Files:** none — this is a GitHub Actions operation.

- [ ] **Step 1: Confirm the tree is clean and green**

```bash
git status --short
pytest -v
```
Expected: no output from `git status`, all tests PASS.

- [ ] **Step 2: Cut a minor release**

Run the `release` workflow from the Actions tab with the `minor` input. It derives the next `vX.Y.Z` from the newest tag, builds notes from conventional-commit prefixes, tags, creates the release, and calls `publish-image.yml`.

- [ ] **Step 3: Add the upgrade notes to the release body**

Edit the generated release and add, near the top:

```markdown
### Upgrading

- **Entry point changed.** `python sms_discord_bridge.py` is now `python -m sms_bridge`.
  The Docker image handles this itself; host/systemd installs need the updated unit file.
- **Configuration is backward compatible.** `CHAT_PLATFORM` defaults to `discord`, so
  existing `.env` files keep working unchanged.
- **Attachments now send as real MMS** rather than a link in the message body.
```

- [ ] **Step 4: Verify the published image**

```bash
docker pull ghcr.io/<owner>/<repo>:latest
docker run --rm ghcr.io/<owner>/<repo>:latest 2>&1 | head -3
```
Expected: the missing-env-var exit, confirming the entry point works.

---

## Deferred: repository rename

Do this only after both platforms are confirmed working in production. It is
isolated — nothing in the code depends on the repository name.

1. Rename the repo in GitHub settings.
2. Update the local remote: `git remote set-url origin <new-url>`.
3. **Repoint every deployment's `IMAGE_REF`.** GitHub redirects a renamed
   repository but does *not* move its GHCR package. Because `publish-image.yml`
   derives the image name from `${{ github.repository }}`, the path changes to
   `ghcr.io/<owner>/<new-name>`. The old package keeps its existing tags but
   receives no new ones.
4. Cut a release afterwards so a build exists at the new path before anything
   depends on it.
5. Update the clone URL in the README.
