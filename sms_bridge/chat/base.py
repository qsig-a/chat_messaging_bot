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
