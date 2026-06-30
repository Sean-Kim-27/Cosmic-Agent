from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.agent.llm_provider import (
    LLMProviderFactory,
    MissingProviderCredentialError,
    ModelRequiredError,
    ProviderDefinition,
    ProviderNotRegisteredError,
)
from app.config.manager import ConfigManager
from app.config.secrets import FernetSecretCodec
from app.config.settings import Settings
from app.config.store import SQLiteSettingsStore


def build_factory(
    tmp_path: Path,
    calls: list[tuple[str, str | None]],
) -> tuple[LLMProviderFactory, ConfigManager]:
    store = SQLiteSettingsStore(tmp_path / "config.sqlite3")
    settings = Settings(
        _env_file=None,
        config_db_path=store.path,
        default_provider="codex",
        default_model="gpt-test",
        openai_api_key="openai-environment-key",
        anthropic_api_key="anthropic-environment-key",
    )
    manager = ConfigManager(
        settings,
        store,
        secret_codec=FernetSecretCodec(Fernet.generate_key()),
    )

    def builder(name: str):
        def create(api_key: str | None) -> object:
            calls.append((name, api_key))
            return {"provider": name}

        return create

    factory = LLMProviderFactory(
        manager,
        providers=(
            ProviderDefinition("codex", builder("codex")),
            ProviderDefinition("openai", builder("openai"), "openai_api_key"),
            ProviderDefinition(
                "anthropic",
                builder("anthropic"),
                "anthropic_api_key",
            ),
            ProviderDefinition("google", builder("google"), "google_api_key"),
        ),
    )
    return factory, manager


def test_factory_uses_defaults_and_explicit_provider_without_if_chains(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str | None]] = []
    factory, _ = build_factory(tmp_path, calls)

    default_binding = factory.create()
    openai_binding = factory.create(provider="openai", model="gpt-provider-test")

    assert (default_binding.provider, default_binding.model) == ("codex", "gpt-test")
    assert (openai_binding.provider, openai_binding.model) == (
        "openai",
        "gpt-provider-test",
    )
    assert calls == [
        ("codex", None),
        ("openai", "openai-environment-key"),
    ]


def test_factory_observes_database_key_override_on_next_creation(tmp_path: Path) -> None:
    calls: list[tuple[str, str | None]] = []
    factory, manager = build_factory(tmp_path, calls)

    factory.create(provider="openai", model="gpt-provider-test")
    manager.set_override("openai_api_key", "openai-database-key")
    factory.create(provider="openai", model="gpt-provider-test")

    assert calls == [
        ("openai", "openai-environment-key"),
        ("openai", "openai-database-key"),
    ]


def test_model_alias_routes_to_provider_and_concrete_model(tmp_path: Path) -> None:
    calls: list[tuple[str, str | None]] = []
    factory, manager = build_factory(tmp_path, calls)
    manager.set_model_route("fast-json", "anthropic", "claude-haiku-test")

    binding = factory.create(model="fast-json")

    assert (binding.provider, binding.model) == (
        "anthropic",
        "claude-haiku-test",
    )
    assert calls == [("anthropic", "anthropic-environment-key")]


def test_factory_reports_status_without_exposing_keys(tmp_path: Path) -> None:
    calls: list[tuple[str, str | None]] = []
    factory, _ = build_factory(tmp_path, calls)

    statuses = {status.name: status for status in factory.list_statuses()}

    assert statuses["codex"].configured is True
    assert statuses["codex"].default is True
    assert statuses["openai"].configured is True
    assert statuses["google"].configured is False
    assert "openai-environment-key" not in repr(statuses)


def test_factory_rejects_missing_credentials_and_unknown_providers(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str | None]] = []
    factory, _ = build_factory(tmp_path, calls)

    with pytest.raises(MissingProviderCredentialError):
        factory.create(provider="google", model="gemini-test")
    with pytest.raises(ProviderNotRegisteredError):
        factory.create(provider="unregistered", model="model-test")
    with pytest.raises(ModelRequiredError):
        factory.create(provider="anthropic")
