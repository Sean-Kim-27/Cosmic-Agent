"""Telegram, CLI, and Web interface adapters."""

from app.interfaces.base import InterfaceAdapter
from app.interfaces.cli import RichCLIAdapter

__all__ = ["InterfaceAdapter", "RichCLIAdapter"]
