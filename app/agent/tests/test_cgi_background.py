from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from app.agent import CGIBackgroundParser, CGIParseJob, LLMProviderFactory
from app.agent.llm_provider import LLMClientBinding, ProviderDefinition
from app.agent.messages import ChatMessage
from app.agent.runtime import LLMRuntimeRegistry
from app.config import ConfigManager, Settings, SQLiteSettingsStore
from app.core import SQLiteCGIMemoryStore


class JSONRuntime:
    def __init__(self) -> None:
        self.binding: LLMClientBinding | None = None
        self.prompt = ""

    async def stream_text(
        self,
        binding: LLMClientBinding,
        messages: Sequence[ChatMessage],
    ) -> AsyncIterator[str]:
        raise AssertionError("background parser must not stream user text")
        yield ""

    async def generate_json(
        self,
        binding: LLMClientBinding,
        prompt: str,
        schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self.binding = binding
        self.prompt = prompt
        assert "nodes" in schema.get("properties", {})
        return {
            "nodes": [
                {
                    "label": "Phase 3",
                    "kind": "project_state",
                    "summary": "SSE streaming is implemented before CGI parsing.",
                    "weight": 0.9,
                    "tags": ["phase-3"],
                }
            ],
            "edges": [],
        }


def build_parser(tmp_path: Path, runtime: JSONRuntime) -> CGIBackgroundParser:
    config_store = SQLiteSettingsStore(tmp_path / "config.sqlite3")
    settings = Settings(
        _env_file=None,
        config_db_path=config_store.path,
        cgi_memory_db_path=tmp_path / "memory.sqlite3",
        cgi_parse_provider="openai",
        cgi_parse_model="gpt-4o-mini-test",
        openai_api_key="test-key",
    )
    manager = ConfigManager(settings, config_store)
    factory = LLMProviderFactory(
        manager,
        providers=(
            ProviderDefinition(
                "openai",
                lambda api_key: {"api_key_configured": api_key is not None},
                "openai_api_key",
            ),
        ),
    )
    return CGIBackgroundParser(
        manager,
        factory,
        LLMRuntimeRegistry({"openai": runtime}),
        SQLiteCGIMemoryStore(settings.cgi_memory_db_path),
    )


@pytest.mark.asyncio
async def test_background_parser_uses_cheap_json_model_and_stores_nodes(tmp_path: Path) -> None:
    runtime = JSONRuntime()
    parser = build_parser(tmp_path, runtime)

    record = await parser.parse_and_store(
        CGIParseJob(
            session_id="session-1",
            user_message="What changed?",
            assistant_answer="Phase 3 now streams first and parses in the background.",
        )
    )

    assert record.node_count == 1
    assert record.edge_count == 0
    assert runtime.binding is not None
    assert (runtime.binding.provider, runtime.binding.model) == ("openai", "gpt-4o-mini-test")
    assert "What changed?" in runtime.prompt
