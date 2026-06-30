from __future__ import annotations

from typing import Any

import pytest

from app.agent.llm_provider import LLMClientBinding
from app.agent.messages import ChatMessage
from app.agent.runtime import AnthropicRuntime, GoogleRuntime, OpenAIRuntime


class AsyncItems:
    def __init__(self, items: list[object]) -> None:
        self._items = items

    async def __aiter__(self):
        for item in self._items:
            yield item


class FakeOpenAICompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            return AsyncItems(
                [
                    {"choices": [{"delta": {"content": "one"}}]},
                    {"choices": [{"delta": {"content": " two"}}]},
                ]
            )
        return {"choices": [{"message": {"content": '{"nodes":[],"edges":[]}'}}]}


class FakeOpenAIClient:
    def __init__(self) -> None:
        self.chat = type("Chat", (), {"completions": FakeOpenAICompletions()})()


class FakeAnthropicMessages:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            return AsyncItems(
                [
                    {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": "one"},
                    },
                    {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": " two"},
                    },
                ]
            )
        return {"content": [{"type": "tool_use", "input": {"nodes": [], "edges": []}}]}


class FakeAnthropicClient:
    def __init__(self) -> None:
        self.messages = FakeAnthropicMessages()


class FakeGoogleModels:
    def __init__(self) -> None:
        self.stream_calls: list[dict[str, Any]] = []
        self.json_calls: list[dict[str, Any]] = []

    async def generate_content_stream(self, **kwargs: Any) -> object:
        self.stream_calls.append(kwargs)
        return AsyncItems([{"text": "one"}, {"text": " two"}])

    async def generate_content(self, **kwargs: Any) -> object:
        self.json_calls.append(kwargs)
        return {"text": '{"nodes":[],"edges":[]}'}


class FakeGoogleClient:
    def __init__(self) -> None:
        self.models = FakeGoogleModels()


@pytest.mark.asyncio
async def test_openai_runtime_uses_stream_true_and_json_response_format() -> None:
    client = FakeOpenAIClient()
    binding = LLMClientBinding("openai", "gpt-test", client)
    runtime = OpenAIRuntime()

    text = [
        chunk
        async for chunk in runtime.stream_text(
            binding,
            [ChatMessage("system", "sys"), ChatMessage("user", "hi")],
        )
    ]
    payload = await runtime.generate_json(binding, "answer", {"type": "object"})

    assert "".join(text) == "one two"
    assert client.chat.completions.calls[0]["stream"] is True
    assert client.chat.completions.calls[1]["response_format"] == {"type": "json_object"}
    assert payload == {"nodes": [], "edges": []}


@pytest.mark.asyncio
async def test_anthropic_runtime_uses_stream_true_and_tool_choice_for_json() -> None:
    client = FakeAnthropicClient()
    binding = LLMClientBinding("anthropic", "claude-test", client)
    runtime = AnthropicRuntime()

    text = [
        chunk
        async for chunk in runtime.stream_text(
            binding,
            [ChatMessage("system", "sys"), ChatMessage("user", "hi")],
        )
    ]
    payload = await runtime.generate_json(binding, "answer", {"type": "object"})

    assert "".join(text) == "one two"
    assert client.messages.calls[0]["stream"] is True
    assert client.messages.calls[1]["tool_choice"] == {
        "type": "tool",
        "name": "extract_cgi_memory",
    }
    assert payload == {"nodes": [], "edges": []}


@pytest.mark.asyncio
async def test_google_runtime_uses_async_stream_and_json_mime_config() -> None:
    client = FakeGoogleClient()
    binding = LLMClientBinding("google", "gemini-test", client)
    runtime = GoogleRuntime()

    text = [
        chunk
        async for chunk in runtime.stream_text(
            binding,
            [ChatMessage("system", "sys"), ChatMessage("user", "hi")],
        )
    ]
    payload = await runtime.generate_json(binding, "answer", {"type": "object"})

    assert "".join(text) == "one two"
    assert client.models.stream_calls[0]["model"] == "gemini-test"
    assert client.models.json_calls[0]["model"] == "gemini-test"
    assert "config" in client.models.json_calls[0]
    assert payload == {"nodes": [], "edges": []}
