from __future__ import annotations

from pathlib import Path

from app.config.settings import Settings


def test_settings_read_multi_provider_keys_from_environment(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-env-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-env-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "google-env-key")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvidia-env-key")
    monkeypatch.setenv("CONFIG_DB_PATH", str(tmp_path / "config.sqlite3"))
    monkeypatch.setenv("CGI_MEMORY_DB_PATH", str(tmp_path / "memory.sqlite3"))
    monkeypatch.setenv("CGI_PARSE_PROVIDER", "OpenAI")
    monkeypatch.setenv("CGI_PARSE_MODEL", "gpt-4o-mini-test")
    monkeypatch.setenv("CGI_PARSE_MAX_NODES", "12")
    monkeypatch.setenv("LLM_PROVIDER", "AnThRoPiC")
    monkeypatch.setenv("LLM_MODEL", "claude-test")

    settings = Settings(_env_file=None)

    assert settings.openai_api_key is not None
    assert settings.openai_api_key.get_secret_value() == "openai-env-key"
    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == "anthropic-env-key"
    assert settings.google_api_key is not None
    assert settings.google_api_key.get_secret_value() == "google-env-key"
    assert settings.nvidia_api_key is not None
    assert settings.nvidia_api_key.get_secret_value() == "nvidia-env-key"
    assert settings.config_db_path == tmp_path / "config.sqlite3"
    assert settings.cgi_memory_db_path == tmp_path / "memory.sqlite3"
    assert settings.cgi_parse_provider == "openai"
    assert settings.cgi_parse_model == "gpt-4o-mini-test"
    assert settings.cgi_parse_max_nodes == 12
    assert settings.default_provider == "anthropic"
    assert settings.default_model == "claude-test"


def test_google_api_key_takes_precedence_over_legacy_gemini_key(monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "preferred-key")
    monkeypatch.setenv("GEMINI_API_KEY", "legacy-key")

    settings = Settings(_env_file=None)

    assert settings.google_api_key is not None
    assert settings.google_api_key.get_secret_value() == "preferred-key"
    assert "preferred-key" not in repr(settings)
