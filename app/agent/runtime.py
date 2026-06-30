"""Provider-specific streaming and structured JSON runtime adapters."""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Protocol

from app.agent.llm_provider import LLMClientBinding
from app.agent.messages import ChatMessage

_PROVIDER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_DEFAULT_MAX_TEXT_TOKENS = 4_096
_DEFAULT_JSON_TOKENS = 2_048


class LLMRuntimeError(RuntimeError):
    """Base error for provider runtime operations."""


class ProviderRuntimeNotRegisteredError(LLMRuntimeError):
    """Raised when no streaming runtime exists for the resolved provider."""


class ProviderResponseFormatError(LLMRuntimeError):
    """Raised when a provider response cannot be interpreted safely."""


class ProviderRuntime(Protocol):
    """Provider-specific text streaming and JSON generation."""

    def stream_text(
        self,
        binding: LLMClientBinding,
        messages: Sequence[ChatMessage],
    ) -> AsyncIterator[str]:
        """Yield text deltas for the selected model."""
        ...

    async def generate_json(
        self,
        binding: LLMClientBinding,
        prompt: str,
        schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Return a JSON object for background CGI parsing."""
        ...


class LLMRuntimeRegistry:
    """Resolve provider runtime behavior through a small registry."""

    def __init__(self, runtimes: Mapping[str, ProviderRuntime] | None = None) -> None:
        self._runtimes: dict[str, ProviderRuntime] = {}
        for provider, runtime in (runtimes or builtin_provider_runtimes()).items():
            self.register(provider, runtime)

    def register(self, provider: str, runtime: ProviderRuntime, *, replace: bool = False) -> None:
        normalized = self._normalize_provider(provider)
        if normalized in self._runtimes and not replace:
            raise LLMRuntimeError(f"Runtime for provider '{normalized}' is already registered")
        self._runtimes[normalized] = runtime

    async def stream_text(
        self,
        binding: LLMClientBinding,
        messages: Sequence[ChatMessage],
    ) -> AsyncIterator[str]:
        runtime = self._get(binding.provider)
        async for chunk in runtime.stream_text(binding, messages):
            if chunk:
                yield chunk

    async def generate_json(
        self,
        binding: LLMClientBinding,
        prompt: str,
        schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return await self._get(binding.provider).generate_json(binding, prompt, schema)

    def _get(self, provider: str) -> ProviderRuntime:
        normalized = self._normalize_provider(provider)
        try:
            return self._runtimes[normalized]
        except KeyError as exc:
            raise ProviderRuntimeNotRegisteredError(
                f"Provider '{normalized}' does not have a streaming runtime"
            ) from exc

    @staticmethod
    def _normalize_provider(provider: str) -> str:
        normalized = provider.strip().lower()
        if not _PROVIDER_PATTERN.fullmatch(normalized):
            raise LLMRuntimeError("Invalid provider name")
        return normalized


class OpenAIRuntime:
    """Runtime for OpenAI-compatible Chat Completions clients."""

    async def stream_text(
        self,
        binding: LLMClientBinding,
        messages: Sequence[ChatMessage],
    ) -> AsyncIterator[str]:
        chat = _chat_completions(binding.client)
        stream = await _maybe_await(
            chat.create(
                model=binding.model,
                messages=_openai_messages(messages),
                stream=True,
            )
        )
        async for event in stream:
            for text in _extract_openai_chat_delta(event):
                yield text

    async def generate_json(
        self,
        binding: LLMClientBinding,
        prompt: str,
        schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        chat = _chat_completions(binding.client)
        response = await _maybe_await(
            chat.create(
                model=binding.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Return only one valid JSON object matching the provided schema. "
                            "Do not include markdown fences or prose."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"JSON schema:\n{json.dumps(schema, ensure_ascii=False)}\n\n"
                            f"Source text:\n{prompt}"
                        ),
                    },
                ],
                response_format={"type": "json_object"},
            )
        )
        return _coerce_json_object(_extract_openai_chat_message(response))


class AnthropicRuntime:
    """Runtime for Anthropic Messages clients."""

    async def stream_text(
        self,
        binding: LLMClientBinding,
        messages: Sequence[ChatMessage],
    ) -> AsyncIterator[str]:
        system, anthropic_messages = _anthropic_messages(messages)
        kwargs: dict[str, Any] = {
            "model": binding.model,
            "max_tokens": _DEFAULT_MAX_TEXT_TOKENS,
            "messages": anthropic_messages,
            "stream": True,
        }
        if system:
            kwargs["system"] = system
        stream = await _maybe_await(binding.client.messages.create(**kwargs))
        async for event in stream:
            text = _extract_anthropic_text_delta(event)
            if text:
                yield text

    async def generate_json(
        self,
        binding: LLMClientBinding,
        prompt: str,
        schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        response = await _maybe_await(
            binding.client.messages.create(
                model=binding.model,
                max_tokens=_DEFAULT_JSON_TOKENS,
                system=(
                    "Extract CGI memory. Use the provided tool exactly once and do not "
                    "answer with normal prose."
                ),
                messages=[{"role": "user", "content": prompt}],
                tools=[
                    {
                        "name": "extract_cgi_memory",
                        "description": "Extract a compact CGI memory graph from an answer.",
                        "input_schema": dict(schema),
                    }
                ],
                tool_choice={"type": "tool", "name": "extract_cgi_memory"},
            )
        )
        tool_payload = _extract_anthropic_tool_input(response)
        if tool_payload is not None:
            return _coerce_json_object(tool_payload)
        return _coerce_json_object(_extract_anthropic_message_text(response))


class GoogleRuntime:
    """Runtime for the Google Gen AI async client."""

    async def stream_text(
        self,
        binding: LLMClientBinding,
        messages: Sequence[ChatMessage],
    ) -> AsyncIterator[str]:
        system, contents = _google_contents(messages)
        stream = await _maybe_await(
            binding.client.models.generate_content_stream(
                model=binding.model,
                contents=contents,
                config=_google_config(system_instruction=system),
            )
        )
        async for event in stream:
            text = _get(event, "text")
            if isinstance(text, str) and text:
                yield text

    async def generate_json(
        self,
        binding: LLMClientBinding,
        prompt: str,
        schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        response = await _maybe_await(
            binding.client.models.generate_content(
                model=binding.model,
                contents=(
                    "Return one JSON object matching this schema:\n"
                    f"{json.dumps(schema, ensure_ascii=False)}\n\n"
                    f"Source text:\n{prompt}"
                ),
                config=_google_config(
                    system_instruction="Extract compact CGI memory as JSON only.",
                    response_mime_type="application/json",
                ),
            )
        )
        parsed = _get(response, "parsed")
        if parsed is not None:
            return _coerce_json_object(parsed)
        text = _get(response, "text")
        return _coerce_json_object(text)


class CodexRuntime(OpenAIRuntime):
    """Best-effort runtime for Codex clients that expose OpenAI-compatible chat APIs."""


def builtin_provider_runtimes() -> dict[str, ProviderRuntime]:
    """Return built-in runtime adapters."""

    openai_runtime = OpenAIRuntime()
    return {
        "openai": openai_runtime,
        "anthropic": AnthropicRuntime(),
        "google": GoogleRuntime(),
        "codex": CodexRuntime(),
    }


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _get(value: object, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _chat_completions(client: object) -> object:
    chat = _get(client, "chat")
    completions = _get(chat, "completions") if chat is not None else None
    if completions is None:
        raise LLMRuntimeError("Provider client does not expose chat.completions")
    return completions


def _openai_messages(messages: Sequence[ChatMessage]) -> list[dict[str, str]]:
    return [{"role": message.role, "content": message.content} for message in messages]


def _extract_openai_chat_delta(event: object) -> list[str]:
    texts: list[str] = []
    choices = _get(event, "choices") or []
    for choice in choices:
        delta = _get(choice, "delta")
        content = _get(delta, "content")
        texts.extend(_text_parts(content))
    return texts


def _extract_openai_chat_message(response: object) -> str:
    choices = _get(response, "choices") or []
    if not choices:
        raise ProviderResponseFormatError("OpenAI JSON response did not include choices")
    message = _get(choices[0], "message")
    content = _get(message, "content")
    parts = _text_parts(content)
    if not parts:
        raise ProviderResponseFormatError("OpenAI JSON response did not include content")
    return "".join(parts)


def _anthropic_messages(messages: Sequence[ChatMessage]) -> tuple[str, list[dict[str, str]]]:
    system = "\n\n".join(message.content for message in messages if message.role == "system")
    payload = [
        {"role": "assistant" if message.role == "assistant" else "user", "content": message.content}
        for message in messages
        if message.role != "system"
    ]
    return system, payload


def _extract_anthropic_text_delta(event: object) -> str | None:
    if _get(event, "type") != "content_block_delta":
        return None
    delta = _get(event, "delta")
    if _get(delta, "type") not in {None, "text_delta"}:
        return None
    text = _get(delta, "text")
    return text if isinstance(text, str) else None


def _extract_anthropic_tool_input(response: object) -> object | None:
    for block in _get(response, "content") or []:
        if _get(block, "type") == "tool_use":
            return _get(block, "input")
    return None


def _extract_anthropic_message_text(response: object) -> str:
    texts: list[str] = []
    for block in _get(response, "content") or []:
        texts.extend(_text_parts(_get(block, "text")))
    if not texts:
        raise ProviderResponseFormatError("Anthropic JSON response did not include text")
    return "".join(texts)


def _google_contents(messages: Sequence[ChatMessage]) -> tuple[str, list[dict[str, object]]]:
    system = "\n\n".join(message.content for message in messages if message.role == "system")
    contents: list[dict[str, object]] = []
    for message in messages:
        if message.role == "system":
            continue
        role = "model" if message.role == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": message.content}]})
    return system, contents


def _google_config(**kwargs: object) -> object:
    try:
        from google.genai import types
    except ImportError:
        return {key: value for key, value in kwargs.items() if value is not None}
    return types.GenerateContentConfig(
        **{key: value for key, value in kwargs.items() if value is not None}
    )


def _text_parts(content: object) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, Sequence) and not isinstance(content, (bytes, bytearray, str)):
        texts: list[str] = []
        for part in content:
            if isinstance(part, str):
                texts.append(part)
            else:
                text = _get(part, "text")
                if isinstance(text, str):
                    texts.append(text)
        return texts
    return []


def _coerce_json_object(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ProviderResponseFormatError("Provider returned invalid JSON") from exc
        if isinstance(parsed, Mapping):
            return parsed
    raise ProviderResponseFormatError("Provider did not return a JSON object")
