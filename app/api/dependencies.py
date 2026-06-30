"""FastAPI dependency wiring for shared application services."""

from __future__ import annotations

from functools import lru_cache

from app.agent import CGIBackgroundParser, CosmicAgentService, LLMProviderFactory
from app.agent.runtime import LLMRuntimeRegistry
from app.config import ConfigManager, load_settings
from app.core import SQLiteCGIMemoryStore


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
def get_agent_service() -> CosmicAgentService:
    return CosmicAgentService(
        get_config_manager(),
        get_provider_factory(),
        get_runtime_registry(),
    )


@lru_cache
def get_cgi_background_parser() -> CGIBackgroundParser:
    return CGIBackgroundParser(
        get_config_manager(),
        get_provider_factory(),
        get_runtime_registry(),
        get_cgi_memory_store(),
    )
