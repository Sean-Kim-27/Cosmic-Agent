"""Provider-neutral agent orchestration for streaming chat responses."""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.agent.llm_provider import LLMProviderFactory
from app.agent.messages import (
    AgentChatRequest,
    AgentStreamCompleted,
    AgentStreamEvent,
    AgentStreamStarted,
    AgentTextDelta,
    ChatMessage,
)
from app.agent.runtime import LLMRuntimeRegistry
from app.config import ConfigManager


class CosmicAgentService:
    """Application service shared by HTTP, CLI, Telegram, and future interfaces."""

    def __init__(
        self,
        config_manager: ConfigManager,
        provider_factory: LLMProviderFactory,
        runtime_registry: LLMRuntimeRegistry,
    ) -> None:
        self._config_manager = config_manager
        self._provider_factory = provider_factory
        self._runtime_registry = runtime_registry

    async def stream_reply_events(
        self,
        request: AgentChatRequest,
    ) -> AsyncIterator[AgentStreamEvent]:
        """Resolve current settings and stream a reply without CGI parsing work."""

        settings = self._config_manager.get_effective_settings()
        binding = self._provider_factory.create(provider=request.provider, model=request.model)
        messages = self._build_messages(
            system_prompt=settings.system_prompt,
            persona=settings.persona,
            request=request,
        )

        yield AgentStreamStarted(provider=binding.provider, model=binding.model)
        async for chunk in self._runtime_registry.stream_text(binding, messages):
            yield AgentTextDelta(text=chunk)
        yield AgentStreamCompleted(provider=binding.provider, model=binding.model)

    @staticmethod
    def _build_messages(
        *,
        system_prompt: str,
        persona: str,
        request: AgentChatRequest,
    ) -> tuple[ChatMessage, ...]:
        persona_block = f"Persona: {persona}" if persona != "default" else "Persona: default"
        return (
            ChatMessage("system", f"{system_prompt}\n\n{persona_block}"),
            *request.history,
            ChatMessage("user", request.message),
        )
