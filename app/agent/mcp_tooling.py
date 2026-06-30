"""MCP tool schema conversion for provider-native function/tool calling."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from app.core import MCPTool, MCPToolCall

ProviderToolFormat = Literal["openai", "anthropic", "google"]

_FUNCTION_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_INVALID_FUNCTION_CHARS = re.compile(r"[^A-Za-z0-9_-]+")


@dataclass(frozen=True, slots=True)
class ProviderMCPTool:
    """One MCP tool with a provider-safe function name."""

    provider_name: str
    mcp_tool: MCPTool

    @property
    def input_schema(self) -> dict[str, object]:
        return _object_json_schema(self.mcp_tool.input_schema)


class MCPToolCatalog:
    """Map MCP tools to provider function schemas and back to MCP tool calls."""

    def __init__(self, tools: tuple[MCPTool, ...]) -> None:
        self._tools = tuple(
            ProviderMCPTool(provider_name=name, mcp_tool=tool)
            for name, tool in _safe_tool_names(tools)
        )
        self._by_provider_name = {tool.provider_name: tool for tool in self._tools}

    @classmethod
    def from_tools(cls, tools: tuple[MCPTool, ...] | list[MCPTool]) -> MCPToolCatalog:
        return cls(tuple(tools))

    @property
    def tools(self) -> tuple[ProviderMCPTool, ...]:
        return self._tools

    def to_mcp_call(self, provider_name: str, arguments: dict[str, object]) -> MCPToolCall | None:
        tool = self._by_provider_name.get(provider_name)
        if tool is None:
            return None
        return MCPToolCall(name=tool.mcp_tool.name, arguments=arguments)

    def openai_tools(self) -> list[dict[str, object]]:
        """Return OpenAI Chat Completions ``tools`` payloads."""

        return [
            {
                "type": "function",
                "function": {
                    "name": tool.provider_name,
                    "description": tool.mcp_tool.description,
                    "parameters": tool.input_schema,
                },
            }
            for tool in self._tools
        ]

    def anthropic_tools(self) -> list[dict[str, object]]:
        """Return Anthropic Messages API ``tools`` payloads."""

        return [
            {
                "name": tool.provider_name,
                "description": tool.mcp_tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in self._tools
        ]

    def google_tools(self) -> list[dict[str, object]]:
        """Return Google Gen AI function declaration payloads."""

        return [
            {
                "name": tool.provider_name,
                "description": tool.mcp_tool.description,
                "parameters": _google_compatible_schema(tool.input_schema),
            }
            for tool in self._tools
        ]


def mcp_tools_to_provider_schema(
    provider: str,
    tools: tuple[MCPTool, ...] | list[MCPTool],
) -> list[dict[str, object]]:
    """Convert MCP ``tools/list`` results into a provider function-call schema."""

    catalog = MCPToolCatalog.from_tools(tools)
    normalized = provider.strip().lower()
    if normalized in {"openai", "codex"}:
        return catalog.openai_tools()
    if normalized == "anthropic":
        return catalog.anthropic_tools()
    if normalized == "google":
        return catalog.google_tools()
    return catalog.openai_tools()


def _safe_tool_names(tools: tuple[MCPTool, ...]) -> list[tuple[str, MCPTool]]:
    used: set[str] = set()
    result: list[tuple[str, MCPTool]] = []
    for index, tool in enumerate(tools, start=1):
        base = _safe_function_name(tool.name) or f"mcp_tool_{index}"
        candidate = base[:64]
        suffix = 2
        while candidate in used:
            suffix_text = f"_{suffix}"
            candidate = f"{base[: 64 - len(suffix_text)]}{suffix_text}"
            suffix += 1
        used.add(candidate)
        result.append((candidate, tool))
    return result


def _safe_function_name(name: str) -> str:
    normalized = _INVALID_FUNCTION_CHARS.sub("_", name.strip()).strip("_")
    if not normalized:
        return ""
    if normalized[0] == "-":
        normalized = f"tool_{normalized}"
    if not _FUNCTION_NAME_PATTERN.fullmatch(normalized[:64]):
        normalized = _INVALID_FUNCTION_CHARS.sub("_", normalized[:64])
    return normalized[:64]


def _object_json_schema(schema: dict[str, object]) -> dict[str, object]:
    if not schema:
        return {"type": "object", "properties": {}, "additionalProperties": True}
    normalized: dict[str, object] = dict(schema)
    if normalized.get("type") != "object":
        normalized = {
            "type": "object",
            "properties": {},
            "additionalProperties": True,
            "x-mcp-original-schema": normalized,
        }
    normalized.setdefault("properties", {})
    return normalized


def _google_compatible_schema(schema: dict[str, object]) -> dict[str, object]:
    """Keep JSON Schema fields commonly accepted by Google function declarations."""

    allowed = {
        "type",
        "properties",
        "required",
        "description",
        "enum",
        "items",
        "nullable",
        "additionalProperties",
    }
    cleaned: dict[str, object] = {}
    for key, value in schema.items():
        if key not in allowed:
            continue
        if key == "properties" and isinstance(value, dict):
            cleaned[key] = {
                str(prop_name): _google_compatible_schema(_dict_or_empty(prop_schema))
                for prop_name, prop_schema in value.items()
            }
        elif key == "items":
            cleaned[key] = _google_compatible_schema(_dict_or_empty(value))
        else:
            cleaned[key] = value
    cleaned.setdefault("type", "object")
    cleaned.setdefault("properties", {})
    return cleaned


def _dict_or_empty(value: Any) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}
