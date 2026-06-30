"""Agent-level chat request and stream event types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ChatRole = Literal["system", "user", "assistant"]


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """A normalized message passed to provider runtimes."""

    role: ChatRole
    content: str

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant"}:
            raise ValueError("Unsupported chat role")
        if not self.content.strip():
            raise ValueError("Message content must not be empty")


@dataclass(frozen=True, slots=True)
class AgentChatRequest:
    """Provider-neutral chat request from any interface."""

    message: str
    history: tuple[ChatMessage, ...] = field(default_factory=tuple)
    provider: str | None = None
    model: str | None = None

    def __post_init__(self) -> None:
        if not self.message.strip():
            raise ValueError("User message must not be empty")


@dataclass(frozen=True, slots=True)
class AgentStreamStarted:
    """The selected provider and model are ready to stream."""

    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class AgentTextDelta:
    """A user-visible text delta from an LLM stream."""

    text: str


@dataclass(frozen=True, slots=True)
class AgentStreamCompleted:
    """The user-visible LLM stream finished successfully."""

    provider: str
    model: str


AgentStreamEvent = AgentStreamStarted | AgentTextDelta | AgentStreamCompleted
