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
        if file_id not in self.attachments:
            raise KeyError(file_id)
        return self.attachments[file_id]

    async def notify_inbox(self, text: str) -> None:
        self.inbox.append(text)

    async def check_access(self) -> None:
        pass

    def strip_markup(self, text: str) -> str:
        return text

    # -- test helpers ----------------------------------------------------
    def posted_text(self) -> str:
        """Everything a contact channel received - text, filenames, and file bytes.

        Passcode assertions depend on this seeing attachments too: a code that
        leaks as an uploaded file is still a leak.
        """
        parts = []
        for _, text, files in self.posts:
            parts.append(text)
            for f in files:
                parts.append(f.filename)
                parts.append(f.data.decode("utf-8", "replace"))
        return "\n".join(parts)

    def make_outbound(self, text: str, topic: str | None, attachments=()) -> OutboundMessage:
        return OutboundMessage(
            channel=ChannelRef(id="chan-1"),
            message=MessageRef(channel_id="chan-1", message_id="m1"),
            text=text,
            channel_topic=topic,
            attachments=tuple(attachments),
        )
