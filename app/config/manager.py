"""Merge environment settings with validated, database-backed overrides."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import SecretStr, ValidationError

from app.config.secrets import FernetSecretCodec, SecretCodec, SecretCodecError
from app.config.settings import Settings
from app.config.store import ModelRoute, SQLiteSettingsStore, StoredSetting

SECRET_SETTING_KEYS = frozenset(
    {
        "openai_api_key",
        "anthropic_api_key",
        "google_api_key",
        "nvidia_api_key",
    }
)
OVERRIDABLE_SETTING_KEYS = frozenset(
    {
        *SECRET_SETTING_KEYS,
        "cgi_parse_max_nodes",
        "cgi_parse_model",
        "cgi_parse_provider",
        "cgi_parse_job_limit_per_run",
        "cgi_parse_job_max_attempts",
        "cgi_parse_job_retry_base_seconds",
        "cgi_parse_job_retry_max_seconds",
        "cgi_parse_recovery_batch_limit",
        "cgi_parse_recovery_interval_seconds",
        "cgi_parse_stale_lock_seconds",
        "cgi_memory_max_interactions",
        "cgi_memory_prune_min_weight",
        "default_provider",
        "default_model",
        "llm_usage_input_cost_per_million",
        "llm_usage_output_cost_per_million",
        "api_rate_limit_enabled",
        "api_rate_limit_per_minute",
        "system_prompt",
        "persona",
    }
)

_PROVIDER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")

SettingSource = Literal["default", "environment", "database"]


class ConfigManagerError(RuntimeError):
    """Base error for runtime configuration operations."""


class UnknownSettingError(ConfigManagerError):
    """Raised when a caller tries to override an unsupported setting."""


class InvalidConfigValueError(ConfigManagerError):
    """Raised when a runtime override fails schema validation."""


class SecretOverrideUnavailableError(ConfigManagerError):
    """Raised when a secret override is requested without encryption."""


@dataclass(frozen=True, slots=True)
class SettingStatus:
    """Dashboard-safe setting metadata."""

    key: str
    source: SettingSource
    secret: bool
    configured: bool
    value: str | None


class ConfigManager:
    """Resolve fresh effective settings and persist validated overrides."""

    def __init__(
        self,
        base_settings: Settings,
        store: SQLiteSettingsStore,
        *,
        secret_codec: SecretCodec | None = None,
    ) -> None:
        self._base_settings = base_settings
        self._store = store
        self._secret_codec = secret_codec
        self._store.initialize()

    @classmethod
    def from_settings(cls, settings: Settings) -> ConfigManager:
        codec: SecretCodec | None = None
        if settings.config_encryption_key is not None:
            codec = FernetSecretCodec.from_secret(settings.config_encryption_key)
        return cls(
            settings,
            SQLiteSettingsStore(settings.config_db_path),
            secret_codec=codec,
        )

    def get_effective_settings(self) -> Settings:
        values = self._base_settings.model_dump()
        for setting in self._store.list_settings():
            self._validate_stored_setting(setting)
            if setting.is_secret:
                if self._secret_codec is None:
                    raise SecretOverrideUnavailableError(
                        f"Encrypted override for '{setting.key}' requires CONFIG_ENCRYPTION_KEY"
                    )
                try:
                    values[setting.key] = self._secret_codec.decode(setting.value)
                except SecretCodecError as exc:
                    raise ConfigManagerError(
                        f"Unable to decrypt database override for '{setting.key}'"
                    ) from exc
            else:
                try:
                    values[setting.key] = json.loads(setting.value)
                except json.JSONDecodeError as exc:
                    raise ConfigManagerError(
                        f"Invalid serialized database override for '{setting.key}'"
                    ) from exc
        try:
            return Settings.model_validate(values)
        except ValidationError as exc:
            raise InvalidConfigValueError("Stored settings failed validation") from exc

    def set_override(self, key: str, value: object) -> Settings:
        self._require_overridable_key(key)
        current_values = self.get_effective_settings().model_dump()
        current_values[key] = value
        try:
            validated = Settings.model_validate(current_values)
        except ValidationError as exc:
            raise InvalidConfigValueError(f"Invalid value for '{key}'") from exc

        validated_value = getattr(validated, key)
        if key in SECRET_SETTING_KEYS:
            if self._secret_codec is None:
                raise SecretOverrideUnavailableError(
                    "CONFIG_ENCRYPTION_KEY is required before storing API keys in SQLite"
                )
            if validated_value is None:
                raise InvalidConfigValueError(
                    f"Use delete_override('{key}') to remove a secret override"
                )
            plain_value = self._secret_value(validated_value)
            if not plain_value.strip():
                raise InvalidConfigValueError(f"Secret setting '{key}' must not be empty")
            serialized = self._secret_codec.encode(plain_value)
            is_secret = True
        else:
            serialized = json.dumps(validated_value, ensure_ascii=False)
            is_secret = False

        self._store.set_setting(
            StoredSetting(
                key=key,
                value=serialized,
                is_secret=is_secret,
            )
        )
        return self.get_effective_settings()

    def delete_override(self, key: str) -> bool:
        self._require_overridable_key(key)
        return self._store.delete_setting(key)

    def list_statuses(self) -> list[SettingStatus]:
        effective = self.get_effective_settings()
        database_keys = {setting.key for setting in self._store.list_settings()}
        statuses: list[SettingStatus] = []

        for key in sorted(OVERRIDABLE_SETTING_KEYS):
            value = getattr(effective, key)
            is_secret = key in SECRET_SETTING_KEYS
            if key in database_keys:
                source: SettingSource = "database"
            elif key in self._base_settings.model_fields_set:
                source = "environment"
            else:
                source = "default"

            if is_secret:
                configured = value is not None and bool(self._secret_value(value).strip())
                public_value = None
            else:
                configured = value is not None
                public_value = str(value) if value is not None else None

            statuses.append(
                SettingStatus(
                    key=key,
                    source=source,
                    secret=is_secret,
                    configured=configured,
                    value=public_value,
                )
            )
        return statuses

    def set_model_route(self, alias: str, provider: str, model: str) -> ModelRoute:
        normalized_alias = alias.strip()
        normalized_provider = provider.strip().lower()
        normalized_model = model.strip()
        if not _ALIAS_PATTERN.fullmatch(normalized_alias):
            raise InvalidConfigValueError("Invalid model route alias")
        if not _PROVIDER_PATTERN.fullmatch(normalized_provider):
            raise InvalidConfigValueError("Invalid provider name")
        if not _MODEL_PATTERN.fullmatch(normalized_model):
            raise InvalidConfigValueError("Invalid provider model id")

        route = ModelRoute(
            alias=normalized_alias,
            provider=normalized_provider,
            model=normalized_model,
        )
        self._store.upsert_model_route(route)
        return route

    def get_model_route(self, alias: str) -> ModelRoute | None:
        return self._store.get_model_route(alias.strip())

    def list_model_routes(self) -> list[ModelRoute]:
        return self._store.list_model_routes()

    def delete_model_route(self, alias: str) -> bool:
        return self._store.delete_model_route(alias.strip())

    @staticmethod
    def _secret_value(value: object) -> str:
        if isinstance(value, SecretStr):
            return value.get_secret_value()
        if isinstance(value, str):
            return value
        raise InvalidConfigValueError("Secret values must be strings")

    @staticmethod
    def _require_overridable_key(key: str) -> None:
        if key not in OVERRIDABLE_SETTING_KEYS:
            raise UnknownSettingError(f"Setting '{key}' cannot be overridden at runtime")

    @staticmethod
    def _validate_stored_setting(setting: StoredSetting) -> None:
        if setting.key not in OVERRIDABLE_SETTING_KEYS:
            raise UnknownSettingError(f"Database contains unsupported setting '{setting.key}'")
        expected_secret = setting.key in SECRET_SETTING_KEYS
        if setting.is_secret != expected_secret:
            raise ConfigManagerError(f"Database secrecy metadata is invalid for '{setting.key}'")
