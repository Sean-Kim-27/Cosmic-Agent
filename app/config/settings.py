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


def _default_usage_db_path() -> Path:
    data_home = Path(
        os.environ.get(
            "XDG_DATA_HOME",
            Path.home() / ".local" / "share",
        )
    )
    return data_home / "cosmic-agent" / "usage.sqlite3"


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

    mcp_enabled: bool = Field(default=False)
    mcp_transport: str = Field(default="stdio", max_length=16)
    mcp_stdio_command: str | None = Field(default=None, max_length=1_000)
    mcp_stdio_args: str = Field(default="", max_length=4_000)
    mcp_stdio_cwd: Path | None = None
    mcp_sse_url: str | None = Field(default=None, max_length=2_000)
    mcp_sse_headers_json: str = Field(default="{}", max_length=10_000)
    mcp_context_max_chars: int = Field(default=20_000, ge=1_000, le=200_000)
    mcp_tool_max_calls: int = Field(default=3, ge=1, le=10)

    cgi_parse_provider: str = Field(default="openai", validation_alias="CGI_PARSE_PROVIDER")
    cgi_parse_model: str = Field(default="gpt-4o-mini", validation_alias="CGI_PARSE_MODEL")
    cgi_parse_max_nodes: int = Field(default=24, ge=1, le=200)
    cgi_parse_job_max_attempts: int = Field(default=3, ge=1, le=10)
    cgi_parse_job_retry_base_seconds: float = Field(default=2.0, ge=0.0, le=3600.0)
    cgi_parse_job_retry_max_seconds: float = Field(default=60.0, ge=0.0, le=86_400.0)
    cgi_parse_job_limit_per_run: int = Field(default=3, ge=1, le=50)
    cgi_parse_stale_lock_seconds: int = Field(default=600, ge=1, le=86_400)
    cgi_parse_recovery_interval_seconds: int = Field(default=300, ge=1, le=86_400)
    cgi_parse_recovery_batch_limit: int = Field(default=100, ge=1, le=1_000)
    cgi_memory_db_path: Path = Field(default_factory=_default_cgi_memory_db_path)
    cgi_memory_max_interactions: int = Field(default=200, ge=1, le=10_000)
    cgi_memory_prune_min_weight: float = Field(default=0.05, ge=0.0, le=1.0)

    usage_db_path: Path = Field(default_factory=_default_usage_db_path)
    llm_usage_input_cost_per_million: float = Field(default=0.0, ge=0.0)
    llm_usage_output_cost_per_million: float = Field(default=0.0, ge=0.0)

    frontend_api_secret: SecretStr | None = None
    api_rate_limit_enabled: bool = Field(default=True)
    api_rate_limit_per_minute: int = Field(default=20, ge=1, le=10_000)

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

    @field_validator("mcp_transport")
    @classmethod
    def validate_mcp_transport(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"stdio", "sse"}:
            raise ValueError("mcp_transport must be either 'stdio' or 'sse'")
        return normalized

    @field_validator("default_model", "cgi_parse_model", "system_prompt", "persona")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be empty")
        return normalized

    @field_validator("mcp_stdio_args", "mcp_sse_headers_json")
    @classmethod
    def strip_optional_string_setting(cls, value: str) -> str:
        return value.strip()

    @field_validator("mcp_stdio_command", "mcp_sse_url")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("config_db_path", "cgi_memory_db_path", "usage_db_path", "mcp_stdio_cwd")
    @classmethod
    def expand_config_db_path(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        return value.expanduser()


def load_settings(**overrides: object) -> Settings:
    """Load a fresh settings object so tests and process wiring avoid hidden globals."""

    return Settings(**overrides)
