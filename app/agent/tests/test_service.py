from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from app.agent import (
    AgentChatRequest,
    AgentStreamCompleted,
    AgentStreamStarted,
    AgentTextDelta,
    ChatMessage,
    CosmicAgentService,
    LLMProviderFactory,
)
from app.agent.llm_provider import LLMClientBinding, ProviderDefinition
from app.agent.runtime import LLMRuntimeRegistry
from app.config import ConfigManager, Settings, SQLiteSettingsStore


class FakeRuntime:
    def __init__(self) -> None:
        self.messages: Sequence[ChatMessage] | None = None

    async def stream_text(
        self,
        binding: LLMClientBinding,
        messages: Sequence[ChatMessage],
    ) -> AsyncIterator[str]:
        self.messages = messages
        yield "hello"
        yield " world"

    async def generate_json(
        self,
        binding: LLMClientBinding,
        prompt: str,
        schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        raise AssertionError("streaming service must not parse CGI JSON inline")


def build_service(tmp_path: Path, runtime: FakeRuntime) -> CosmicAgentService:
    store = SQLiteSettingsStore(tmp_path / "config.sqlite3")
    settings = Settings(
        _env_file=None,
        config_db_path=store.path,
        default_provider="codex",
        default_model="codex-test",
        system_prompt="System prompt",
        persona="test-persona",
    )
    manager = ConfigManager(settings, store)
    factory = LLMProviderFactory(
        manager,
        providers=(ProviderDefinition("codex", lambda api_key: object()),),
    )
    registry = LLMRuntimeRegistry({"codex": runtime})
    return CosmicAgentService(manager, factory, registry)


@pytest.mark.asyncio
async def test_service_streams_text_without_inline_cgi_parsing(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    service = build_service(tmp_path, runtime)

    events = [
        event
        async for event in service.stream_reply_events(
            AgentChatRequest(message="Question?", history=(ChatMessage("assistant", "Earlier"),))
        )
    ]

    assert events == [
        AgentStreamStarted(provider="codex", model="codex-test"),
        AgentTextDelta(text="hello"),
        AgentTextDelta(text=" world"),
        AgentStreamCompleted(provider="codex", model="codex-test"),
    ]
    assert runtime.messages is not None
    assert runtime.messages[0].role == "system"
    assert "System prompt" in runtime.messages[0].content
    assert "test-persona" in runtime.messages[0].content
    assert runtime.messages[-1] == ChatMessage("user", "Question?")
