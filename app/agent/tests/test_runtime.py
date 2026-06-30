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
                    {
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 3,
                            "completion_tokens": 2,
                            "total_tokens": 5,
                        },
                    },
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
                    {
                        "type": "message_delta",
                        "usage": {"output_tokens": 2},
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
        return AsyncItems(
            [
                {"text": "one"},
                {"text": " two"},
                {
                    "usage_metadata": {
                        "prompt_token_count": 3,
                        "candidates_token_count": 2,
                        "total_token_count": 5,
                    }
                },
            ]
        )

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

    chunks = [
        chunk
        async for chunk in runtime.stream_text(
            binding,
            [ChatMessage("system", "sys"), ChatMessage("user", "hi")],
        )
    ]
    payload = await runtime.generate_json(binding, "answer", {"type": "object"})

    assert "".join(chunk.text for chunk in chunks) == "one two"
    assert chunks[-1].prompt_tokens == 3
    assert chunks[-1].completion_tokens == 2
    assert chunks[-1].usage_source == "openai_stream_usage"
    assert client.chat.completions.calls[0]["stream"] is True
    assert client.chat.completions.calls[0]["stream_options"] == {"include_usage": True}
    assert client.chat.completions.calls[1]["response_format"] == {"type": "json_object"}
    assert payload == {"nodes": [], "edges": []}


@pytest.mark.asyncio
async def test_anthropic_runtime_uses_stream_true_and_tool_choice_for_json() -> None:
    client = FakeAnthropicClient()
    binding = LLMClientBinding("anthropic", "claude-test", client)
    runtime = AnthropicRuntime()

    chunks = [
        chunk
        async for chunk in runtime.stream_text(
            binding,
            [ChatMessage("system", "sys"), ChatMessage("user", "hi")],
        )
    ]
    payload = await runtime.generate_json(binding, "answer", {"type": "object"})

    assert "".join(chunk.text for chunk in chunks) == "one two"
    assert chunks[-1].completion_tokens == 2
    assert chunks[-1].usage_source == "anthropic_stream_usage"
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

    chunks = [
        chunk
        async for chunk in runtime.stream_text(
            binding,
            [ChatMessage("system", "sys"), ChatMessage("user", "hi")],
        )
    ]
    payload = await runtime.generate_json(binding, "answer", {"type": "object"})

    assert "".join(chunk.text for chunk in chunks) == "one two"
    assert chunks[-1].prompt_tokens == 3
    assert chunks[-1].completion_tokens == 2
    assert chunks[-1].total_tokens == 5
    assert chunks[-1].usage_source == "google_stream_usage_metadata"
    assert client.models.stream_calls[0]["model"] == "gemini-test"
    assert client.models.json_calls[0]["model"] == "gemini-test"
    assert "config" in client.models.json_calls[0]
    assert payload == {"nodes": [], "edges": []}
