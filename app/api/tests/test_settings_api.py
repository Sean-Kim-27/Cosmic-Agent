from __future__ import annotations

from pathlib import Path

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.agent import LLMProviderFactory, ProviderDefinition
from app.api.application import create_app
from app.api.dependencies import get_config_manager, get_provider_factory
from app.config import ConfigManager, Settings, SQLiteSettingsStore
from app.config.secrets import FernetSecretCodec


def build_manager(tmp_path: Path) -> ConfigManager:
    store = SQLiteSettingsStore(tmp_path / "config.sqlite3")
    settings = Settings(
        _env_file=None,
        config_db_path=store.path,
        default_provider="openai",
        default_model="gpt-test",
        openai_api_key="env-openai-key",
    )
    return ConfigManager(
        settings,
        store,
        secret_codec=FernetSecretCodec(Fernet.generate_key()),
    )


def build_client(manager: ConfigManager) -> TestClient:
    factory = LLMProviderFactory(
        manager,
        providers=(
            ProviderDefinition("openai", lambda api_key: object(), "openai_api_key"),
            ProviderDefinition("codex", lambda api_key: object()),
        ),
    )
    app = create_app()
    app.dependency_overrides[get_config_manager] = lambda: manager
    app.dependency_overrides[get_provider_factory] = lambda: factory
    return TestClient(app)


def test_settings_dashboard_and_secret_override_do_not_leak_values(tmp_path: Path) -> None:
    manager = build_manager(tmp_path)

    with build_client(manager) as client:
        response = client.get("/api/v1/settings")
        update = client.put("/api/v1/settings/openai_api_key", json={"value": "db-secret"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["providers"][0]["name"] in {"codex", "openai"}
    openai_status = next(item for item in payload["settings"] if item["key"] == "openai_api_key")
    assert openai_status["configured"] is True
    assert openai_status["value"] is None
    assert "env-openai-key" not in response.text

    assert update.status_code == 200
    assert "db-secret" not in update.text
    updated_openai = next(item for item in update.json() if item["key"] == "openai_api_key")
    assert updated_openai["source"] == "database"
    assert updated_openai["value"] is None


def test_model_routes_and_persona_api(tmp_path: Path) -> None:
    manager = build_manager(tmp_path)

    with build_client(manager) as client:
        route_response = client.put(
            "/api/v1/model-routes/fast-json",
            json={"provider": "openai", "model": "gpt-4o-mini"},
        )
        persona_response = client.put(
            "/api/v1/persona",
            json={"system_prompt": "Be exact.", "persona": "operator"},
        )
        persona_get = client.get("/api/v1/persona")

    assert route_response.status_code == 200
    assert route_response.json() == {
        "alias": "fast-json",
        "provider": "openai",
        "model": "gpt-4o-mini",
    }
    assert persona_response.status_code == 200
    assert persona_response.json()["persona"] == "operator"
    assert persona_get.json() == {
        "system_prompt": "Be exact.",
        "persona": "operator",
    }


def test_invalid_setting_returns_404(tmp_path: Path) -> None:
    manager = build_manager(tmp_path)

    with build_client(manager) as client:
        response = client.put("/api/v1/settings/not_allowed", json={"value": "x"})

    assert response.status_code == 404
