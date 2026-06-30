"""Provider-specific streaming and structured JSON runtime adapters."""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from app.agent.llm_provider import LLMClientBinding
from app.agent.mcp_tooling import MCPToolCatalog
from app.agent.messages import ChatMessage
from app.core import MCPTool, MCPToolCall

_PROVIDER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_DEFAULT_MAX_TEXT_TOKENS = 4_096
_DEFAULT_JSON_TOKENS = 2_048


class LLMRuntimeError(RuntimeError):
    """Base error for provider runtime operations."""


class ProviderRuntimeNotRegisteredError(LLMRuntimeError):
    """Raised when no streaming runtime exists for the resolved provider."""


class ProviderResponseFormatError(LLMRuntimeError):
    """Raised when a provider response cannot be interpreted safely."""


@dataclass(frozen=True, slots=True)
class ProviderTextChunk:
    """One provider stream chunk, optionally carrying native usage metadata."""

    text: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    usage_source: str | None = None


class ProviderRuntime(Protocol):
    """Provider-specific text streaming and JSON generation."""

    def stream_text(
        self,
        binding: LLMClientBinding,
        messages: Sequence[ChatMessage],
    ) -> AsyncIterator[ProviderTextChunk | str]:
        """Yield text deltas and optional provider-native usage metadata."""
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
    ) -> AsyncIterator[ProviderTextChunk]:
        runtime = self._get(binding.provider)
        async for chunk in runtime.stream_text(binding, messages):
            normalized = _coerce_stream_chunk(chunk)
            if (
                normalized.text
                or normalized.prompt_tokens is not None
                or normalized.completion_tokens is not None
                or normalized.total_tokens is not None
            ):
                yield normalized

    async def generate_json(
        self,
        binding: LLMClientBinding,
        prompt: str,
        schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return await self._get(binding.provider).generate_json(binding, prompt, schema)

    async def select_tool_calls(
        self,
        binding: LLMClientBinding,
        messages: Sequence[ChatMessage],
        tools: Sequence[MCPTool],
    ) -> tuple[MCPToolCall, ...]:
        if not tools:
            return ()
        runtime = self._get(binding.provider)
        selector = getattr(runtime, "select_tool_calls", None)
        if selector is None:
            return ()
        catalog = MCPToolCatalog.from_tools(tuple(tools))
        selected = await _maybe_await(selector(binding, messages, catalog))
        if selected is None:
            return ()
        return tuple(call for call in selected if isinstance(call, MCPToolCall))

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
    ) -> AsyncIterator[ProviderTextChunk]:
        chat = _chat_completions(binding.client)
        kwargs = {
            "model": binding.model,
            "messages": _openai_messages(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        try:
            stream = await _maybe_await(chat.create(**kwargs))
        except TypeError:
            kwargs.pop("stream_options")
            stream = await _maybe_await(chat.create(**kwargs))
        async for event in stream:
            usage = _extract_openai_usage(event)
            for text in _extract_openai_chat_delta(event):
                yield ProviderTextChunk(text=text)
            if usage is not None:
                yield usage

    async def select_tool_calls(
        self,
        binding: LLMClientBinding,
        messages: Sequence[ChatMessage],
        catalog: MCPToolCatalog,
    ) -> tuple[MCPToolCall, ...]:
        chat = _chat_completions(binding.client)
        response = await _maybe_await(
            chat.create(
                model=binding.model,
                messages=_openai_messages(messages),
                tools=catalog.openai_tools(),
                tool_choice="auto",
            )
        )
        return _extract_openai_tool_calls(response, catalog)

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
    
class NvidiaRuntime(OpenAIRuntime):
    """Runtime for NVIDIA OpenAI-compatible Chat Completions.

    NVIDIA endpoint can fail with stream=True, so this runtime uses
    non-streaming chat completions and yields the final text as one chunk.
    """

    async def stream_text(
        self,
        binding: LLMClientBinding,
        messages: Sequence[ChatMessage],
    ) -> AsyncIterator[ProviderTextChunk]:
        chat = _chat_completions(binding.client)

        response = await _maybe_await(
            chat.create(
                model=binding.model,
                messages=_openai_messages(messages),
                stream=False,
            )
        )

        text = _extract_openai_chat_message(response)
        if text:
            yield ProviderTextChunk(text=text)

class AnthropicRuntime:
    """Runtime for Anthropic Messages clients."""

    async def stream_text(
        self,
        binding: LLMClientBinding,
        messages: Sequence[ChatMessage],
    ) -> AsyncIterator[ProviderTextChunk]:
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
                yield ProviderTextChunk(text=text)
            usage = _extract_anthropic_usage(event)
            if usage is not None:
                yield usage

    async def select_tool_calls(
        self,
        binding: LLMClientBinding,
        messages: Sequence[ChatMessage],
        catalog: MCPToolCatalog,
    ) -> tuple[MCPToolCall, ...]:
        system, anthropic_messages = _anthropic_messages(messages)
        kwargs: dict[str, Any] = {
            "model": binding.model,
            "max_tokens": 1_024,
            "messages": anthropic_messages,
            "tools": catalog.anthropic_tools(),
            "tool_choice": {"type": "auto"},
        }
        if system:
            kwargs["system"] = system
        response = await _maybe_await(binding.client.messages.create(**kwargs))
        return _extract_anthropic_tool_calls(response, catalog)

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
    ) -> AsyncIterator[ProviderTextChunk]:
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
                yield ProviderTextChunk(text=text)
            usage = _extract_google_usage(event)
            if usage is not None:
                yield usage

    async def select_tool_calls(
        self,
        binding: LLMClientBinding,
        messages: Sequence[ChatMessage],
        catalog: MCPToolCatalog,
    ) -> tuple[MCPToolCall, ...]:
        system, contents = _google_contents(messages)
        response = await _maybe_await(
            binding.client.models.generate_content(
                model=binding.model,
                contents=contents,
                config=_google_config(
                    system_instruction=system,
                    tools=[{"function_declarations": catalog.google_tools()}],
                ),
            )
        )
        return _extract_google_tool_calls(response, catalog)

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


class CodexRuntime:
    """Runtime for the openai-codex SDK thread/turn API.

    The Codex SDK does not expose OpenAI-style ``chat.completions``. It uses a
    thread/turn API instead, so we collect each turn's final response and expose
    it through the same provider-neutral runtime interface.
    """

    async def stream_text(
        self,
        binding: LLMClientBinding,
        messages: Sequence[ChatMessage],
    ) -> AsyncIterator[ProviderTextChunk]:
        prompt = _codex_prompt(messages)
        client = binding.client
        try:
            thread = await client.thread_start(
                model=binding.model,
                cwd=None,
                ephemeral=True,
                service_name="cosmic-agent",
            )
            result = await thread.run(prompt, model=binding.model)
            if result.final_response:
                yield ProviderTextChunk(text=result.final_response)
            usage = _codex_usage_chunk(result)
            if usage is not None:
                yield usage
        finally:
            close = getattr(client, "close", None)
            if close is not None:
                await _maybe_await(close())

    async def generate_json(
        self,
        binding: LLMClientBinding,
        prompt: str,
        schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        client = binding.client
        try:
            thread = await client.thread_start(
                model=binding.model,
                cwd=None,
                ephemeral=True,
                service_name="cosmic-agent",
            )
            result = await thread.run(
                (
                    "Return only one valid JSON object matching the provided schema. "
                    "Do not include markdown fences or prose.\n\n"
                    f"JSON schema:\n{json.dumps(schema, ensure_ascii=False)}\n\n"
                    f"Source text:\n{prompt}"
                ),
                model=binding.model,
                output_schema=dict(schema),
            )
            return _coerce_json_object(result.final_response)
        finally:
            close = getattr(client, "close", None)
            if close is not None:
                await _maybe_await(close())


def builtin_provider_runtimes() -> dict[str, ProviderRuntime]:
    """Return built-in runtime adapters."""

    openai_runtime = OpenAIRuntime()
    return {
        "openai": openai_runtime,
        "anthropic": AnthropicRuntime(),
        "google": GoogleRuntime(),
        "codex": CodexRuntime(),
	"nvidia": NvidiaRuntime(),
    }


def _codex_prompt(messages: Sequence[ChatMessage]) -> str:
    """Flatten neutral chat messages into the single-input format Codex accepts."""

    parts: list[str] = []
    for message in messages:
        role = "Assistant" if message.role == "assistant" else "User"
        if message.role == "system":
            role = "System"
        parts.append(f"{role}: {message.content}")
    return "\n\n".join(parts)


def _codex_usage_chunk(result: object) -> ProviderTextChunk | None:
    usage = _get(result, "usage")
    if usage is None:
        return None
    prompt_tokens = _int_or_none(
        _get(usage, "input_tokens")
        or _get(usage, "prompt_tokens")
        or _get(usage, "total_input_tokens")
    )
    completion_tokens = _int_or_none(
        _get(usage, "output_tokens")
        or _get(usage, "completion_tokens")
        or _get(usage, "total_output_tokens")
    )
    total_tokens = _int_or_none(_get(usage, "total_tokens"))
    if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
        total_tokens = prompt_tokens + completion_tokens
    return ProviderTextChunk(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        usage_source="codex_turn_usage",
    )


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _get(value: object, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _coerce_stream_chunk(value: ProviderTextChunk | str) -> ProviderTextChunk:
    if isinstance(value, ProviderTextChunk):
        return value
    return ProviderTextChunk(text=value)


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


def _extract_openai_usage(event: object) -> ProviderTextChunk | None:
    usage = _get(event, "usage")
    if usage is None:
        return None
    return ProviderTextChunk(
        prompt_tokens=_int_or_none(_get(usage, "prompt_tokens")),
        completion_tokens=_int_or_none(_get(usage, "completion_tokens")),
        total_tokens=_int_or_none(_get(usage, "total_tokens")),
        usage_source="openai_stream_usage",
    )


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


def _extract_openai_tool_calls(
    response: object,
    catalog: MCPToolCatalog,
) -> tuple[MCPToolCall, ...]:
    choices = _get(response, "choices") or []
    if not choices:
        return ()
    message = _get(choices[0], "message")
    calls: list[MCPToolCall] = []
    for tool_call in _get(message, "tool_calls") or []:
        function = _get(tool_call, "function")
        name = _get(function, "name")
        if not isinstance(name, str):
            continue
        arguments = _json_object_arguments(_get(function, "arguments"))
        call = catalog.to_mcp_call(name, arguments)
        if call is not None:
            calls.append(call)
    return tuple(calls)


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


def _extract_anthropic_usage(event: object) -> ProviderTextChunk | None:
    usage = _get(event, "usage")
    if usage is None:
        message = _get(event, "message")
        usage = _get(message, "usage") if message is not None else None
    if usage is None:
        return None
    input_tokens = _int_or_none(_get(usage, "input_tokens"))
    output_tokens = _int_or_none(_get(usage, "output_tokens"))
    total_tokens = None
    if input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    return ProviderTextChunk(
        prompt_tokens=input_tokens,
        completion_tokens=output_tokens,
        total_tokens=total_tokens,
        usage_source="anthropic_stream_usage",
    )


def _extract_anthropic_tool_input(response: object) -> object | None:
    for block in _get(response, "content") or []:
        if _get(block, "type") == "tool_use":
            return _get(block, "input")
    return None


def _extract_anthropic_tool_calls(
    response: object,
    catalog: MCPToolCatalog,
) -> tuple[MCPToolCall, ...]:
    calls: list[MCPToolCall] = []
    for block in _get(response, "content") or []:
        if _get(block, "type") != "tool_use":
            continue
        name = _get(block, "name")
        if not isinstance(name, str):
            continue
        call = catalog.to_mcp_call(name, _json_object_arguments(_get(block, "input")))
        if call is not None:
            calls.append(call)
    return tuple(calls)


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


def _extract_google_usage(event: object) -> ProviderTextChunk | None:
    usage = _get(event, "usage_metadata")
    if usage is None:
        return None
    return ProviderTextChunk(
        prompt_tokens=_int_or_none(_get(usage, "prompt_token_count")),
        completion_tokens=_int_or_none(_get(usage, "candidates_token_count")),
        total_tokens=_int_or_none(_get(usage, "total_token_count")),
        usage_source="google_stream_usage_metadata",
    )


def _extract_google_tool_calls(
    response: object,
    catalog: MCPToolCatalog,
) -> tuple[MCPToolCall, ...]:
    raw_calls: list[object] = []
    function_calls = _get(response, "function_calls")
    if isinstance(function_calls, Sequence) and not isinstance(function_calls, (str, bytes)):
        raw_calls.extend(function_calls)
    for candidate in _get(response, "candidates") or []:
        content = _get(candidate, "content")
        for part in _get(content, "parts") or []:
            function_call = _get(part, "function_call")
            if function_call is not None:
                raw_calls.append(function_call)

    calls: list[MCPToolCall] = []
    for raw_call in raw_calls:
        name = _get(raw_call, "name")
        if not isinstance(name, str):
            continue
        call = catalog.to_mcp_call(name, _json_object_arguments(_get(raw_call, "args")))
        if call is not None:
            calls.append(call)
    return tuple(calls)


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


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _json_object_arguments(value: object) -> dict[str, object]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        if not value.strip():
            return {}
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ProviderResponseFormatError("Provider returned invalid tool arguments") from exc
        if isinstance(parsed, Mapping):
            return dict(parsed)
    raise ProviderResponseFormatError("Provider returned non-object tool arguments")
