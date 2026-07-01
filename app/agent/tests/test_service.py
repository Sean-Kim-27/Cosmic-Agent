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
    ProviderTextChunk,
)
from app.agent.llm_provider import LLMClientBinding, ProviderDefinition
from app.agent.runtime import LLMRuntimeRegistry
from app.config import ConfigManager, Settings, SQLiteSettingsStore
from app.core import SQLiteUsageStore


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


class UsageRuntime(FakeRuntime):
    async def stream_text(
        self,
        binding: LLMClientBinding,
        messages: Sequence[ChatMessage],
    ) -> AsyncIterator[ProviderTextChunk]:
        self.messages = messages
        yield ProviderTextChunk(text="hello")
        yield ProviderTextChunk(
            prompt_tokens=11,
            completion_tokens=2,
            total_tokens=13,
            usage_source="test_stream_usage",
        )


def build_service(
    tmp_path: Path,
    runtime: FakeRuntime,
    usage_store: SQLiteUsageStore | None = None,
) -> CosmicAgentService:
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
    return CosmicAgentService(manager, factory, registry, usage_store)


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
    assert "provider='codex', model='codex-test'" in runtime.messages[0].content
    assert "do not infer or claim a different upstream brand name" in runtime.messages[0].content
    assert runtime.messages[-1] == ChatMessage("user", "Question?")


@pytest.mark.asyncio
async def test_service_records_provider_native_stream_usage(tmp_path: Path) -> None:
    runtime = UsageRuntime()
    usage_store = SQLiteUsageStore(tmp_path / "usage.sqlite3")
    service = build_service(tmp_path, runtime, usage_store)

    events = [
        event async for event in service.stream_reply_events(AgentChatRequest(message="Question?"))
    ]
    records = usage_store.list_recent(limit=1)

    assert events[-1] == AgentStreamCompleted(provider="codex", model="codex-test")
    assert records[0].prompt_tokens == 11
    assert records[0].completion_tokens == 2
    assert records[0].total_tokens == 13
    assert records[0].metadata["token_source"] == "provider_native"
    assert records[0].metadata["usage_source"] == "test_stream_usage"
