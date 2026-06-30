"""Provider-neutral agent orchestration for streaming chat responses."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from app.agent.llm_provider import LLMClientBinding, LLMProviderFactory
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
from app.core import (
    LLMUsageWrite,
    MCPContextBundle,
    MCPContextItem,
    MCPResource,
    MCPToolCall,
    MCPToolResult,
    RemoteMCPClient,
    SQLiteUsageStore,
    estimate_text_tokens,
    estimate_usage_cost,
)

logger = logging.getLogger("uvicorn.error")


class CosmicAgentService:
    """Application service shared by HTTP, CLI, Telegram, and future interfaces."""

    def __init__(
        self,
        config_manager: ConfigManager,
        provider_factory: LLMProviderFactory,
        runtime_registry: LLMRuntimeRegistry,
        usage_store: SQLiteUsageStore | None = None,
        mcp_client: RemoteMCPClient | None = None,
    ) -> None:
        self._config_manager = config_manager
        self._provider_factory = provider_factory
        self._runtime_registry = runtime_registry
        self._usage_store = usage_store
        self._mcp_client = mcp_client
        self._mcp_initialized = False
        self._mcp_init_lock = asyncio.Lock()

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
            mcp_enabled=settings.mcp_enabled and self._mcp_client is not None,
        )

        yield AgentStreamStarted(provider=binding.provider, model=binding.model)
        messages = await self._inject_mcp_context_if_requested(
            binding=binding,
            messages=messages,
            request=request,
            max_tool_calls=settings.mcp_tool_max_calls,
            max_context_chars=settings.mcp_context_max_chars,
            enabled=settings.mcp_enabled,
        )
        chunks: list[str] = []
        native_prompt_tokens: int | None = None
        native_completion_tokens: int | None = None
        native_total_tokens: int | None = None
        usage_source: str | None = None
        async for chunk in self._runtime_registry.stream_text(binding, messages):
            if chunk.prompt_tokens is not None:
                native_prompt_tokens = chunk.prompt_tokens
            if chunk.completion_tokens is not None:
                native_completion_tokens = chunk.completion_tokens
            if chunk.total_tokens is not None:
                native_total_tokens = chunk.total_tokens
            if chunk.usage_source is not None:
                usage_source = chunk.usage_source
            if chunk.text:
                chunks.append(chunk.text)
                yield AgentTextDelta(text=chunk.text)
        if self._usage_store is not None:
            estimated_prompt_tokens = sum(
                estimate_text_tokens(message.content) for message in messages
            )
            estimated_completion_tokens = estimate_text_tokens("".join(chunks))
            prompt_tokens = (
                native_prompt_tokens
                if native_prompt_tokens is not None
                else estimated_prompt_tokens
            )
            completion_tokens = (
                native_completion_tokens
                if native_completion_tokens is not None
                else estimated_completion_tokens
            )
            token_source = "provider_native" if usage_source is not None else "local_estimate"
            estimated_cost_usd = estimate_usage_cost(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                input_cost_per_million=settings.llm_usage_input_cost_per_million,
                output_cost_per_million=settings.llm_usage_output_cost_per_million,
            )
            await asyncio.to_thread(
                self._usage_store.save,
                LLMUsageWrite(
                    provider=binding.provider,
                    model=binding.model,
                    operation="chat_stream",
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    estimated_cost_usd=estimated_cost_usd,
                    metadata={
                        "history_messages": len(request.history),
                        "native_total_tokens": native_total_tokens,
                        "token_source": token_source,
                        "usage_source": usage_source,
                    },
                ),
            )
            logger.info(
                "LLM usage recorded provider=%s model=%s operation=chat_stream "
                "prompt_tokens=%s completion_tokens=%s estimated_cost_usd=%.8f",
                binding.provider,
                binding.model,
                prompt_tokens,
                completion_tokens,
                estimated_cost_usd,
            )
        yield AgentStreamCompleted(provider=binding.provider, model=binding.model)

    @staticmethod
    def _build_messages(
        *,
        system_prompt: str,
        persona: str,
        request: AgentChatRequest,
        mcp_enabled: bool = False,
    ) -> tuple[ChatMessage, ...]:
        persona_block = f"Persona: {persona}" if persona != "default" else "Persona: default"
        mcp_block = (
            "\n\nMCP tools may be available. If the user's request needs local files, "
            "external tool data, or system context exposed through MCP, call the relevant "
            "tool before answering. Do not invent MCP context."
            if mcp_enabled
            else ""
        )
        return (
            ChatMessage("system", f"{system_prompt}\n\n{persona_block}{mcp_block}"),
            *request.history,
            ChatMessage("user", request.message),
        )

    async def _inject_mcp_context_if_requested(
        self,
        *,
        binding: LLMClientBinding,
        messages: tuple[ChatMessage, ...],
        request: AgentChatRequest,
        max_tool_calls: int,
        max_context_chars: int,
        enabled: bool,
    ) -> tuple[ChatMessage, ...]:
        if not enabled or self._mcp_client is None:
            return messages

        await self._ensure_mcp_initialized()
        tools = await self._mcp_client.list_tools()
        if not tools:
            logger.info("MCP enabled but no tools were advertised")
            return messages

        calls = await self._runtime_registry.select_tool_calls(
            binding=binding,
            messages=messages,
            tools=tools,
        )
        limited_calls = _dedupe_tool_calls(calls)[:max_tool_calls]
        if not limited_calls:
            logger.info("MCP tools available but model did not request a tool")
            return messages

        results: list[MCPToolResult] = []
        for call in limited_calls:
            logger.info(
                "MCP tool selected provider=%s model=%s tool=%s argument_keys=%s",
                binding.provider,
                binding.model,
                call.name,
                sorted(call.arguments),
            )
            result = await self._mcp_client.call_tool(call)
            logger.info(
                "MCP tool completed provider=%s model=%s tool=%s is_error=%s chars=%s",
                binding.provider,
                binding.model,
                result.name,
                result.is_error,
                len(result.text),
            )
            results.append(result)

        context_block = _mcp_results_prompt_block(results, max_chars=max_context_chars)
        if not context_block:
            return messages
        return (
            *messages,
            ChatMessage(
                "user",
                (
                    "Use the MCP tool results below as trusted context for the final answer. "
                    "If a tool result reports an error, explain the limitation clearly.\n\n"
                    f"{context_block}\n\nOriginal user question:\n{request.message}"
                ),
            ),
        )

    async def _ensure_mcp_initialized(self) -> None:
        if self._mcp_client is None or self._mcp_initialized:
            return
        async with self._mcp_init_lock:
            if self._mcp_initialized:
                return
            await self._mcp_client.initialize()
            self._mcp_initialized = True


def _mcp_results_prompt_block(results: list[MCPToolResult], *, max_chars: int) -> str:
    remaining = max_chars
    items: list[MCPContextItem] = []
    for result in results:
        if remaining <= 0:
            break
        text = result.text or "<empty tool result>"
        if result.is_error:
            text = f"[MCP tool returned an error]\n{text}"
        truncated_text = text[:remaining]
        items.append(
            MCPContextItem(
                resource=MCPResource(
                    uri=f"mcp-tool://{result.name}",
                    name=result.name,
                    mime_type="text/plain",
                ),
                text=truncated_text,
                truncated=len(text) > len(truncated_text),
            )
        )
        remaining -= len(truncated_text)
    return MCPContextBundle(items=tuple(items)).as_prompt_block()


def _dedupe_tool_calls(calls: tuple[MCPToolCall, ...]) -> tuple[MCPToolCall, ...]:
    seen: set[tuple[str, str]] = set()
    unique: list[MCPToolCall] = []
    for call in calls:
        key = (
            call.name,
            json.dumps(call.arguments, ensure_ascii=False, sort_keys=True, default=str),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(call)
    return tuple(unique)
