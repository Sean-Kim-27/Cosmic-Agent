"""Registry-based LLM client factory with dynamic configuration lookup."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import SecretStr

from app.auth import create_codex_client
from app.config import ConfigManager

ApiKeySetting = Literal[
    "openai_api_key",
    "anthropic_api_key",
    "google_api_key",
    "nvidia_api_key",
]

_PROVIDER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class LLMProviderError(RuntimeError):
    """Base error for provider registration and client creation."""


class ProviderNotRegisteredError(LLMProviderError):
    """Raised when no provider builder has been registered."""


class MissingProviderCredentialError(LLMProviderError):
    """Raised when a provider requires an API key that is not configured."""


class ModelRequiredError(LLMProviderError):
    """Raised when a provider switch does not include a model id."""


class ProviderBuilder(Protocol):
    """Build a provider SDK client from an optional API key."""

    def __call__(self, api_key: str | None) -> object:
        """Create a client without performing a network request."""


@dataclass(frozen=True, slots=True)
class ProviderDefinition:
    """Provider construction metadata registered with the factory."""

    name: str
    builder: ProviderBuilder
    api_key_setting: ApiKeySetting | None = None


@dataclass(frozen=True, slots=True)
class LLMClientBinding:
    """A concrete SDK client paired with its resolved provider and model."""

    provider: str
    model: str
    client: object


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    """Credential-safe provider availability metadata."""

    name: str
    registered: bool
    configured: bool
    default: bool


class LLMProviderFactory:
    """Resolve settings on every call and dispatch through a provider registry."""

    def __init__(
        self,
        config_manager: ConfigManager,
        providers: tuple[ProviderDefinition, ...] | None = None,
    ) -> None:
        self._config_manager = config_manager
        self._providers: dict[str, ProviderDefinition] = {}
        definitions = builtin_provider_definitions() if providers is None else providers
        for definition in definitions:
            self.register(definition)

    def register(self, definition: ProviderDefinition, *, replace: bool = False) -> None:
        name = self._normalize_provider(definition.name)
        if name in self._providers and not replace:
            raise LLMProviderError(f"Provider '{name}' is already registered")
        self._providers[name] = ProviderDefinition(
            name=name,
            builder=definition.builder,
            api_key_setting=definition.api_key_setting,
        )

    def create(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> LLMClientBinding:
        settings = self._config_manager.get_effective_settings()
        resolved_provider, resolved_model = self._resolve_selection(
            provider=provider,
            model=model,
            default_provider=settings.default_provider,
            default_model=settings.default_model,
        )

        try:
            definition = self._providers[resolved_provider]
        except KeyError as exc:
            raise ProviderNotRegisteredError(
                f"Provider '{resolved_provider}' is not registered"
            ) from exc

        api_key: str | None = None
        if definition.api_key_setting is not None:
            secret = getattr(settings, definition.api_key_setting)
            if secret is None or not self._secret_value(secret).strip():
                raise MissingProviderCredentialError(
                    f"API key for provider '{resolved_provider}' is not configured"
                )
            api_key = self._secret_value(secret)

        client = definition.builder(api_key)
        return LLMClientBinding(
            provider=resolved_provider,
            model=resolved_model,
            client=client,
        )

    def list_statuses(self) -> list[ProviderStatus]:
        settings = self._config_manager.get_effective_settings()
        statuses: list[ProviderStatus] = []
        for name, definition in sorted(self._providers.items()):
            if definition.api_key_setting is None:
                configured = True
            else:
                secret = getattr(settings, definition.api_key_setting)
                configured = secret is not None and bool(self._secret_value(secret).strip())
            statuses.append(
                ProviderStatus(
                    name=name,
                    registered=True,
                    configured=configured,
                    default=name == settings.default_provider,
                )
            )
        return statuses

    def _resolve_selection(
        self,
        *,
        provider: str | None,
        model: str | None,
        default_provider: str,
        default_model: str,
    ) -> tuple[str, str]:
        if provider is None and model is not None:
            route = self._config_manager.get_model_route(model)
            if route is not None:
                return route.provider, route.model

        resolved_provider = self._normalize_provider(provider or default_provider)
        if model is not None:
            resolved_model = model.strip()
        elif resolved_provider == default_provider:
            resolved_model = default_model
        else:
            raise ModelRequiredError(
                f"A model is required when switching to provider '{resolved_provider}'"
            )

        if not resolved_model:
            raise ModelRequiredError("Model must not be empty")
        return resolved_provider, resolved_model

    @staticmethod
    def _normalize_provider(provider: str) -> str:
        normalized = provider.strip().lower()
        if not _PROVIDER_PATTERN.fullmatch(normalized):
            raise LLMProviderError("Invalid provider name")
        return normalized

    @staticmethod
    def _secret_value(secret: object) -> str:
        if isinstance(secret, SecretStr):
            return secret.get_secret_value()
        if isinstance(secret, str):
            return secret
        raise MissingProviderCredentialError("Provider API key has an invalid type")


def _build_openai_client(api_key: str | None) -> object:
    from openai import AsyncOpenAI

    return AsyncOpenAI(api_key=api_key)


def _build_anthropic_client(api_key: str | None) -> object:
    from anthropic import AsyncAnthropic

    return AsyncAnthropic(api_key=api_key)


def _build_google_client(api_key: str | None) -> object:
    from google import genai

    return genai.Client(api_key=api_key).aio


def _build_codex_client(api_key: str | None) -> object:
    del api_key
    return create_codex_client()

def _build_nvidia_client(api_key: str | None) -> object:
    from openai import AsyncOpenAI
    return AsyncOpenAI(api_key=api_key,
                       base_url="https://integrate.api.nvidia.com/v1",
		               timeout=60.0
                       )


def builtin_provider_definitions() -> tuple[ProviderDefinition, ...]:
    """Return built-in registrations without embedding routing conditionals."""

    return (
        ProviderDefinition(
            name="openai",
            builder=_build_openai_client,
            api_key_setting="openai_api_key",
        ),
        ProviderDefinition(
            name="anthropic",
            builder=_build_anthropic_client,
            api_key_setting="anthropic_api_key",
        ),
        ProviderDefinition(
            name="google",
            builder=_build_google_client,
            api_key_setting="google_api_key",
        ),
        ProviderDefinition(
            name="codex",
            builder=_build_codex_client,
        ),
        ProviderDefinition(
            name="nvidia",
            builder=_build_nvidia_client,
            api_key_setting="nvidia_api_key",
        ),
    )
