"""Interface adapter protocols shared by CLI, Web, and future Telegram shells."""

from __future__ import annotations

from typing import Protocol


class InterfaceAdapter(Protocol):
    """A user-facing shell that delegates all reasoning to agent services."""

    async def run(self) -> None:
        """Start the interface loop."""
