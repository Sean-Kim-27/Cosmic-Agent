from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from fastapi.testclient import TestClient

from app.agent import (
    AgentChatRequest,
    AgentStreamCompleted,
    AgentStreamStarted,
    AgentTextDelta,
    CGIParseJob,
    LLMProviderFactory,
    ProviderDefinition,
)
from app.api.application import create_app
from app.api.dependencies import (
    get_agent_service,
    get_cgi_background_parser,
    get_cgi_memory_store,
    get_chat_history_store,
    get_config_manager,
    get_provider_factory,
    get_usage_store,
)
from app.config import ConfigManager, Settings, SQLiteSettingsStore
from app.core import (
    CGIMemoryWrite,
    LLMUsageWrite,
    SQLiteCGIMemoryStore,
    SQLiteChatHistoryStore,
    SQLiteUsageStore,
    parse_cgi_memory_document,
)


class FakeAgentService:
    async def stream_reply_events(self, request: AgentChatRequest) -> AsyncIterator[object]:
        yield AgentStreamStarted(provider="google", model="gemma-test")
        yield AgentTextDelta(text="streamed")
        yield AgentStreamCompleted(provider="google", model="gemma-test")


class FakeCGIParser:
    def __init__(self) -> None:
        self.jobs: list[CGIParseJob] = []

    async def enqueue_and_process_safely(self, job: CGIParseJob) -> None:
        self.jobs.append(job)


def build_manager(tmp_path: Path) -> ConfigManager:
    store = SQLiteSettingsStore(tmp_path / "config.sqlite3")
    settings = Settings(
        _env_file=None,
        config_db_path=store.path,
        default_provider="google",
        default_model="gemma-test",
        cgi_parse_provider="google",
        cgi_parse_model="gemma-test",
        google_api_key="google-test-key",
    )
    return ConfigManager(settings, store)


def build_memory_store(tmp_path: Path) -> SQLiteCGIMemoryStore:
    store = SQLiteCGIMemoryStore(tmp_path / "memory.sqlite3")
    store.save(
        CGIMemoryWrite(
            session_id="phase55",
            user_message="remember",
            assistant_answer="answer",
            parser_provider="google",
            parser_model="gemma-test",
            document=parse_cgi_memory_document(
                {
                    "nodes": [
                        {
                            "label": "Dashboard API",
                            "summary": "Compatibility endpoints are present.",
                        }
                    ],
                    "edges": [],
                }
            ),
        )
    )
    return store


def build_usage_store(tmp_path: Path) -> SQLiteUsageStore:
    store = SQLiteUsageStore(tmp_path / "usage.sqlite3")
    store.save(
        LLMUsageWrite(
            provider="google",
            model="gemma-test",
            operation="chat_stream",
            prompt_tokens=10,
            completion_tokens=4,
            estimated_cost_usd=0.0,
            metadata={"token_source": "local_estimate"},
        )
    )
    return store


def test_phase55_compat_dashboard_api_and_stream_aliases(tmp_path: Path) -> None:
    parser = FakeCGIParser()
    manager = build_manager(tmp_path)
    memory_store = build_memory_store(tmp_path)
    history_store = SQLiteChatHistoryStore(memory_store.path)
    usage_store = build_usage_store(tmp_path)
    factory = LLMProviderFactory(
        manager,
        providers=(ProviderDefinition("google", lambda api_key: object(), "google_api_key"),),
    )
    app = create_app()
    app.dependency_overrides[get_config_manager] = lambda: manager
    app.dependency_overrides[get_provider_factory] = lambda: factory
    app.dependency_overrides[get_agent_service] = lambda: FakeAgentService()
    app.dependency_overrides[get_cgi_background_parser] = lambda: parser
    app.dependency_overrides[get_cgi_memory_store] = lambda: memory_store
    app.dependency_overrides[get_chat_history_store] = lambda: history_store
    app.dependency_overrides[get_usage_store] = lambda: usage_store

    with TestClient(app) as client:
        openapi = client.get("/openapi.json")
        config = client.get("/api/config")
        memory = client.get("/api/memory/nodes")
        usage = client.get("/api/usage/today")
        stream = client.post(
            "/api/chat/stream",
            json={"message": "hello", "session_id": "s1"},
        )

    paths = openapi.json()["paths"]
    assert "/api/config" in paths
    assert "/api/memory/nodes" in paths
    assert "/api/chat/stream" in paths
    assert config.status_code == 200
    assert memory.status_code == 200
    assert memory.json()["interactions"][0]["nodes"][0]["label"] == "Dashboard API"
    assert usage.status_code == 200
    assert usage.json()["total_tokens"] == 14
    assert stream.status_code == 200
    assert 'event: token\ndata: {"text":"streamed"}' in stream.text
    assert parser.jobs[0].assistant_answer == "streamed"
