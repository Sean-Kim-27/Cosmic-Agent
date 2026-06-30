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


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


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


class RetryRuntime(JSONRuntime):
    def __init__(self, failures: list[BaseException]) -> None:
        super().__init__()
        self.failures = failures

    async def generate_json(
        self,
        binding: LLMClientBinding,
        prompt: str,
        schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if self.failures:
            raise self.failures.pop(0)
        return await super().generate_json(binding, prompt, schema)


def build_parser(tmp_path: Path, runtime: JSONRuntime) -> CGIBackgroundParser:
    config_store = SQLiteSettingsStore(tmp_path / "config.sqlite3")
    settings = Settings(
        _env_file=None,
        config_db_path=config_store.path,
        cgi_memory_db_path=tmp_path / "memory.sqlite3",
        cgi_parse_provider="openai",
        cgi_parse_model="gpt-4o-mini-test",
        cgi_parse_job_retry_base_seconds=0,
        cgi_parse_job_retry_max_seconds=0,
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


@pytest.mark.asyncio
async def test_quota_exceeded_429_locks_job_without_retry(tmp_path: Path) -> None:
    runtime = RetryRuntime(
        [
            ProviderError(
                "429 RESOURCE_EXHAUSTED: Quota exceeded for quotaMetric "
                "generativelanguage.googleapis.com/generate_content_free_tier",
                status_code=429,
            )
        ]
    )
    parser = build_parser(tmp_path, runtime)

    queued = await parser.enqueue(
        CGIParseJob(
            session_id="quota-session",
            user_message="remember this",
            assistant_answer="this should be queued even if quota is gone",
        )
    )
    processed = await parser.process_due_jobs(limit=1)
    stored = parser._memory_store.get_parse_job(queued.id)

    assert [job.status for job in processed] == ["QUOTA_LOCKED"]
    assert stored is not None
    assert stored.status == "QUOTA_LOCKED"
    assert stored.attempts == 1
    assert stored.last_error_type == "quota_exceeded"
    assert parser._memory_store.count_interactions() == 0


@pytest.mark.asyncio
async def test_transient_429_reschedules_then_completes(tmp_path: Path) -> None:
    runtime = RetryRuntime(
        [ProviderError("429 Too many requests. retryDelay: 0s", status_code=429)]
    )
    parser = build_parser(tmp_path, runtime)

    queued = await parser.enqueue(
        CGIParseJob(
            session_id="rate-session",
            user_message="remember this",
            assistant_answer="this should parse after one retry",
        )
    )

    first = await parser.process_due_jobs(limit=1)
    second = await parser.process_due_jobs(limit=1)
    stored = parser._memory_store.get_parse_job(queued.id)

    assert [job.status for job in first] == ["PENDING"]
    assert [job.status for job in second] == ["COMPLETED"]
    assert stored is not None
    assert stored.status == "COMPLETED"
    assert stored.attempts == 2
    assert stored.interaction_id is not None
    assert parser._memory_store.count_interactions() == 1


@pytest.mark.asyncio
async def test_transient_retry_exhaustion_marks_failed(tmp_path: Path) -> None:
    runtime = RetryRuntime([ProviderError("503 temporarily unavailable", status_code=503)])
    parser = build_parser(tmp_path, runtime)
    settings = parser._config_manager.get_effective_settings().model_copy(
        update={"cgi_parse_job_max_attempts": 1}
    )
    parser._config_manager._base_settings = settings

    queued = await parser.enqueue(
        CGIParseJob(
            session_id="failed-session",
            user_message="remember this",
            assistant_answer="this should fail once",
        )
    )
    processed = await parser.process_due_jobs(limit=1)
    stored = parser._memory_store.get_parse_job(queued.id)

    assert [job.status for job in processed] == ["FAILED"]
    assert stored is not None
    assert stored.status == "FAILED"
    assert stored.attempts == 1
    assert stored.last_error_type == "http_503"
