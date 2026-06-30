"""Interface-layer service construction without importing HTTP adapters."""

from __future__ import annotations

from functools import lru_cache

from app.agent import CGIBackgroundParser, CosmicAgentService, LLMProviderFactory
from app.agent.runtime import LLMRuntimeRegistry
from app.config import ConfigManager, load_settings
from app.core import SQLiteCGIMemoryStore, SQLiteUsageStore


@lru_cache
def get_config_manager() -> ConfigManager:
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
def get_agent_service() -> CosmicAgentService:
    return CosmicAgentService(
        get_config_manager(),
        get_provider_factory(),
        get_runtime_registry(),
        get_usage_store(),
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
