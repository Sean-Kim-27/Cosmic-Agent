"""FastAPI dependency wiring for shared application services."""

from __future__ import annotations

import json
import shlex
from functools import lru_cache

from app.agent import CGIBackgroundParser, CosmicAgentService, LLMProviderFactory
from app.agent.runtime import LLMRuntimeRegistry
from app.config import ConfigManager, load_settings
from app.core import (
    MCPHTTPConfig,
    MCPServerConfig,
    RemoteMCPClient,
    SQLiteCGIMemoryStore,
    SQLiteChatHistoryStore,
    SQLiteUsageStore,
    SSEMCPTransport,
    StdioMCPTransport,
)


@lru_cache
def get_config_manager() -> ConfigManager:
    """Create the dynamic config manager once while still reading overrides per call."""

    return ConfigManager.from_settings(load_settings())


@lru_cache
def get_provider_factory() -> LLMProviderFactory:
    return LLMProviderFactory(get_config_manager())


@lru_cache
def get_runtime_registry() -> LLMRuntimeRegistry:
    return LLMRuntimeRegistry()


@lru_cache
def get_cgi_memory_store() -> SQLiteCGIMemoryStore:
    settings = get_config_manager().get_effective_settings()
    return SQLiteCGIMemoryStore(settings.cgi_memory_db_path)


@lru_cache
def get_usage_store() -> SQLiteUsageStore:
    settings = get_config_manager().get_effective_settings()
    return SQLiteUsageStore(settings.usage_db_path)


@lru_cache
def get_chat_history_store() -> SQLiteChatHistoryStore:
    settings = get_config_manager().get_effective_settings()
    return SQLiteChatHistoryStore(settings.cgi_memory_db_path)


@lru_cache
def get_mcp_client() -> RemoteMCPClient | None:
    settings = get_config_manager().get_effective_settings()
    if not settings.mcp_enabled:
        return None
    if settings.mcp_transport == "stdio":
        if settings.mcp_stdio_command is None:
            raise RuntimeError("MCP stdio transport requires MCP_STDIO_COMMAND")
        return RemoteMCPClient(
            StdioMCPTransport(
                MCPServerConfig(
                    name="configured-stdio",
                    command=settings.mcp_stdio_command,
                    args=tuple(shlex.split(settings.mcp_stdio_args)),
                    cwd=settings.mcp_stdio_cwd,
                )
            )
        )
    if settings.mcp_sse_url is None:
        raise RuntimeError("MCP SSE transport requires MCP_SSE_URL")
    headers = _parse_mcp_headers(settings.mcp_sse_headers_json)
    return RemoteMCPClient(
        SSEMCPTransport(
            MCPHTTPConfig(name="configured-sse", url=settings.mcp_sse_url, headers=headers)
        )
    )


@lru_cache
def get_agent_service() -> CosmicAgentService:
    return CosmicAgentService(
        get_config_manager(),
        get_provider_factory(),
        get_runtime_registry(),
        get_usage_store(),
        get_mcp_client(),
    )


@lru_cache
def get_cgi_background_parser() -> CGIBackgroundParser:
    return CGIBackgroundParser(
        get_config_manager(),
        get_provider_factory(),
        get_runtime_registry(),
        get_cgi_memory_store(),
        get_usage_store(),
    )


def _parse_mcp_headers(value: str) -> dict[str, str]:
    if not value.strip():
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RuntimeError("MCP_SSE_HEADERS_JSON must be a JSON object") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("MCP_SSE_HEADERS_JSON must be a JSON object")
    return {str(key): str(item) for key, item in payload.items()}
