# Unified SignalWire ↔ chat bridge (Discord + Slack)

**Date:** 2026-08-03
**Status:** Approved, not yet implemented

## Summary

Turn the single-file Discord bridge into one application that targets either Discord or
Slack, selected by a `CHAT_PLATFORM` environment variable. The SignalWire half is written
once; each chat platform contributes an adapter.

This supersedes the original idea of a separate Slack repo. A second repo would duplicate
the SignalWire logic — signature validation, dedup, chunking, segmentation, media fetch,
status callbacks — and, more importantly, would duplicate the passcode-redaction rules.
Those rules are the one place a bug leaks a live one-time passcode, and they should exist
in exactly one testable location.

## Context and decisions

The two bridges run as **independent deployments against different SignalWire numbers**.
They never interact. One process serves one platform.

| Decision | Choice | Reasoning |
|---|---|---|
| Repo | Evolve this one in place | Keeps history, issues, tag lineage |
| Structure | Core + pluggable chat adapters | SignalWire logic and redaction policy written once |
| Slack transport | Socket Mode | Mirrors discord.py's gateway; no new public endpoint or second signature scheme |
| Outbound media | Signed media endpoint, real MMS | Slack file URLs need auth, so the Discord link trick cannot work |
| Discord media | Also switch to real MMS | Discord CDN links have been signed-and-expiring since 2023; one media path to test |
| Slack channels | Private, auto-invite operators | Matches the Discord deployment's privacy posture |
| Tests | Pure functions + fake adapter | The refactor has no other safety net |
| Concurrency | One platform per process | Two platforms in one process means two inboxes, two secure channels, ambiguous `!sms` routing, no benefit |

## Architecture

The governing invariant: **the adapter never makes a policy decision.** It reports
capability and executes instructions. Core decides. This is what keeps passcode
suppression in one place and makes it testable with a fake.

### Module layout

```
sms_bridge/
  config.py        ~90   env parsing; required set varies by CHAT_PLATFORM
  routing.py       ~60   E164, normalise_number, topic_for, number_from_topic
  text.py          ~110  chunk, segment_count, looks_like_a_code
  store.py         ~60   sqlite: seen, outbound
  signalwire.py    ~180  valid_signature, explain_bad_signature, send_sms, fetch_media
  media.py         ~70   signed-token mint/verify for outbound MMS
  delivery.py      ~160  deliver_inbound, inbound_worker, handle_outbound, update_reaction
  server.py        ~110  FastAPI: /sms/inbound, /sms/status, /healthz, /media/{token}
  chat/base.py     ~80   ChatAdapter protocol, refs, enums
  chat/discord.py  ~210
  chat/slack.py    ~250
  __main__.py      ~50   wires adapter + uvicorn onto one event loop
tests/                   seven files, no network
```

No module exceeds ~250 lines. `delivery.py` holds the message-handling brain and imports
no platform SDK.

The package name `sms_bridge` is independent of the repository name, so a later repo
rename touches nothing inside the package.

### Adapter interface

```python
class Reaction(Enum):      PENDING; OK; FAIL
class SecureResult(Enum):  DELIVERED; NOT_CONFIGURED; UNAVAILABLE

class ChatAdapter(Protocol):
    max_post_chars: int

    async def start(self, on_outbound: Callable) -> None
    async def close(self) -> None
    def is_ready(self) -> bool
    def latency_ms(self) -> float

    async def find_channel(number: str) -> ChannelRef | None
    async def create_channel(number: str) -> ChannelRef
    async def post(ch: ChannelRef, text: str, files: list) -> MessageRef
    async def reply(ref: MessageRef, text: str) -> None
    async def react(ref: MessageRef, r: Reaction) -> None
    async def unreact(ref: MessageRef, r: Reaction) -> None

    async def post_secure(text: str) -> tuple[SecureResult, str]
    async def fetch_attachment(file_id: str) -> tuple[bytes, str]
    async def notify_inbox(text: str) -> None
    async def check_access() -> None
    def strip_markup(text: str) -> str
```

Two details carry weight:

**`post_secure` returns a result, not a decision.** Today the Discord code interleaves
"can I reach the secure channel?" with "what do I do if I can't?". The adapter will answer
only the first question, returning a `SecureResult` and a human-readable access hint.
`delivery.py` owns the consequence. The fake adapter can then drive all four outcomes.

**`max_post_chars` is an adapter attribute.** Core cannot hardcode 1900 any more: Discord's
cap is 2000 (we use 1900), Slack's practical limit is around 4000 (we use 3800).

Reactions are an enum, not emoji strings — Discord uses Unicode literals, Slack uses names
like `:hourglass_flowing_sand:`.

## Configuration

Unchanged common variables: `SIGNALWIRE_SPACE_URL`, `SIGNALWIRE_PROJECT_ID`,
`SIGNALWIRE_API_TOKEN`, `SIGNALWIRE_SIGNING_KEY`, `SIGNALWIRE_NUMBER`, `PUBLIC_BASE_URL`,
`REDACT_CODES`, `VERIFY_SIGNATURE`, `BIND_HOST`, `BIND_PORT`, `DB_PATH`, `HEARTBEAT_URL`,
`COMMAND_PREFIX`, `NOTE_PREFIX`.

New: `CHAT_PLATFORM` (`discord` | `slack`, default `discord`) and `MEDIA_SIGNING_KEY`
(optional) and `MAX_MMS_BYTES` (optional, default 1048576).

Platform-specific:

| Discord | Slack |
|---|---|
| `DISCORD_TOKEN` | `SLACK_BOT_TOKEN` (xoxb-) |
| `DISCORD_GUILD_ID` | `SLACK_APP_TOKEN` (xapp-, Socket Mode) |
| `DISCORD_INBOX_CHANNEL_ID` | `SLACK_INBOX_CHANNEL_ID` |
| `DISCORD_SECURE_CHANNEL_ID` *(optional)* | `SLACK_SECURE_CHANNEL_ID` *(optional)* |
| `DISCORD_CATEGORY_ID` *(optional)* | *no analogue — Slack has no categories* |
| — | `SLACK_INVITE_USERS` *(optional, comma-separated member IDs)* |

Only the selected platform's variables are required. A missing one exits at startup naming
both the variable and the platform that required it.

`CHAT_PLATFORM` defaults to `discord` so existing `.env` files keep working untouched.
This does not contradict the rule that `SIGNALWIRE_SIGNING_KEY` must never default: a
wrong signing key produces a silent asymmetric failure (sending works, receiving 403s)
that is hard to diagnose, whereas a wrong platform exits immediately with a missing-variable
message. Startup logs the selected platform.

`MEDIA_SIGNING_KEY` defaults to a random per-process key. SignalWire fetches `MediaUrl`
during the send call, so an ephemeral key suffices; a restart merely expires in-flight
media URLs early, which fails closed.

**Slack OAuth scopes:** `chat:write`, `groups:write`, `groups:read`, `files:read`,
`reactions:write`, and the app-level `connections:write`.

## Data flow

### Inbound

`POST /sms/inbound` → Twilio-scheme HMAC check → dedup on `MessageSid` → enqueue →
`inbound_worker` → `deliver_inbound`:

1. Fetch media and fold `text/plain` parts into the body — **before** the passcode check.
   This ordering is load-bearing: carriers deliver MMS captions as a separate `text/plain`
   part rather than in `Body`, so a code sent as a caption would otherwise skip redaction.
2. Passcode check.
3. Secure delivery or suppression, per the decision table below.
4. Otherwise resolve the channel and post, chunked at `adapter.max_post_chars`.

### Slack channel resolution

Discord's `guild.text_channels` is a local cache, so scanning it per message is free. Slack
has no equivalent: `conversations.list` is paginated and tier-2 rate limited (roughly 20
requests per minute), so a per-message scan would exhaust the budget immediately.

The Slack adapter maintains an **in-memory topic → channel index**, built once at startup
from a paginated `conversations.list`, updated on `channel_created`, `channel_rename` and
`channel_archive` events and on its own channel creations, with a full refresh on any
lookup miss.

This preserves the topic-as-routing-table design. The index is derived, in-memory,
disposable, and rebuildable from channel topics at any moment. Nothing is persisted and no
contacts table is introduced. `conversations.list` returns only those private channels the
bot belongs to, which is exactly the set it created.

### Outbound

Slack Socket Mode `message` events, filtered to drop: messages carrying `bot_id` or
`subtype == "bot_message"`, the app's own messages, and the `message_changed` /
`message_deleted` subtypes.

**Thread replies are not sent over SMS.** Only top-level messages in a contact channel go
out. This gives Slack a per-message discussion space that stays inside the workspace and
removes the main accidental-send risk. `NOTE_PREFIX` still works for inline notes.

Both platforms: resolve the target from the `!sms <number>` command or the channel topic,
strip platform markup, chunk at `MAX_SMS_CHARS`, send, record SIDs, react ⏳ then ✅/❌ from
the status callback.

## Outbound media

`GET /media/{token}`. The token is `base64url(f"{file_id}:{exp}")` followed by an
HMAC-SHA256 over that payload, compared in constant time. On a valid, unexpired token the
endpoint calls `adapter.fetch_attachment(file_id)` and streams the bytes back with the
source content type and `Cache-Control: no-store`. Expiry is 10 minutes.

`send_sms` gains a `MediaUrl` parameter; SignalWire accepts several per message.

There is no SSRF surface: the endpoint resolves opaque platform file IDs through the
adapter's own credential and never fetches a caller-supplied URL.

Attachments over `MAX_MMS_BYTES` (default 1 MB) are rejected with a reply explaining why,
rather than failing silently — carriers commonly reject larger MMS regardless of what the
API accepts.

This adds a public route to a service that deliberately exposes as little as possible, so
it becomes an explicit constraint in CLAUDE.md: **the HMAC and the expiry are the only
guard. Never accept an unsigned token; never accept a URL parameter.**

## Error handling

Passcode decision table, enforced in `delivery.py`:

| Situation | Behaviour |
|---|---|
| No secure channel configured | Suppress; placeholder to contact channel |
| Configured, adapter cannot see it | Suppress; notify inbox with hint; placeholder |
| Configured, send rejected | Suppress; notify inbox with hint; placeholder |
| Configured and reachable | Code to secure channel only; nothing to contact channel |

Missing access must never downgrade into posting the code somewhere less private.

`explain_bad_signature` is unchanged and stays SignalWire-specific.

Slack's error taxonomy is more precise than Discord's. Discord collapses "deleted",
"never granted", and "override denies it" into a single ambiguous `50001 Missing Access`,
which is why `access_hint` has to enumerate possibilities. Slack distinguishes
`channel_not_found`, `not_in_channel`, `is_archived`, and `missing_scope` — and
`missing_scope` names the scope, so the hint quotes it directly.

Slack 429 responses are honoured via `Retry-After`. discord.py handles its own rate limits.

`check_access()` runs at startup per adapter. The Slack implementation calls `auth.test`,
then `conversations.info` on the inbox and secure channels, verifies bot membership, and
builds the topic index.

## Tests

Seven files, no network, running in seconds:

- `test_routing.py` — E164, `normalise_number`, `topic_for`, `number_from_topic`
- `test_text.py` — `chunk`, `segment_count` (GSM-7 vs UCS-2), `looks_like_a_code`
- `test_signature.py` — a known-good Twilio vector plus each `explain_bad_signature` variant
- `test_media_token.py` — mint, verify, expiry, tampering
- `test_markup_discord.py` — the Discord adapter's `strip_markup`
- `test_markup_slack.py` — the Slack adapter's `strip_markup` (mrkdwn: `*bold*`, `_italic_`,
  `~strike~`, `<@U123>`, `<#C123|name>`, `<https://url|text>`)
- `test_delivery.py` — `FakeChatAdapter` driving all four passcode branches, MMS caption
  folding, chunking at `max_post_chars`, and dedup

Development dependencies live in a separate `requirements-dev.in` / `requirements-dev.txt`,
compiled with the same `--generate-hashes` process, so the runtime image stays lean.

A `test.yml` workflow runs pytest on pull requests. This breaks the repo's manual-first
workflow convention deliberately: a test suite nobody runs is not a safety net.

## Build order

1. **Tests against the current single file** — pure functions only. Establishes a baseline
   that must stay green through the refactor.
2. **Extract core plus `chat/discord.py`.** Behaviour identical. Tests green. Verified
   manually against the live deployment.
3. **Media endpoint.** `media.py`, `/media/{token}`, `MediaUrl` on send; switch Discord to
   real MMS.
4. **`chat/slack.py`.** Socket Mode, topic index, private channel creation, auto-invite.
5. **Docs and manifests.** CLAUDE.md rewrite, README, `.env.example`, `docker-compose.yml`,
   `stack.portainer.yml`, `sms-bridge.service`. Release as a minor version.
6. **Repo rename** — deferred until both platforms are confirmed working in production.

Steps 1 and 2 complete before any Slack code exists. That ordering is the entire safety
argument for restructuring a working, deployed service.

## Migration notes

**Entry point changes:** `python sms_discord_bridge.py` becomes `python -m sms_bridge`.
This touches the Dockerfile `CMD`, `sms-bridge.service`'s `ExecStart`, and the README.

**Configuration is backward compatible.** `CHAT_PLATFORM` defaults to `discord`, so an
existing `.env` continues to work unchanged.

**The repo rename (step 6) changes the image path.** GitHub redirects a renamed repository
but does not move its GHCR package. Because `publish-image.yml` derives the image name from
`${{ github.repository }}`, `ghcr.io/<owner>/discord_sms_bot` becomes
`ghcr.io/<owner>/<new-name>`. The old package keeps its existing tags but receives no new
ones. `IMAGE_REF` in the Portainer stack must be repointed. This belongs in the release
notes prominently, not as a footnote.

Because the rename is deferred to step 6, steps 1–5 publish to the existing image path and
require no deployment changes beyond the entry point.

**CLAUDE.md needs a substantial rewrite.** Its organising idea — one file holding all
application code — is what this change removes. (Its stated line count is already stale:
it claims ~590 lines against an actual 748.) The constraints section survives nearly intact;
the passcode rules, the unpublished-port rule, the MMS caption ordering, the `on_ready`
reconnect guard, and the `python-multipart` floor all still apply, and the media endpoint
adds one more.

## Explicitly out of scope

- Running both platforms from a single process
- A contacts database — channel topics remain the routing table on both platforms
- Slack slash commands or interactivity (no Bolt dependency; raw `slack_sdk` mirrors the
  existing use of raw `discord.Client`)
- An Events API transport for Slack
- Migrating existing Discord contact channels to Slack
