from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from app.agent import (
    AgentChatRequest,
    AgentStreamCompleted,
    AgentTextDelta,
    ChatMessage,
    CosmicAgentService,
    LLMProviderFactory,
)
from app.agent.llm_provider import LLMClientBinding, ProviderDefinition
from app.agent.mcp_tooling import MCPToolCatalog, mcp_tools_to_provider_schema
from app.agent.runtime import LLMRuntimeRegistry
from app.config import ConfigManager, Settings, SQLiteSettingsStore
from app.core import MCPTool, MCPToolCall, MCPToolResult


class ToolSelectingRuntime:
    def __init__(self) -> None:
        self.selection_messages: Sequence[ChatMessage] | None = None
        self.final_messages: Sequence[ChatMessage] | None = None

    async def select_tool_calls(
        self,
        binding: LLMClientBinding,
        messages: Sequence[ChatMessage],
        catalog: MCPToolCatalog,
    ) -> tuple[MCPToolCall, ...]:
        self.selection_messages = messages
        provider_tool_name = catalog.tools[0].provider_name
        call = catalog.to_mcp_call(provider_tool_name, {"path": "notes/today.md"})
        assert call is not None
        return (call, call)

    async def stream_text(
        self,
        binding: LLMClientBinding,
        messages: Sequence[ChatMessage],
    ) -> AsyncIterator[str]:
        self.final_messages = messages
        assert "<mcp_context>" in messages[-1].content
        assert "The launch checklist says ship it." in messages[-1].content
        yield "The MCP file says: "
        yield "ship it."

    async def generate_json(
        self,
        binding: LLMClientBinding,
        prompt: str,
        schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        raise AssertionError("MCP orchestration must not invoke JSON parsing")


class FakeMCPClient:
    def __init__(self) -> None:
        self.initialized = False
        self.calls: list[MCPToolCall] = []

    async def initialize(self) -> dict[str, object]:
        self.initialized = True
        return {"ok": True}

    async def list_tools(self) -> tuple[MCPTool, ...]:
        return (
            MCPTool(
                name="local/read-file",
                description="Read a local text file",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            ),
        )

    async def call_tool(self, call: MCPToolCall) -> MCPToolResult:
        self.calls.append(call)
        return MCPToolResult(
            name=call.name,
            text="The launch checklist says ship it.",
            raw={"content": [{"type": "text", "text": "The launch checklist says ship it."}]},
        )


def build_mcp_service(
    tmp_path: Path,
    runtime: ToolSelectingRuntime,
    mcp_client: FakeMCPClient,
) -> CosmicAgentService:
    store = SQLiteSettingsStore(tmp_path / "config.sqlite3")
    settings = Settings(
        _env_file=None,
        config_db_path=store.path,
        default_provider="codex",
        default_model="codex-test",
        system_prompt="System prompt",
        persona="test-persona",
        mcp_enabled=True,
    )
    manager = ConfigManager(settings, store)
    factory = LLMProviderFactory(
        manager,
        providers=(ProviderDefinition("codex", lambda api_key: object()),),
    )
    registry = LLMRuntimeRegistry({"codex": runtime})
    return CosmicAgentService(manager, factory, registry, mcp_client=mcp_client)  # type: ignore[arg-type]


def test_mcp_tools_convert_to_provider_function_schemas() -> None:
    tools = [
        MCPTool(
            name="local/read-file",
            description="Read a file",
            input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        )
    ]

    openai_schema = mcp_tools_to_provider_schema("openai", tools)
    anthropic_schema = mcp_tools_to_provider_schema("anthropic", tools)
    google_schema = mcp_tools_to_provider_schema("google", tools)

    assert openai_schema[0]["type"] == "function"
    assert openai_schema[0]["function"]["name"] == "local_read-file"  # type: ignore[index]
    assert anthropic_schema[0]["name"] == "local_read-file"
    assert google_schema[0]["name"] == "local_read-file"


@pytest.mark.asyncio
async def test_service_lets_model_select_mcp_tool_and_streams_answer(tmp_path: Path) -> None:
    runtime = ToolSelectingRuntime()
    mcp_client = FakeMCPClient()
    service = build_mcp_service(tmp_path, runtime, mcp_client)

    events = [
        event
        async for event in service.stream_reply_events(
            AgentChatRequest(message="Read the local launch checklist.")
        )
    ]

    assert mcp_client.initialized is True
    assert mcp_client.calls == [MCPToolCall("local/read-file", {"path": "notes/today.md"})]
    assert runtime.selection_messages is not None
    assert "MCP tools may be available" in runtime.selection_messages[0].content
    assert [event for event in events if isinstance(event, AgentTextDelta)] == [
        AgentTextDelta("The MCP file says: "),
        AgentTextDelta("ship it."),
    ]
    assert events[-1] == AgentStreamCompleted(provider="codex", model="codex-test")
