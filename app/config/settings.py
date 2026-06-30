"""Typed environment settings for Cosmic Agent."""

from __future__ import annotations

import os
import re
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROVIDER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _default_config_db_path() -> Path:
    data_home = Path(
        os.environ.get(
            "XDG_DATA_HOME",
            Path.home() / ".local" / "share",
        )
    )
    return data_home / "cosmic-agent" / "config.sqlite3"


def _default_cgi_memory_db_path() -> Path:
    data_home = Path(
        os.environ.get(
            "XDG_DATA_HOME",
            Path.home() / ".local" / "share",
        )
    )
    return data_home / "cosmic-agent" / "cgi-memory.sqlite3"


class Settings(BaseSettings):
    """Environment-backed settings before runtime database overrides."""

    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    google_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    )

    config_encryption_key: SecretStr | None = None
    config_db_path: Path = Field(default_factory=_default_config_db_path)

    default_provider: str = Field(
        default="codex",
        validation_alias=AliasChoices("DEFAULT_PROVIDER", "LLM_PROVIDER"),
    )
    default_model: str = Field(
        default="gpt-5.5",
        validation_alias=AliasChoices("DEFAULT_MODEL", "LLM_MODEL"),
        max_length=256,
    )
    system_prompt: str = Field(
        default="You are Cosmic Agent. Be accurate, useful, and concise.",
        max_length=100_000,
    )
    persona: str = Field(default="default", max_length=100_000)

    cgi_parse_provider: str = Field(default="openai", validation_alias="CGI_PARSE_PROVIDER")
    cgi_parse_model: str = Field(default="gpt-4o-mini", validation_alias="CGI_PARSE_MODEL")
    cgi_parse_max_nodes: int = Field(default=24, ge=1, le=200)
    cgi_memory_db_path: Path = Field(default_factory=_default_cgi_memory_db_path)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        case_sensitive=False,
    )

    @field_validator("default_provider", "cgi_parse_provider")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _PROVIDER_PATTERN.fullmatch(normalized):
            raise ValueError("provider must contain only lowercase letters, numbers, '_' or '-'")
        return normalized

    @field_validator("default_model", "cgi_parse_model", "system_prompt", "persona")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be empty")
        return normalized

    @field_validator("config_db_path", "cgi_memory_db_path")
    @classmethod
    def expand_config_db_path(cls, value: Path) -> Path:
        return value.expanduser()


def load_settings(**overrides: object) -> Settings:
    """Load a fresh settings object so tests and process wiring avoid hidden globals."""

    return Settings(**overrides)
