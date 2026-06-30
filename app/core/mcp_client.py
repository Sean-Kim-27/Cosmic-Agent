"""MCP context ingestion skeleton for local tools and note vaults.

This module deliberately stays inside ``app.core`` and does not import FastAPI,
CLI, Telegram, or LLM SDK objects. Agent orchestration can ask a context source
for text, then inject the returned bundle into a prompt before calling a model.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

_DEFAULT_TEXT_EXTENSIONS = frozenset(
    {
        ".md",
        ".markdown",
        ".txt",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".py",
    }
)


class MCPClientError(RuntimeError):
    """Base error for MCP context loading."""


class MCPAccessError(MCPClientError):
    """Raised when a requested local resource escapes the allowed boundary."""


class MCPTransportError(MCPClientError):
    """Raised when MCP transport or JSON-RPC framing fails."""


class MCPRemoteError(MCPClientError):
    """Raised when an MCP server returns a JSON-RPC error object."""


@dataclass(frozen=True, slots=True)
class MCPResource:
    """A readable context resource exposed through an MCP-like boundary."""

    uri: str
    name: str
    mime_type: str = "text/plain"


@dataclass(frozen=True, slots=True)
class MCPContextItem:
    """Text loaded from one resource for prompt injection."""

    resource: MCPResource
    text: str
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class MCPContextBundle:
    """Prompt-ready context from one or more MCP resources."""

    items: tuple[MCPContextItem, ...] = field(default_factory=tuple)

    def as_prompt_block(self) -> str:
        """Render loaded resources as a compact prompt block."""

        if not self.items:
            return ""
        blocks: list[str] = ["<mcp_context>"]
        for item in self.items:
            suffix = " [truncated]" if item.truncated else ""
            blocks.append(
                f"<resource uri={item.resource.uri!r} name={item.resource.name!r}{suffix}>"
            )
            blocks.append(item.text)
            blocks.append("</resource>")
        blocks.append("</mcp_context>")
        return "\n".join(blocks)


@dataclass(frozen=True, slots=True)
class MCPTool:
    """A tool advertised by a remote MCP server."""

    name: str
    description: str
    input_schema: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MCPToolCall:
    """A model-requested MCP tool invocation."""

    name: str
    arguments: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MCPToolResult:
    """Result from an MCP tool call, normalized for prompt injection."""

    name: str
    text: str
    raw: dict[str, object]
    is_error: bool = False


class MCPContextClient(Protocol):
    """Minimal protocol a future stdio/websocket MCP adapter must implement."""

    def list_resources(self) -> tuple[MCPResource, ...]:
        """Return resources the agent may ask to inject into a prompt."""
        ...

    def read_resource(self, uri: str, *, max_chars: int = 20_000) -> MCPContextItem:
        """Read one resource as text."""
        ...

    def build_context(self, uris: Iterable[str], *, max_chars: int = 20_000) -> MCPContextBundle:
        """Read multiple resources into one prompt-ready bundle."""
        ...


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    """Configuration for a local stdio MCP server process."""

    name: str
    command: str
    args: tuple[str, ...] = ()
    cwd: Path | None = None
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MCPHTTPConfig:
    """Configuration for a remote Streamable HTTP/SSE MCP endpoint."""

    name: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 30.0


class MCPTransport(Protocol):
    """Async JSON-RPC transport boundary for MCP clients."""

    async def request(self, method: str, params: dict[str, object] | None = None) -> object:
        """Send one JSON-RPC request and return the result payload."""
        ...

    async def close(self) -> None:
        """Release transport resources."""
        ...


class StdioMCPTransport:
    """MCP stdio transport using newline-delimited JSON-RPC messages."""

    def __init__(self, config: MCPServerConfig, *, timeout_seconds: float = 30.0) -> None:
        self.config = config
        self.timeout_seconds = timeout_seconds
        self._process: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._lock = asyncio.Lock()

    async def request(self, method: str, params: dict[str, object] | None = None) -> object:
        """Send one request to the child MCP server and wait for its matching response."""

        async with self._lock:
            process = await self._ensure_process()
            if process.stdin is None or process.stdout is None:
                raise MCPTransportError("MCP stdio process streams are unavailable")
            request_id = self._next_id
            self._next_id += 1
            payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            }
            encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
            process.stdin.write(encoded.encode("utf-8"))
            await process.stdin.drain()

            while True:
                response = await asyncio.wait_for(
                    self._read_response_line(process.stdout),
                    timeout=self.timeout_seconds,
                )
                if response.get("id") != request_id:
                    continue
                return _jsonrpc_result(response)

    async def close(self) -> None:
        """Close stdin and terminate the subprocess if it does not exit promptly."""

        process = self._process
        self._process = None
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
            with contextlib.suppress(RuntimeError):
                await process.stdin.wait_closed()
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except asyncio.TimeoutError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

    async def _ensure_process(self) -> asyncio.subprocess.Process:
        if self._process is not None and self._process.returncode is None:
            return self._process
        env = os.environ.copy()
        env.update(self.config.env)
        self._process = await asyncio.create_subprocess_exec(
            self.config.command,
            *self.config.args,
            cwd=str(self.config.cwd) if self.config.cwd is not None else None,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return self._process

    @staticmethod
    async def _read_response_line(reader: asyncio.StreamReader) -> dict[str, object]:
        line = await reader.readline()
        if not line:
            raise MCPTransportError("MCP stdio server closed stdout")
        try:
            payload = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise MCPTransportError("MCP stdio server emitted invalid JSON") from exc
        if not isinstance(payload, dict):
            raise MCPTransportError("MCP stdio server emitted non-object JSON-RPC payload")
        return payload


class SSEMCPTransport:
    """MCP Streamable HTTP/SSE request transport.

    The transport sends JSON-RPC requests with an Accept header for both JSON and
    SSE. If the server returns an SSE stream, the first matching JSON-RPC result
    event is used as the request result.
    """

    def __init__(self, config: MCPHTTPConfig) -> None:
        self.config = config
        self._next_id = 1
        self._lock = asyncio.Lock()

    async def request(self, method: str, params: dict[str, object] | None = None) -> object:
        async with self._lock:
            request_id = self._next_id
            self._next_id += 1
            payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            }
            return await asyncio.to_thread(self._post_jsonrpc, payload, request_id)

    async def close(self) -> None:
        return None

    def _post_jsonrpc(self, payload: dict[str, object], request_id: int) -> object:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **self.config.headers,
        }
        request = urllib.request.Request(
            self.config.url,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                content_type = response.headers.get("content-type", "")
                payload_text = response.read().decode("utf-8")
        except urllib.error.URLError as exc:
            raise MCPTransportError(f"MCP HTTP request failed: {exc}") from exc

        if "text/event-stream" in content_type:
            for message in _parse_sse_jsonrpc_messages(payload_text):
                if str(message.get("id")) == str(request_id):
                    return _jsonrpc_result(message)
            raise MCPTransportError("MCP SSE response did not include the matching JSON-RPC id")

        try:
            json_payload = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            raise MCPTransportError("MCP HTTP response was not valid JSON") from exc
        if not isinstance(json_payload, dict):
            raise MCPTransportError("MCP HTTP response was not a JSON object")
        return _jsonrpc_result(json_payload)


class RemoteMCPClient:
    """Useful MCP client facade for resources and tool-use results."""

    def __init__(self, transport: MCPTransport) -> None:
        self.transport = transport

    async def initialize(
        self,
        *,
        client_name: str = "cosmic-agent",
        client_version: str = "0.1.0",
        protocol_version: str = "2025-06-18",
    ) -> object:
        """Negotiate a basic MCP session with the remote server."""

        return await self.transport.request(
            "initialize",
            {
                "protocolVersion": protocol_version,
                "capabilities": {},
                "clientInfo": {"name": client_name, "version": client_version},
            },
        )

    async def list_tools(self) -> tuple[MCPTool, ...]:
        """Return tools advertised by the remote MCP server."""

        result = await self.transport.request("tools/list")
        tools = _object_list_from_result(result, "tools")
        return tuple(
            MCPTool(
                name=str(tool.get("name", "")),
                description=str(tool.get("description", "")),
                input_schema=_dict_or_empty(tool.get("inputSchema")),
            )
            for tool in tools
            if tool.get("name")
        )

    async def call_tool(self, call: MCPToolCall) -> MCPToolResult:
        """Execute one remote MCP tool and normalize text content for prompts."""

        result = await self.transport.request(
            "tools/call",
            {"name": call.name, "arguments": call.arguments},
        )
        raw = _dict_or_empty(result)
        return MCPToolResult(
            name=call.name,
            text=_extract_mcp_text(raw),
            raw=raw,
            is_error=bool(raw.get("isError")),
        )

    async def list_resources(self) -> tuple[MCPResource, ...]:
        """Return resources advertised by the remote MCP server."""

        result = await self.transport.request("resources/list")
        resources = _object_list_from_result(result, "resources")
        return tuple(
            MCPResource(
                uri=str(resource.get("uri", "")),
                name=str(resource.get("name") or resource.get("uri") or ""),
                mime_type=str(resource.get("mimeType") or "text/plain"),
            )
            for resource in resources
            if resource.get("uri")
        )

    async def read_resource(self, uri: str, *, max_chars: int = 20_000) -> MCPContextItem:
        """Read a remote MCP resource into a prompt-ready text item."""

        result = await self.transport.request("resources/read", {"uri": uri})
        payload = _dict_or_empty(result)
        contents = _object_list_from_mapping(payload, "contents")
        if not contents:
            raise MCPClientError(f"MCP resource did not return text contents: {uri}")
        first = contents[0]
        text = str(first.get("text", ""))
        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars]
        return MCPContextItem(
            resource=MCPResource(
                uri=str(first.get("uri") or uri),
                name=str(first.get("name") or first.get("uri") or uri),
                mime_type=str(first.get("mimeType") or "text/plain"),
            ),
            text=text,
            truncated=truncated,
        )

    async def build_context(
        self,
        *,
        resource_uris: Iterable[str] = (),
        tool_calls: Iterable[MCPToolCall] = (),
        max_chars: int = 20_000,
    ) -> MCPContextBundle:
        """Read resources and tool outputs into one prompt-injectable bundle."""

        remaining = max_chars
        items: list[MCPContextItem] = []
        for uri in resource_uris:
            if remaining <= 0:
                break
            item = await self.read_resource(uri, max_chars=remaining)
            items.append(item)
            remaining -= len(item.text)
        for call in tool_calls:
            if remaining <= 0:
                break
            result = await self.call_tool(call)
            text = result.text[:remaining]
            items.append(
                MCPContextItem(
                    resource=MCPResource(
                        uri=f"mcp-tool://{call.name}",
                        name=call.name,
                        mime_type="text/plain",
                    ),
                    text=text,
                    truncated=len(result.text) > len(text),
                )
            )
            remaining -= len(text)
        return MCPContextBundle(items=tuple(items))

    async def close(self) -> None:
        await self.transport.close()


class LocalDirectoryMCPClient:
    """Safe local-directory context client used as the first MCP bridge.

    It models the same shape a real MCP resource client will expose, while
    limiting reads to a single configured root such as an Obsidian vault.
    """

    def __init__(
        self,
        root: Path,
        *,
        allowed_extensions: Iterable[str] = _DEFAULT_TEXT_EXTENSIONS,
    ) -> None:
        self.root = root.expanduser().resolve()
        self.allowed_extensions = frozenset(extension.lower() for extension in allowed_extensions)

    def list_resources(self) -> tuple[MCPResource, ...]:
        resources: list[MCPResource] = []
        if not self.root.exists():
            return ()
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in self.allowed_extensions:
                continue
            resources.append(self._resource_for_path(path))
        return tuple(resources)

    def read_resource(self, uri: str, *, max_chars: int = 20_000) -> MCPContextItem:
        path = self._path_from_uri(uri)
        if path.suffix.lower() not in self.allowed_extensions:
            raise MCPAccessError(f"File extension is not allowed: {path.suffix}")
        text = path.read_text(encoding="utf-8", errors="replace")
        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars]
        return MCPContextItem(
            resource=self._resource_for_path(path),
            text=text,
            truncated=truncated,
        )

    def build_context(self, uris: Iterable[str], *, max_chars: int = 20_000) -> MCPContextBundle:
        remaining = max_chars
        items: list[MCPContextItem] = []
        for uri in uris:
            if remaining <= 0:
                break
            item = self.read_resource(uri, max_chars=remaining)
            items.append(item)
            remaining -= len(item.text)
        return MCPContextBundle(items=tuple(items))

    def _resource_for_path(self, path: Path) -> MCPResource:
        relative = path.resolve().relative_to(self.root)
        return MCPResource(
            uri=f"local://{relative.as_posix()}",
            name=relative.as_posix(),
            mime_type=_mime_type_for_path(path),
        )

    def _path_from_uri(self, uri: str) -> Path:
        if not uri.startswith("local://"):
            raise MCPAccessError("Only local:// resources are supported by this client")
        relative = uri.removeprefix("local://").strip("/")
        path = (self.root / relative).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise MCPAccessError("Requested resource escapes the configured root") from exc
        if not path.is_file():
            raise MCPAccessError(f"Resource does not exist or is not a file: {uri}")
        return path


def _mime_type_for_path(path: Path) -> str:
    if path.suffix.lower() in {".md", ".markdown"}:
        return "text/markdown"
    if path.suffix.lower() == ".json":
        return "application/json"
    if path.suffix.lower() in {".yaml", ".yml"}:
        return "application/yaml"
    return "text/plain"


def _jsonrpc_result(message: dict[str, object]) -> object:
    error = message.get("error")
    if error is not None:
        raise MCPRemoteError(f"MCP server returned JSON-RPC error: {error}")
    if "result" not in message:
        raise MCPTransportError("MCP JSON-RPC response did not include result")
    return message["result"]


def _parse_sse_jsonrpc_messages(payload: str) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []
    event_data: list[str] = []
    for raw_line in payload.splitlines():
        line = raw_line.rstrip("\r")
        if not line:
            if event_data:
                messages.append(_parse_json_object("\n".join(event_data)))
                event_data = []
            continue
        if line.startswith("data:"):
            event_data.append(line.removeprefix("data:").strip())
    if event_data:
        messages.append(_parse_json_object("\n".join(event_data)))
    return messages


def _parse_json_object(payload: str) -> dict[str, object]:
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise MCPTransportError("MCP SSE data event contained invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise MCPTransportError("MCP SSE data event was not a JSON object")
    return parsed


def _object_list_from_result(result: object, key: str) -> list[dict[str, object]]:
    return _object_list_from_mapping(_dict_or_empty(result), key)


def _object_list_from_mapping(mapping: dict[str, object], key: str) -> list[dict[str, object]]:
    value = mapping.get(key)
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _dict_or_empty(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _extract_mcp_text(result: dict[str, object]) -> str:
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            texts.append(str(block["text"]))
    return "\n".join(texts)
