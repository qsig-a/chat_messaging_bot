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


@pytest.mark.parametrize(
    "ctype",
    ["text/plain", "text/plain; charset=utf-8", "Text/Plain; charset=UTF-8",
     "TEXT/PLAIN", "text/plain ; charset=utf-8"],
)
async def test_caption_is_folded_whatever_the_content_type_casing(ctype):
    """HTTP media types are case-insensitive, so redaction must not depend on casing."""
    url = "https://media.example/cap"
    delivery, adapter, _ = build(
        FakeAdapter(secure_result=SecureResult.NOT_CONFIGURED),
        media_bodies={url: (b"Your code is 445566", ctype)},
    )
    await delivery.handle_inbound(
        InboundSms(sender=CONTACT, body="", media_urls=(url,), sid="SMc")
    )
    assert "445566" not in adapter.posted_text()
    assert adapter.posts[0][2] == (), "a caption must never upload as a file"


async def test_passcode_with_a_binary_attachment_posts_no_files():
    """A screenshot of the SMS would leak the code the placeholder is hiding."""
    url = "https://media.example/shot"
    delivery, adapter, _ = build(
        FakeAdapter(secure_result=SecureResult.NOT_CONFIGURED),
        media_bodies={url: (b"\x89PNG-screenshot", "image/png")},
    )
    await delivery.handle_inbound(
        InboundSms(sender=CONTACT, body="Your code is 445566",
                   media_urls=(url,), sid="SMd")
    )
    assert "445566" not in adapter.posted_text()
    assert all(files == () for _, _, files in adapter.posts)


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


async def test_partial_multi_chunk_failure_does_not_later_flip_to_ok():
    """Chunk 1 succeeds, chunk 2 fails; chunk 1's delivery callback must not undo FAIL."""
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(201, json={"sid": "SM-part1"})
        return httpx.Response(400, json={"message": "nope"})

    delivery, adapter, store = build(send_handler=flaky)
    await delivery.handle_outbound(
        adapter.make_outbound("z" * 4000, topic=f"sms:{CONTACT}")
    )
    assert adapter.reactions[-1][1] is Reaction.FAIL
    assert store.lookup_outbound("SM-part1") is None

    await delivery.update_status("SM-part1", "delivered", "")
    assert all(r is not Reaction.OK for _, r in adapter.reactions)


async def test_attachments_are_not_dropped_silently():
    """Until MMS is wired, the user must still learn nothing was sent."""
    delivery, adapter, store = build()
    await delivery.handle_outbound(
        adapter.make_outbound(
            "", topic=f"sms:{CONTACT}",
            attachments=[Attachment(file_id="F1", filename="a.png", size=10)],
        )
    )
    assert store.lookup_outbound("SM-sent") is None
    assert adapter.replies, "the user is told the attachment was not sent"
    assert "not sent" in adapter.replies[0][1]


async def test_empty_note_prefix_does_not_swallow_every_message():
    delivery, adapter, store = build(env_extra={"NOTE_PREFIX": ""})
    await delivery.handle_outbound(
        adapter.make_outbound("hello", topic=f"sms:{CONTACT}")
    )
    assert store.lookup_outbound("SM-sent") is not None


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
