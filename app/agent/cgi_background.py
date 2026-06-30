"""Background CGI JSON parsing after a user-visible stream completes."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.agent.llm_provider import LLMProviderFactory
from app.agent.runtime import LLMRuntimeRegistry
from app.config import ConfigManager
from app.core import (
    CGIMemoryRecord,
    CGIMemoryWrite,
    SQLiteCGIMemoryStore,
    cgi_memory_json_schema,
    parse_cgi_memory_document,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CGIParseJob:
    """Completed response passed from the streaming path to background parsing."""

    session_id: str | None
    user_message: str
    assistant_answer: str


class CGIBackgroundParser:
    """Parse streamed answers into CGI memory using a cheap JSON-focused model."""

    def __init__(
        self,
        config_manager: ConfigManager,
        provider_factory: LLMProviderFactory,
        runtime_registry: LLMRuntimeRegistry,
        memory_store: SQLiteCGIMemoryStore,
    ) -> None:
        self._config_manager = config_manager
        self._provider_factory = provider_factory
        self._runtime_registry = runtime_registry
        self._memory_store = memory_store

    async def parse_and_store(self, job: CGIParseJob) -> CGIMemoryRecord:
        """Generate CGI JSON with the configured parse model and persist it."""

        settings = self._config_manager.get_effective_settings()
        binding = self._provider_factory.create(
            provider=settings.cgi_parse_provider,
            model=settings.cgi_parse_model,
        )
        payload = await self._runtime_registry.generate_json(
            binding,
            self._build_prompt(job, max_nodes=settings.cgi_parse_max_nodes),
            cgi_memory_json_schema(),
        )
        document = parse_cgi_memory_document(payload).limited(settings.cgi_parse_max_nodes)
        return self._memory_store.save(
            CGIMemoryWrite(
                session_id=job.session_id,
                user_message=job.user_message,
                assistant_answer=job.assistant_answer,
                parser_provider=binding.provider,
                parser_model=binding.model,
                document=document,
            )
        )

    async def parse_and_store_safely(self, job: CGIParseJob) -> CGIMemoryRecord | None:
        """Run background parsing without breaking the completed user response."""

        try:
            return await self.parse_and_store(job)
        except Exception:
            logger.warning("Background CGI parsing failed after stream completion", exc_info=True)
            return None

    @staticmethod
    def _build_prompt(job: CGIParseJob, *, max_nodes: int) -> str:
        return (
            "Extract a compact CGI memory graph from this completed assistant response.\n"
            f"Maximum nodes: {max_nodes}.\n"
            "Create nodes only for durable facts, decisions, preferences, project state, "
            "or meaningful relationships. Avoid transient filler and duplicate labels.\n\n"
            f"User message:\n{job.user_message}\n\n"
            f"Assistant answer:\n{job.assistant_answer}"
        )
