from __future__ import annotations

from typing import Any

import pytest

from app.agent.llm_provider import LLMClientBinding
from app.agent.messages import ChatMessage
from app.agent.runtime import (
    AnthropicRuntime,
    GoogleRuntime,
    NvidiaRuntime,
    OpenAIRuntime,
    ProviderResponseFormatError,
)


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


class FakeNvidiaCompletions:
    def __init__(self, response: object | list[object]) -> None:
        self.responses = list(response) if isinstance(response, list) else [response]
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


class FakeNvidiaClient:
    def __init__(
        self,
        response: object | list[object],
        polls: list[object] | None = None,
    ) -> None:
        self.chat = type("Chat", (), {"completions": FakeNvidiaCompletions(response)})()
        self.polls = list(polls or [])
        self.poll_calls: list[tuple[str, object]] = []

    async def get(self, path: str, *, cast_to: object) -> object:
        self.poll_calls.append((path, cast_to))
        return self.polls.pop(0)


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
async def test_nvidia_runtime_polls_pending_completion_before_reading_choices() -> None:
    client = FakeNvidiaClient(
        {"requestId": "pending-request-123"},
        polls=[
            {"requestId": "pending-request-123"},
            {
                "choices": [{"message": {"content": "NVIDIA answer"}}],
                "usage": {
                    "prompt_tokens": 4,
                    "completion_tokens": 2,
                    "total_tokens": 6,
                },
            },
        ],
    )
    binding = LLMClientBinding("nvidia", "minimaxai/minimax-m2.5", client)
    runtime = NvidiaRuntime(poll_interval_seconds=0, max_poll_attempts=3)

    chunks = [
        chunk
        async for chunk in runtime.stream_text(
            binding,
            [ChatMessage("system", "sys"), ChatMessage("user", "hi")],
        )
    ]

    assert "".join(chunk.text for chunk in chunks) == "NVIDIA answer"
    assert chunks[-1].prompt_tokens == 4
    assert chunks[-1].completion_tokens == 2
    assert chunks[-1].total_tokens == 6
    assert chunks[-1].usage_source == "nvidia_completion_usage"
    assert client.chat.completions.calls == [
        {
            "model": "minimaxai/minimax-m2.5",
            "stream": False,
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "hi"},
            ],
        }
    ]
    assert [path for path, _ in client.poll_calls] == [
        "/status/pending-request-123",
        "/status/pending-request-123",
    ]
    assert all(cast_to == dict[str, object] for _, cast_to in client.poll_calls)


@pytest.mark.asyncio
async def test_nvidia_runtime_surfaces_provider_error_instead_of_choices_error() -> None:
    client = FakeNvidiaClient({"detail": "The selected model is unavailable"})
    binding = LLMClientBinding("nvidia", "retired/model", client)
    runtime = NvidiaRuntime(poll_interval_seconds=0)

    with pytest.raises(
        ProviderResponseFormatError,
        match="NVIDIA API returned an error.*selected model is unavailable",
    ):
        _ = [
            chunk
            async for chunk in runtime.stream_text(
                binding,
                [ChatMessage("user", "hi")],
            )
        ]


@pytest.mark.asyncio
async def test_nvidia_runtime_retries_empty_m3_completion_with_model_defaults() -> None:
    client = FakeNvidiaClient(
        [
            {
                "id": "chatcmpl-empty",
                "choices": [],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 0,
                    "total_tokens": 11,
                },
            },
            {
                "id": "chatcmpl-success",
                "choices": [{"message": {"content": "MiniMax M3 answer"}}],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 4,
                    "total_tokens": 15,
                },
            },
        ]
    )
    binding = LLMClientBinding("nvidia", "minimaxai/minimax-m3", client)
    runtime = NvidiaRuntime(
        poll_interval_seconds=0,
        empty_choice_max_retries=1,
        empty_choice_retry_base_seconds=0,
    )

    chunks = [
        chunk
        async for chunk in runtime.stream_text(
            binding,
            [ChatMessage("user", "hi")],
        )
    ]

    assert "".join(chunk.text for chunk in chunks) == "MiniMax M3 answer"
    assert len(client.chat.completions.calls) == 2
    assert all(call["max_tokens"] == 8_192 for call in client.chat.completions.calls)
    assert all(call["temperature"] == 1.0 for call in client.chat.completions.calls)
    assert all(call["top_p"] == 0.95 for call in client.chat.completions.calls)


@pytest.mark.asyncio
async def test_nvidia_runtime_reports_usage_after_empty_choice_retries_exhausted() -> None:
    empty_response = {
        "id": "chatcmpl-empty",
        "choices": [],
        "usage": {
            "prompt_tokens": 11,
            "completion_tokens": 0,
            "total_tokens": 11,
        },
    }
    client = FakeNvidiaClient(empty_response)
    binding = LLMClientBinding("nvidia", "minimaxai/minimax-m3", client)
    runtime = NvidiaRuntime(
        poll_interval_seconds=0,
        empty_choice_max_retries=1,
        empty_choice_retry_base_seconds=0,
    )

    with pytest.raises(
        ProviderResponseFormatError,
        match=r"empty successful completion after 2 attempts.*completion_tokens=0",
    ):
        _ = [
            chunk
            async for chunk in runtime.stream_text(
                binding,
                [ChatMessage("user", "hi")],
            )
        ]


@pytest.mark.asyncio
async def test_nvidia_json_generation_does_not_require_response_format_support() -> None:
    client = FakeNvidiaClient({"choices": [{"message": {"content": '{"nodes":[],"edges":[]}'}}]})
    binding = LLMClientBinding("nvidia", "nvidia/test-model", client)
    runtime = NvidiaRuntime(poll_interval_seconds=0)

    payload = await runtime.generate_json(binding, "answer", {"type": "object"})

    assert payload == {"nodes": [], "edges": []}
    assert "response_format" not in client.chat.completions.calls[0]


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
