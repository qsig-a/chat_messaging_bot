# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

## Commands

```bash
pip install --require-hashes -r requirements.txt
python -m sms_bridge                    # needs all required env vars set, see below

pip install --require-hashes -r requirements-dev.txt
pytest                                  # no network access required

docker build -t sms-bridge:latest .

# Regenerate a lock after editing its .in — never hand-edit the .txt
docker run --rm -v "$PWD":/w -w /w python:3.12-slim sh -c \
  "pip install -q pip-tools && pip-compile --generate-hashes --strip-extras -o requirements.txt requirements.in"
```

`requirements.in` holds the direct runtime deps; `requirements.txt` is a pip-compile
lock (exact versions + SHA-256 hashes). Hand-editing a lock breaks the hash check and
the install refuses to run. `requirements-dev.in` / `requirements-dev.txt` are the same
arrangement for the test-only packages and are regenerated the same way — the image
never installs them.

`GET /healthz` returns connection readiness, the platform name, inbound queue depth,
and gateway latency (Slack's Socket Mode exposes none, so it reports `0`).

## CI

Three workflows. Release and publish are manual-first; tests are not.

- `.github/workflows/test.yml` — runs `pytest` on every pull request and on
  `workflow_dispatch`. This one deliberately breaks the manual-first convention: a
  suite nobody runs stops being a safety net the moment someone forgets. It uses the
  runner's preinstalled Python, so it adds no new third-party action to track.
- `.github/workflows/release.yml` — `workflow_dispatch` with a `patch|minor|major` input.
  Derives the next version from the newest `vX.Y.Z` tag (first run starts at `v0.1.0`),
  builds release notes from commits since that tag grouped by conventional-commit prefix
  (`feat`/`fix`/`docs`/`chore`… → sections, anything else → "Other"), tags, creates the
  release, then calls the publish workflow. Aborts if no commits since the last tag.
- `.github/workflows/publish-image.yml` — builds and pushes
  `ghcr.io/<owner>/<repo>` — derived from `${{ github.repository }}`, so forks work
  unedited — tagged `vX.Y.Z`, `X.Y`, and `latest`. Platforms come from the job-level
  `PLATFORMS` env (currently `linux/amd64`); adding `linux/arm64` also requires
  restoring `docker/setup-qemu-action`. It also accepts `workflow_dispatch` to rebuild
  an existing tag without cutting a release.

`release.yml` invokes `publish-image.yml` through `workflow_call` rather than letting the
`release: published` event do it. **This is deliberate: a release created with
`GITHUB_TOKEN` does not trigger further workflow runs.** If you rewire this to depend on
the event, publishing will silently stop happening. `publish-image.yml` keeps its
`release: published` trigger only for releases made by hand in the GitHub UI.

The commit convention matters — release notes are generated from it. Keep using
`feat:`, `fix:`, `docs:`, `chore:` prefixes.

**Third-party actions are pinned to full commit SHAs**, with the version in a trailing
comment (`uses: actions/checkout@3d3c42e5… # v7.0.1`). A mutable tag like `@v7` can be
repointed at new code by whoever owns the action; a SHA can't. Don't "tidy" these back
into version tags. To upgrade one, resolve the new SHA rather than trusting the tag:

```bash
curl -s https://api.github.com/repos/actions/checkout/commits/v7.0.2 | jq -r .sha
```

Update the trailing comment in the same edit — it's the only record of which version a
SHA corresponds to. Nothing renews these automatically; they will go stale silently.

## Architecture

Two servers share one asyncio event loop, started from `run()` in `__main__.py`: the
chat adapter's connection (Discord's gateway, or Slack's Socket Mode WebSocket) and a
`uvicorn`/FastAPI webhook listener. They are decoupled by an `asyncio.Queue` so webhook
handlers return within milliseconds — SignalWire retries on slow responses.
`Delivery.run_worker()` drains the queue and does the slow chat work (channel creation,
media download/re-upload).

**`ChatAdapter` is a capability interface, not a policy interface.** `chat/base.py`
defines it; `delivery.py` is the only thing that decides anything. An adapter reports
what happened (`SecureResult.NOT_CONFIGURED` / `UNAVAILABLE` / `DELIVERED`) and the core
chooses the consequence. Keep it that way: the moment an adapter decides where a
passcode goes, that rule needs testing twice. `delivery.py` imports no platform SDK, and
the whole passcode decision table is exercised through `FakeAdapter` in
`tests/test_delivery.py`.

**Channel topics are the routing table.** There is no contact database. A channel belongs
to a phone number iff its topic contains an `sms:+E164` token (`number_from_topic` /
`topic_for`). Channels can be freely renamed and their topics annotated as long as that
token survives. Do not add a contacts table — the topic-as-state design is deliberate and
is why the only persistent state is disposable. Discord scans `guild.text_channels`
linearly on each miss; Slack cannot (its API has no topic search), so `chat/slack_index.py`
keeps an in-memory index — see the constraints below.

**SQLite (`bridge.sqlite3`) is a cache, not a source of truth.** Two tables: `seen`
(webhook `MessageSid` de-duplication, since SignalWire retries) and `outbound`
(SID → channel/message ID, so delivery-status callbacks can swap ⏳ for ✅/❌ on the
right message). Both id columns are TEXT: Discord ids are integers but Slack `ts` values
are not. Rows older than 30 days are pruned at startup. Deleting the file loses only
in-flight reaction updates.

**Message flow, both directions:**

- Inbound: `POST /sms/inbound` → signature check (`check`) → dedup (`already_seen`) →
  enqueue → `Delivery.handle_inbound` → fetch media → passcode check → find-or-create
  channel → `adapter.post`.
- Outbound: the adapter's event handler filters (bots, notes, other guilds, Slack thread
  replies) and hands the core an `OutboundMessage` → `Delivery.handle_outbound` resolves
  the target from either the `!sms <number>` command or the channel topic →
  `adapter.strip_markup` → `chunk` → `send_sms` per piece → record SIDs.
- Status: `POST /sms/status` → `Delivery.update_status` via the `outbound` table.
- Outbound media: `Delivery._media_urls_for` mints a signed token per attachment and
  passes `PUBLIC_BASE_URL/media/{token}` to SignalWire, which fetches it back through
  `GET /media/{token}` → `adapter.fetch_attachment`.

**Webhook signature validation** (`valid_signature`) is the Twilio scheme SignalWire's
LaML API uses: HMAC-SHA1 over `PUBLIC_BASE_URL + path` plus sorted form params, keyed
by `SIGNALWIRE_SIGNING_KEY`. It is the *only* authentication on the webhook endpoints.

**The signing key is a distinct credential from `SIGNALWIRE_API_TOKEN`** — a project can
hold several tokens, any of which authenticates REST calls, while only one signs
webhooks. It is required rather than defaulting to the API token on purpose: the wrong
key rejects every inbound message with a 403 while outbound keeps working, and that
asymmetry ("sending works, receiving doesn't") is much harder to diagnose than a
missing-variable exit at startup. `explain_bad_signature` exists to tell those cases
apart: on every rejection it re-runs the HMAC against each plausible URL variant and the
other credential, and logs which one would have matched. Don't reintroduce a fallback.
`PUBLIC_BASE_URL` must match what SignalWire calls character for character — scheme,
host, trailing slash — or every request 403s. If you change endpoint paths, the literal
path string passed to `check()` in `server.py` must stay in sync with the route
decorator above it — nothing enforces that they match.

## Constraints to preserve when editing

- **Do not publish the container's port.** `BIND_HOST` defaults to `0.0.0.0` inside
  Docker because container loopback would make the webhook unreachable; that is only
  safe because both compose setups deliberately expose nothing and route
  `cloudflared` → `http://bridge:8080` on a private network. Adding a `ports:` mapping
  puts an endpoint guarded solely by the signature check on the LAN.
- **Passcode redaction** (`looks_like_a_code`: a code-ish keyword *and* a 4–8 digit
  number) diverts to the platform's secure channel if set, otherwise writes nothing to
  the chat platform at all. The no-secure-channel branch must not leak the body. A
  secure channel that is configured but unusable — deleted, invisible to the bot,
  `Forbidden` on Discord, `not_in_channel` on Slack — takes that same suppression path
  rather than falling back to the contact channel, and reports why via `access_hint`
  to both the log and the inbox channel. Missing access must never downgrade into
  posting the code somewhere less private. This is the decision table
  `tests/test_delivery.py` pins; changes to suppression logic must keep it green.
- **MMS captions arrive as a `text/plain` media part**, not in `Body` — carriers split
  them out, so an image sent with a caption webhooks in as `Body=""` plus two
  `MediaUrl`s. `Delivery.handle_inbound` folds those parts back into the body instead of
  attaching them (otherwise the caption uploads as a stray `mms.bin`). This is why
  media is fetched *before* the passcode check: a code sent as a caption would
  otherwise skip redaction entirely. Keep that ordering.
- **The media endpoint's HMAC and expiry are its only guard.** `GET /media/{token}` is
  reachable by anyone who can reach the tunnel, and it hands back private chat
  attachments. Never accept an unsigned token, never let it take a URL rather than an
  opaque file id, and keep every failure a bare 404 so it cannot be used to probe which
  ids exist. Tokens live 10 minutes; SignalWire fetches during the send call.
- **The Slack channel index is derived state** (`chat/slack_index.py`) — in memory,
  never persisted, and rebuildable at any time from channel topics. It exists only
  because Slack has no topic search and `conversations.list` is rate-limited (tier 2,
  ~20/min), so a miss triggers at most one refresh. Do not turn it into a contacts
  table; the topics remain the truth.
- **Slack thread replies are never sent as SMS.** Only top-level messages in a contact
  channel go out. Threads are the in-workspace discussion space, and that split is what
  makes it safe to talk about a conversation inside it.
- Adapter startup is explicit and happens once, in `run()`. The old `on_ready`
  reconnect guard is gone — nothing spawns background tasks from a reconnect handler
  any more, so new tasks just go in `run()`'s task list.
- Chat markup is literal text over SMS, so outbound bodies go through
  `adapter.strip_markup` (`chat/discord.py`'s own rules, `chat/slack_markup.py` for
  Slack's `<@U…>` / `<url|label>` forms). Outbound SMS is chunked at `MAX_SMS_CHARS`
  (1500; SignalWire's hard cap is 1600); inbound posts are chunked at the adapter's
  `max_post_chars` — 1900 for Discord (cap 2000), 3800 for Slack (~4000).
- `segment_count` implements GSM-7 vs UCS-2 segmentation for logging only — non-GSM-7
  characters (emoji, curly quotes) drop the per-segment limit from 160 to 70.
- **Keep `python-multipart` at 0.0.30+.** `check()` calls `await request.form()` — that
  parser — *before* `valid_signature`, so it processes bodies from any unauthenticated
  internet caller that reaches the tunnel. Pre-0.0.30 releases carry DoS and
  parameter-smuggling advisories. If you ever reorder `check()`, validating the
  signature before parsing the form would shrink this surface considerably.

## Configuration

All config is environment variables, parsed once in `config.load()` into a frozen
`Config`; anything missing or malformed raises `ConfigError`, which `main()` turns into
a startup exit naming the variable. Required variables depend on the platform, and the
error says which platform wanted them.

- Common: `SIGNALWIRE_SPACE_URL`, `SIGNALWIRE_PROJECT_ID`, `SIGNALWIRE_API_TOKEN`,
  `SIGNALWIRE_SIGNING_KEY`, `SIGNALWIRE_NUMBER`, `PUBLIC_BASE_URL`
- Discord-only, required when `CHAT_PLATFORM=discord`: `DISCORD_TOKEN`,
  `DISCORD_GUILD_ID`, `DISCORD_INBOX_CHANNEL_ID`; optional `DISCORD_CATEGORY_ID`,
  `DISCORD_SECURE_CHANNEL_ID`
- Slack-only, required when `CHAT_PLATFORM=slack`: `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`,
  `SLACK_INBOX_CHANNEL_ID`; optional `SLACK_SECURE_CHANNEL_ID`, `SLACK_INVITE_USERS`
- Optional everywhere: `REDACT_CODES`, `VERIFY_SIGNATURE`, `BIND_HOST`, `BIND_PORT`,
  `DB_PATH`, `HEARTBEAT_URL`, `COMMAND_PREFIX`, `NOTE_PREFIX`, `MEDIA_SIGNING_KEY`,
  `MAX_MMS_BYTES`

`CHAT_PLATFORM` defaults to `discord` so pre-split `.env` files keep working. That does
**not** contradict the rule that `SIGNALWIRE_SIGNING_KEY` must never default: a wrong
platform exits at startup naming the variable it wanted, while a wrong signing key fails
silently and asymmetrically. Defaults are safe exactly when being wrong is loud.

Integer settings go through `_int()` rather than a bare `int()`, so `BIND_PORT=abc`
raises `ConfigError` like everything else instead of escaping as a traceback.

The Discord bot needs the **Message Content** privileged intent. The Slack app needs
Socket Mode, an app-level token with `connections:write`, the bot scopes `chat:write`,
`groups:write`, `groups:read`, `files:read`, `files:write`, `reactions:write`, and the
bot events `message.groups`, `channel_created`, `channel_rename`, `channel_archive`.

## Tests

`pytest`, no network access required, ~240 tests. The pure helpers are tested directly;
`delivery.py` is driven through `FakeAdapter` in `tests/fakes.py`, which implements
every `ChatAdapter` member and records what was posted — including attachment filenames
and bytes, so a passcode leaking as an attachment fails the assertions rather than
slipping past a text-only check. Anything touching passcode suppression, signature
validation, or the media token must keep those tests green.

## Deployment manifests

Three paths, all reading the same variables:

- `docker-compose.yml` — builds locally, runs bridge + `cloudflared`, no published ports.
- `sms-bridge.service` — systemd unit for a venv install at `/opt/sms-bridge`.
- `stack.portainer.yml` — pulls a published image; `IMAGE_REF` supplies the tag.

`.env.example` is consumed by **both** Compose (`env_file`) and systemd
(`EnvironmentFile`), which constrains its syntax: plain `KEY=VALUE`, no quotes, no
`export`, no trailing comments. systemd strips quotes and Compose doesn't, so a quoted
value silently means different things in the two paths. Keep new variables unquoted.

`BIND_HOST` and `DB_PATH` differ between the container and host installs, so they're
commented out in `.env.example` — the image's own `ENV` defaults cover the Docker case.

`docker-compose.yml` passes `.env` through wholesale via `env_file`, so new variables
flow without edits. `stack.portainer.yml` enumerates every variable individually,
because Portainer supplies them from its own panel rather than a file — a new variable
has to be added there by hand or it silently never reaches the container.

## Public-repo conventions

Docs and manifests are written for a general audience: placeholders (`<owner>/<repo>`,
`sms.example.com`, `IMAGE_REF`) rather than any specific deployment. Don't reintroduce
host- or account-specific details. Licensed MIT.
