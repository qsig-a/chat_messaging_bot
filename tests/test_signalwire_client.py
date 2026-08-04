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
