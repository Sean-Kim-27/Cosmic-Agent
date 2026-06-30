from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.config.manager import (
    ConfigManager,
    InvalidConfigValueError,
    SecretOverrideUnavailableError,
    UnknownSettingError,
)
from app.config.secrets import FernetSecretCodec
from app.config.settings import Settings
from app.config.store import SQLiteSettingsStore


def build_manager(
    tmp_path: Path,
    *,
    openai_api_key: str | None = "environment-key",
) -> tuple[ConfigManager, SQLiteSettingsStore]:
    store = SQLiteSettingsStore(tmp_path / "config.sqlite3")
    settings = Settings(
        _env_file=None,
        config_db_path=store.path,
        openai_api_key=openai_api_key,
    )
    codec = FernetSecretCodec(Fernet.generate_key())
    return ConfigManager(settings, store, secret_codec=codec), store


def test_database_overrides_apply_immediately_and_secrets_are_encrypted(
    tmp_path: Path,
) -> None:
    manager, store = build_manager(tmp_path)

    manager.set_override("system_prompt", "Updated prompt")
    manager.set_override("openai_api_key", "database-secret-key")

    effective = manager.get_effective_settings()
    assert effective.system_prompt == "Updated prompt"
    assert effective.openai_api_key is not None
    assert effective.openai_api_key.get_secret_value() == "database-secret-key"

    with sqlite3.connect(store.path) as connection:
        rows = connection.execute("SELECT key, value FROM runtime_settings ORDER BY key").fetchall()
    serialized_database = repr(rows)
    assert "database-secret-key" not in serialized_database

    statuses = {status.key: status for status in manager.list_statuses()}
    assert statuses["openai_api_key"].source == "database"
    assert statuses["openai_api_key"].configured is True
    assert statuses["openai_api_key"].value is None
    assert statuses["system_prompt"].value == "Updated prompt"


def test_deleting_override_restores_environment_value(tmp_path: Path) -> None:
    manager, _ = build_manager(tmp_path)
    manager.set_override("openai_api_key", "database-key")

    assert manager.delete_override("openai_api_key") is True

    restored = manager.get_effective_settings()
    assert restored.openai_api_key is not None
    assert restored.openai_api_key.get_secret_value() == "environment-key"


def test_secret_override_requires_encryption_key(tmp_path: Path) -> None:
    store = SQLiteSettingsStore(tmp_path / "config.sqlite3")
    settings = Settings(_env_file=None, config_db_path=store.path)
    manager = ConfigManager(settings, store)

    with pytest.raises(SecretOverrideUnavailableError):
        manager.set_override("anthropic_api_key", "must-not-be-plaintext")


def test_encrypted_override_survives_manager_restart(tmp_path: Path) -> None:
    encryption_key = Fernet.generate_key().decode("ascii")
    settings = Settings(
        _env_file=None,
        config_db_path=tmp_path / "config.sqlite3",
        config_encryption_key=encryption_key,
    )
    ConfigManager.from_settings(settings).set_override(
        "anthropic_api_key",
        "persisted-secret",
    )

    restarted = ConfigManager.from_settings(settings).get_effective_settings()

    assert restarted.anthropic_api_key is not None
    assert restarted.anthropic_api_key.get_secret_value() == "persisted-secret"


def test_unknown_and_invalid_overrides_are_rejected(tmp_path: Path) -> None:
    manager, _ = build_manager(tmp_path)

    with pytest.raises(UnknownSettingError):
        manager.set_override("config_db_path", "/tmp/elsewhere.sqlite3")
    with pytest.raises(InvalidConfigValueError):
        manager.set_override("default_provider", "Not Allowed!")


def test_model_routes_and_migration_versions_are_persisted(tmp_path: Path) -> None:
    manager, store = build_manager(tmp_path)

    route = manager.set_model_route("fast-json", "anthropic", "claude-haiku-test")

    assert manager.get_model_route("fast-json") == route
    assert manager.list_model_routes() == [route]
    assert store.applied_migration_versions() == (1,)
    assert manager.delete_model_route("fast-json") is True
    assert manager.get_model_route("fast-json") is None


def test_cgi_parse_runtime_settings_can_be_overridden(tmp_path: Path) -> None:
    manager, _ = build_manager(tmp_path)

    manager.set_override("cgi_parse_provider", "anthropic")
    manager.set_override("cgi_parse_model", "claude-3-haiku-test")
    manager.set_override("cgi_parse_max_nodes", 7)

    effective = manager.get_effective_settings()
    assert effective.cgi_parse_provider == "anthropic"
    assert effective.cgi_parse_model == "claude-3-haiku-test"
    assert effective.cgi_parse_max_nodes == 7


def test_store_initialization_is_safe_across_concurrent_workers(tmp_path: Path) -> None:
    database_path = tmp_path / "config.sqlite3"

    def initialize_store() -> None:
        SQLiteSettingsStore(database_path).initialize()

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: initialize_store(), range(8)))

    assert SQLiteSettingsStore(database_path).applied_migration_versions() == (1,)
