"""Codex OAuth-backed client construction."""

from __future__ import annotations


def create_codex_client() -> object:
    """Create an unstarted Codex client that reuses the current user OAuth session."""

    from openai_codex import AsyncCodex

    return AsyncCodex()
