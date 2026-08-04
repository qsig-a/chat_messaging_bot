# SignalWire ↔ Slack/Discord SMS bridge

Send and receive SMS/MMS from Slack or Discord, using a SignalWire phone number.

One process bridges **one** platform, chosen with `CHAT_PLATFORM=discord|slack`.
Both behave the same way: one channel per contact, and the channel **topic**
(`sms:+14165550123`) is the routing table, so there's no contact database to
maintain — rename channels, annotate topics, whatever, as long as the `sms:`
token survives. The only persistent state is a small SQLite file used for
webhook de-duplication and delivery-status reactions; deleting it costs almost
nothing.

It's a small Python package with no framework, intended for one number and one
person's traffic. It is not a team helpdesk or a bulk-messaging tool.

## What it does

- Inbound SMS/MMS → posted to that contact's channel (created on first contact)
- Anything typed in a contact channel → sent from your SignalWire number
- `!sms +14165550123 hey` from any channel → starts a new conversation
- Lines starting with `//` are internal notes, never sent
- Attachments go out as real MMS, via a signed URL SignalWire fetches
- ⏳ → ✅ / ❌ reactions driven by carrier delivery receipts
- Messages that look like one-time passcodes are suppressed or diverted
- Webhook signature validation, retry de-duplication, `/healthz`

## Requirements

- A **SignalWire** account with an SMS-capable phone number
- Either a **Discord** server you control, or a **Slack** workspace you can
  install an app into
- Somewhere to run a container or a Python 3.12 process, that stays on
- A way to give SignalWire a public HTTPS URL for webhooks. The examples use a
  Cloudflare Tunnel, which needs a domain on Cloudflare; any reverse proxy with
  a valid certificate works too.

Slack needs no public endpoint of its own — the adapter uses **Socket Mode**, an
outbound WebSocket. The only inbound URL either platform needs is SignalWire's
webhook.

## Setup

Do section 1a or 1b, not both.

### 1a. Discord

1. https://discord.com/developers → New Application → Bot → copy the token.
2. Under **Privileged Gateway Intents**, enable **Message Content Intent**.
   The bot cannot read outbound messages you type without it. (Self-serve while
   the app is in fewer than 100 servers.)
3. *Recommended for personal use* — make the app private, in this order, as the
   portal rejects the reverse:
   1. **Installation** tab → **Install Link** → **None** → Save Changes.
      Under **Installation Contexts**, uncheck **User Install** and leave only
      **Guild Install**.
   2. **Bot** tab → turn **Public Bot** off → Save Changes.

   The other order fails with *"Private application cannot have a default
   authorization link"* — Discord won't unpublish an app that still hands out
   an install link. The verification warning in that message is a red herring;
   verification only matters past 100 servers.

   Public Bot off means only the app owner can install it anywhere. It changes
   nothing about how the bot behaves once running.
4. Invite it with a URL you build yourself — setting the install link to None
   removes the portal's "Add to Server" button. Either use OAuth2 → URL
   Generator (scope `bot`, permissions: *Manage Channels, View Channels, Send
   Messages, Attach Files, Add Reactions, Read Message History*), or go
   straight there with the Application ID from **General Information**:

   ```
   https://discord.com/api/oauth2/authorize?client_id=YOUR_APP_ID&scope=bot&permissions=101456
   ```

   `101456` is exactly that permission set.
5. Invite it to a **private server of your own**. Don't reuse a server other
   people are in — every text the number receives lands there. A private app
   doesn't help with this: it restricts who may *install* the bot, not who can
   read the channels it posts in.
6. Create an `#inbox` channel and, optionally, a category for the `sms-*`
   channels and a locked-down `#codes` channel. Enable Developer Mode
   (Settings → Advanced) to copy channel and server IDs.

### 1b. Slack

1. https://api.slack.com/apps → **Create New App** → *From scratch*, and pick
   the workspace.
2. **Socket Mode** → toggle **Enable Socket Mode** on. It offers to mint an
   app-level token: create one with the `connections:write` scope and copy it —
   this is `SLACK_APP_TOKEN` and starts `xapp-`.
3. **OAuth & Permissions** → *Bot Token Scopes*, add all six:

   | Scope | Why |
   | --- | --- |
   | `chat:write` | post inbound texts and operator notices |
   | `groups:write` | create the private contact channels |
   | `groups:read` | read their topics, and find them again after a restart |
   | `files:read` | download an attachment to send it as MMS |
   | `reactions:write` | the ⏳ → ✅ / ❌ delivery reactions |
   | `files:write` | upload inbound MMS media into the channel |

4. **Event Subscriptions** → enable events, then under *Subscribe to bot
   events* add: `message.groups`, `channel_created`, `channel_rename`,
   `channel_archive`. With Socket Mode on there is no Request URL to verify.
5. **Install App** → install to the workspace, then copy the *Bot User OAuth
   Token* (`xoxb-…`) into `SLACK_BOT_TOKEN`.
6. Create an inbox channel and, optionally, a locked-down channel for
   passcodes, and **`/invite` the bot into both** — Slack will not let it post
   into a channel it is not a member of. To get a channel ID: open the channel,
   click its name, and copy the ID at the bottom of the dialog (`C0…`).
7. Contact channels are created **private**, so by default only the bot can see
   them. Put your own member ID (Profile → ⋯ → Copy member ID, `U0…`) in
   `SLACK_INVITE_USERS` and the bridge invites you to each new one as it is
   created.

### 2. SignalWire

Phone Numbers → your number → **Messaging**:

- Handle messages using: **LaML Webhooks**
- When a message comes in: `POST https://sms.example.com/sms/inbound`

`SIGNALWIRE_SIGNING_KEY` must be the key SignalWire signs webhooks with, not
just any API token from the project, or signature validation rejects everything
inbound while sending keeps working.

### 3. Configure

All configuration is environment variables, read once at startup. Missing
required values cause an immediate exit naming the variable — and naming the
platform that wanted it.

**Common — required on both platforms**

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `CHAT_PLATFORM` | no | `discord` | `discord` or `slack`. One per process |
| `SIGNALWIRE_SPACE_URL` | yes | — | e.g. `example.signalwire.com` |
| `SIGNALWIRE_PROJECT_ID` | yes | — | |
| `SIGNALWIRE_API_TOKEN` | yes | — | REST auth for sending |
| `SIGNALWIRE_SIGNING_KEY` | yes | — | Key SignalWire signs inbound webhooks with. A separate credential from the API token — if you set it to the token, sending still works but every inbound message is rejected |
| `SIGNALWIRE_NUMBER` | yes | — | E.164, e.g. `+14165550123` |
| `PUBLIC_BASE_URL` | yes | — | Public HTTPS base, no trailing slash |

**Discord — required when `CHAT_PLATFORM=discord`**

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `DISCORD_TOKEN` | yes | — | Bot token |
| `DISCORD_GUILD_ID` | yes | — | The server the bot operates in; messages elsewhere are ignored |
| `DISCORD_INBOX_CHANNEL_ID` | yes | — | Gets new-contact notices and delivery failures |
| `DISCORD_CATEGORY_ID` | no | none | Category to create contact channels under |
| `DISCORD_SECURE_CHANNEL_ID` | no | none | Where passcodes go, if anywhere |

**Slack — required when `CHAT_PLATFORM=slack`**

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `SLACK_BOT_TOKEN` | yes | — | `xoxb-…`, from OAuth & Permissions |
| `SLACK_APP_TOKEN` | yes | — | `xapp-…` with `connections:write`, for Socket Mode |
| `SLACK_INBOX_CHANNEL_ID` | yes | — | Gets new-contact notices and delivery failures. Invite the bot |
| `SLACK_SECURE_CHANNEL_ID` | no | none | Where passcodes go, if anywhere. Invite the bot |
| `SLACK_INVITE_USERS` | no | none | Member IDs invited to each new contact channel, comma-separated. Without it, only the bot can see them |

**Behaviour and runtime — both platforms**

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `REDACT_CODES` | no | `true` | See [Passcode handling](#passcode-handling) |
| `VERIFY_SIGNATURE` | no | `true` | Only turn off for local testing |
| `BIND_HOST` | no | `0.0.0.0` | `127.0.0.1` for a non-container install |
| `BIND_PORT` | no | `8080` | |
| `DB_PATH` | no | `bridge.sqlite3` | |
| `HEARTBEAT_URL` | no | none | Pinged every 5 minutes if set |
| `COMMAND_PREFIX` | no | `!sms` | |
| `NOTE_PREFIX` | no | `//` | |
| `MEDIA_SIGNING_KEY` | no | random per process | Signs outbound media URLs. A random key is fine: SignalWire fetches during the send, so a restart only expires URLs already in flight |
| `MAX_MMS_BYTES` | no | `1048576` | Attachments larger than this are not sent, and the bridge says so |

`CHAT_PLATFORM` defaults to `discord` so existing `.env` files keep working.
That default is safe in a way `SIGNALWIRE_SIGNING_KEY`'s would not be: a wrong
platform exits at startup naming the variable it wanted, while a wrong signing
key fails silently and asymmetrically.

### 4. Run it

Two options: Docker (recommended) or a systemd service on the host.

#### Option A — Docker Compose (recommended)

The compose file runs the bridge and a `cloudflared` tunnel together on a
private network, with **no published ports** — the webhook is reachable only
through the tunnel, never from the local network.

```bash
cp .env.example .env      # fill in one platform + SignalWire + TUNNEL_TOKEN
chmod 600 .env
docker compose up -d --build
docker compose logs -f
```

Create the tunnel token in Cloudflare Zero Trust → Networks → Tunnels → create
a tunnel → copy its token into `TUNNEL_TOKEN`, then add a **public hostname**
(e.g. `sms.example.com`) routed to the service URL `http://bridge:8080`.
Set `PUBLIC_BASE_URL=https://sms.example.com` to match.

The SQLite file lives in the `bridge-data` named volume, so it survives
rebuilds. The container runs as a non-root user and has a `/healthz`
healthcheck built in (`docker compose ps` shows health status).

> **Bind address:** inside a container, `127.0.0.1` is the container's own
> loopback and the webhook would be unreachable, so the image defaults
> `BIND_HOST=0.0.0.0`. That's safe **only because no port is published** —
> adding `ports: ["8080:8080"]` exposes an unauthenticated endpoint on your
> local network, guarded solely by the signature check. Don't, unless you
> mean to.

#### Option B — systemd on the host

```bash
sudo useradd -r -s /usr/sbin/nologin sms
sudo mkdir -p /opt/sms-bridge && sudo chown sms:sms /opt/sms-bridge
sudo -u sms cp -r sms_bridge requirements.txt /opt/sms-bridge/
cd /opt/sms-bridge
sudo -u sms python3 -m venv .venv
sudo -u sms .venv/bin/pip install --require-hashes -r requirements.txt
sudo -u sms cp /path/to/.env.example .env   # then fill it in
sudo chmod 600 .env
sudo cp sms-bridge.service /etc/systemd/system/
sudo systemctl enable --now sms-bridge
journalctl -u sms-bridge -f
```

The unit runs `python -m sms_bridge`, so the `sms_bridge/` package directory has
to be copied whole — not a single file, as in older versions.

### 5. Expose the webhook

Docker users: the `cloudflared` service already does this — skip to testing.

For the systemd install, set `BIND_HOST=127.0.0.1` and put a Cloudflare Tunnel
in front rather than forwarding a port:

```bash
cloudflared tunnel create sms
cloudflared tunnel route dns sms sms.example.com
# ingress: hostname sms.example.com -> service http://localhost:8080
```

`PUBLIC_BASE_URL` must match the URL SignalWire actually calls, character for
character — the signature is computed over the full URL. Scheme, host, trailing
slash, all of it.

## Passcode handling

`REDACT_CODES=true` catches messages containing both a code-ish keyword
(*code, OTP, verify, 2FA, PIN…*) and a 4–8 digit number.

- With a secure channel set (`DISCORD_SECURE_CHANNEL_ID` /
  `SLACK_SECURE_CHANNEL_ID`): the message goes there instead of the contact
  channel. Lock that channel's permissions down hard.
- Without one: nothing is written to the chat platform at all; read the code in
  the SignalWire message logs.

Locking that channel down is easy to overdo. If the bot loses access to it —
Discord's **View Channel** / **Send Messages**, or on Slack simply not being a
member — the code is **suppressed**, never rerouted to the contact channel, and
the bridge posts the reason to the inbox channel and logs it. Missing access
never downgrades into posting a passcode somewhere less private. The same check
runs at startup, so a broken secure channel shows up in the log on boot rather
than the next time a code arrives.

If the number never receives verification codes, set `REDACT_CODES=false` and
skip the whole thing.

## Tests

```bash
pip install --require-hashes -r requirements.txt -r requirements-dev.txt
pytest
```

No network access is required. The suite covers the pure helpers and drives the
delivery core through a fake adapter, including every branch of the passcode
decision table. It also runs on every pull request
(`.github/workflows/test.yml`).

## Dependencies

`requirements.in` lists the direct dependencies. `requirements.txt` is a
generated lock — every transitive package pinned to an exact version with a
SHA-256 hash — so a rebuild months from now installs byte-for-byte what was
tested, instead of whatever happened to be newest that day. Installs use
`--require-hashes`, which fails the build rather than drifting silently.
`requirements-dev.in` / `requirements-dev.txt` are the same arrangement for the
test-only packages, which the image does not install.

To upgrade something, edit the `.in` file and regenerate:

```bash
docker run --rm -v "$PWD":/w -w /w python:3.12-slim sh -c \
  "pip install -q pip-tools && pip-compile --generate-hashes --strip-extras -o requirements.txt requirements.in"
```

Don't hand-edit the locks; the hashes won't match and the install will refuse to
proceed.

`python-multipart` has a floor worth respecting: it parses webhook form bodies,
which happens on *every* request **before** the signature check runs, so it is
reachable unauthenticated from the internet. Releases before 0.0.30 carry DoS
and parameter-smuggling advisories. Don't downgrade it.

## Releases and images

Actions → **Release** → *Run workflow* → pick `patch`, `minor`, or `major`. It
reads the newest `vX.Y.Z` tag, bumps that part, writes release notes from the
commits since then (grouped by conventional-commit prefix), creates the tag and
the GitHub release, then builds and pushes the image. The first run starts at
`v0.1.0`. It refuses to run if nothing has been committed since the last tag.

`.github/workflows/publish-image.yml` does the build, pushing `linux/amd64` to
`ghcr.io/<owner>/<repo>` tagged three ways — `v1.2.0`, `1.2`, and `latest`
(a prerelease won't move `latest`). The image name comes from
`${{ github.repository }}`, so forks publish to their own namespace with no
edits. It also runs on releases created by hand in the GitHub UI.

To deploy, pull instead of building on the host:

```bash
docker pull ghcr.io/<owner>/<repo>:latest
```

`stack.portainer.yml` is a ready-made Portainer stack for NAS and home-server
setups, pulling that image alongside `cloudflared`. Set `IMAGE_REF` to the image
you want (`ghcr.io/<owner>/<repo>:latest`, or a pinned tag) in Portainer's
environment-variables panel, along with everything else the stack expects.

If the repo is private the package is too, so Portainer needs a custom registry
entry (`ghcr.io`, your GitHub username, a PAT with `read:packages`) before the
pull will work — the same credentials work for `docker login ghcr.io` on the
command line.

Third-party actions in the workflows are pinned to full commit SHAs with the
version in a trailing comment. Nothing updates them automatically.

## Known limits

- **Numbers are addressed in E.164, and the bridge will dial what you type.**
  `!sms 4165550123` and `!sms 416-555-0123` are treated as NANP and get a `+1`;
  anything else with 8–15 digits is taken as an already-international number and
  gets a bare `+`. So `!sms 20260803 hi` dials `+20260803` — country code +20,
  Egypt — rather than rejecting a date. That is deliberate: it is what lets
  non-NANP numbers work without shipping a country-code table. It also means a
  mistyped number can reach a real handset.
- **Group MMS is not supported.** If someone adds the number to a group text,
  the messages arrive as individual texts and replies won't thread back.
- **Slack thread replies are never sent as SMS.** A reply in a thread stays in
  the workspace; only top-level messages in a contact channel go out. That is
  what makes it possible to discuss a conversation in place.
- **Outbound attachments** are sent as real MMS. The bridge mints a signed URL
  that expires in ten minutes, hands it to SignalWire, and serves the bytes back
  from `GET /media/{token}` when SignalWire fetches it. Anything over
  `MAX_MMS_BYTES` (1 MiB by default) is not sent, and the bridge replies saying
  so. Media rides on the first chunk of a split message only, to avoid paying
  for the same MMS several times. Inbound media goes the other way — downloaded
  and re-uploaded into the channel, up to 8 MB.
- **Long messages** are split at 1500 characters into separate SMS. Anything
  outside the GSM-7 alphabet (emoji, curly quotes) drops the per-segment limit
  from 160 to 70 characters — segment counts are logged.
- **If the host is down**, inbound texts still arrive at SignalWire and sit in
  the message logs, but nothing notifies you. Point `HEARTBEAT_URL` at a
  dead-man's-switch so a monitor tells you, not a person.
- Discord servers cap at 500 channels / 50 per category. Fine at low volume;
  archive old contact channels if you ever approach it.
- One number, one platform, one process. There's no multi-number or
  multi-tenant support.

## Repo layout

| Path | Purpose |
| --- | --- |
| `sms_bridge/` | The application. `delivery.py` holds the platform-agnostic core; `chat/` holds the Discord and Slack adapters |
| `tests/` | pytest suite; no network required |
| `requirements.in` / `requirements.txt` | Direct deps, and the hashed lock generated from them |
| `requirements-dev.in` / `requirements-dev.txt` | The same, for test-only deps |
| `Dockerfile` | Multi-stage build, non-root, with a `/healthz` healthcheck |
| `docker-compose.yml` | Bridge + Cloudflare Tunnel, no published ports (Option A) |
| `sms-bridge.service` | Hardened systemd unit (Option B) |
| `stack.portainer.yml` | Portainer stack pulling a published image |
| `.env.example` | Every configuration variable, documented |

## Operational notes

- `bridge.sqlite3` holds only webhook SIDs (de-dup) and message-ID mappings for
  delivery reactions. Rows older than 30 days are pruned at startup. Deleting it
  costs nothing but reaction updates on in-flight messages.
- `GET /healthz` returns `{"ok", "platform", "queued", "latency_ms"}` —
  connection readiness, the inbound queue depth, and gateway latency where the
  platform reports one (Slack's Socket Mode does not, and reports `0`).
- Everything the bot posts lives on Discord's or Slack's servers, and SMS is not
  an encrypted transport. Treat this as convenience, not confidentiality.

## License

MIT — see [LICENSE](LICENSE). Use it, change it, fork it, ship it in something
you sell. The only condition is that the copyright notice travels with it.
