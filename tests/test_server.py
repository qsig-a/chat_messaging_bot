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
from sms_bridge.media import MediaTokens
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
    media = MediaTokens(cfg.media_signing_key)
    app = create_app(cfg, sw, store, delivery, queue, adapter, media)
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
    media = MediaTokens(cfg.media_signing_key)
    app = create_app(cfg, sw, store, Delivery(cfg, adapter, store, sw), queue, adapter, media)
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


def test_media_endpoint_serves_a_valid_token():
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
