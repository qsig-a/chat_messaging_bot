# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file SignalWire ↔ Discord SMS/MMS bridge. All application code lives in
`sms_discord_bridge.py` (~590 lines); everything else is packaging (`Dockerfile`,
`stack.portainer.yml`, `requirements.txt`).

## Commands

```bash
pip install --require-hashes -r requirements.txt
python sms_discord_bridge.py            # needs all required env vars set, see below

docker build -t sms-discord-bridge:latest .

# Regenerate the lock after editing requirements.in — never hand-edit requirements.txt
docker run --rm -v "$PWD":/w -w /w python:3.12-slim sh -c \
  "pip install -q pip-tools && pip-compile --generate-hashes --strip-extras -o requirements.txt requirements.in"
```

`requirements.in` holds the five direct deps; `requirements.txt` is a pip-compile lock
(26 packages, exact versions + SHA-256 hashes). Hand-editing the lock breaks the hash
check and the install refuses to run.

There is no test suite or linter config. Verification is manual: run the process and hit
`GET /healthz` (returns gateway readiness, inbound queue depth, Discord latency).

## CI

Two workflows, both manual-first — nothing runs on push or PR.

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

Two servers share one asyncio event loop, started from `main()`: a `discord.Client`
gateway connection and a `uvicorn`/FastAPI webhook listener. They are decoupled by
`inbound_queue` (`asyncio.Queue`) so webhook handlers return within milliseconds —
SignalWire retries on slow responses. `inbound_worker()` drains the queue and does the
slow Discord work (channel creation, media download/re-upload).

**Channel topics are the routing table.** There is no contact database. A Discord text
channel belongs to a phone number iff its topic contains an `sms:+E164` token
(`number_from_topic` / `topic_for`). `find_channel` scans `guild.text_channels` linearly
on every inbound message. Channels can be freely renamed and their topics annotated as
long as that token survives. Do not add a contacts table — the topic-as-state design is
deliberate and is why the only persistent state is disposable.

**SQLite (`bridge.sqlite3`) is a cache, not a source of truth.** Two tables: `seen`
(webhook `MessageSid` de-duplication, since SignalWire retries) and `outbound`
(SID → channel/message ID, so delivery-status callbacks can swap ⏳ for ✅/❌ on the
right Discord message). Rows older than 30 days are pruned at startup. Deleting the
file loses only in-flight reaction updates.

**Message flow, both directions:**

- Inbound: `POST /sms/inbound` → signature check (`_check`) → dedup (`already_seen`) →
  enqueue → `deliver_inbound` → passcode check → `get_or_create_channel` → post.
- Outbound: `on_message` → skip bots/notes/other guilds → resolve target from either
  the `!sms <number>` command or the channel topic → `handle_outbound` →
  `strip_discord_markup` → `chunk` → `send_sms` per piece → record SIDs.
- Status: `POST /sms/status` → `update_reaction` via the `outbound` table.

**Webhook signature validation** (`valid_signature`) is the Twilio scheme SignalWire's
LaML API uses: HMAC-SHA1 over `PUBLIC_BASE_URL + path` plus sorted form params, keyed
by `SIGNALWIRE_SIGNING_KEY` (which falls back to `SIGNALWIRE_API_TOKEN`). It is the *only*
authentication on the webhook endpoints. The signing key is not necessarily the same
credential as the REST API token — a project can hold several tokens, any of which
authenticates REST calls, while only one signs webhooks. That asymmetry looks like
"sending works, receiving 403s". `explain_bad_signature` exists to tell those cases
apart: on every rejection it re-runs the HMAC against each plausible URL variant and
the other credential, and logs which one would have matched.
`PUBLIC_BASE_URL` must match what SignalWire calls character for character — scheme,
host, trailing slash — or every request 403s. If you change endpoint paths, the literal
path string passed to `_check` must stay in sync with the route.

## Constraints to preserve when editing

- **Do not publish the container's port.** `BIND_HOST` defaults to `0.0.0.0` inside
  Docker because container loopback would make the webhook unreachable; that is only
  safe because both compose setups deliberately expose nothing and route
  `cloudflared` → `http://bridge:8080` on a private network. Adding a `ports:` mapping
  puts an endpoint guarded solely by the signature check on the LAN.
- **Passcode redaction** (`looks_like_a_code`: a code-ish keyword *and* a 4–8 digit
  number) diverts to `DISCORD_SECURE_CHANNEL_ID` if set, otherwise writes nothing to
  Discord at all. The no-secure-channel branch must not leak the body.
- `on_ready` fires again after every gateway reconnect — the `_tasks_started` guard
  keeps background tasks from being spawned repeatedly. Any new background task belongs
  inside that guard.
- Discord markup is literal text over SMS, so outbound bodies go through
  `strip_discord_markup`. Outbound SMS is chunked at `MAX_SMS_CHARS` (1500; SignalWire's
  hard cap is 1600); inbound Discord posts are chunked at 1900 (Discord's cap is 2000).
- `segment_count` implements GSM-7 vs UCS-2 segmentation for logging only — non-GSM-7
  characters (emoji, curly quotes) drop the per-segment limit from 160 to 70.
- **Keep `python-multipart` at 0.0.30+.** `_check` calls `await request.form()` — that
  parser — *before* `valid_signature`, so it processes bodies from any unauthenticated
  internet caller that reaches the tunnel. Pre-0.0.30 releases carry DoS and
  parameter-smuggling advisories. If you ever reorder `_check`, validating the
  signature before parsing the form would shrink this surface considerably.

## Configuration

All config is environment variables read at import time via `_env()`; missing required
vars call `sys.exit`. Required: `DISCORD_TOKEN`, `DISCORD_GUILD_ID`,
`DISCORD_INBOX_CHANNEL_ID`, `SIGNALWIRE_SPACE_URL`, `SIGNALWIRE_PROJECT_ID`,
`SIGNALWIRE_API_TOKEN`, `SIGNALWIRE_NUMBER`, `PUBLIC_BASE_URL`. Optional:
`SIGNALWIRE_SIGNING_KEY`,
`DISCORD_CATEGORY_ID`, `DISCORD_SECURE_CHANNEL_ID`, `REDACT_CODES`, `VERIFY_SIGNATURE`,
`BIND_HOST`, `BIND_PORT`, `DB_PATH`, `HEARTBEAT_URL`, `COMMAND_PREFIX`, `NOTE_PREFIX`.
The Discord bot needs the **Message Content** privileged intent.

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

## Public-repo conventions

Docs and manifests are written for a general audience: placeholders (`<owner>/<repo>`,
`sms.example.com`, `IMAGE_REF`) rather than any specific deployment. Don't reintroduce
host- or account-specific details. Licensed MIT.
