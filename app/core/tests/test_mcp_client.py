from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from app.core import (
    LocalDirectoryMCPClient,
    MCPAccessError,
    MCPHTTPConfig,
    MCPServerConfig,
    MCPToolCall,
    RemoteMCPClient,
    SSEMCPTransport,
    StdioMCPTransport,
)


def test_local_directory_mcp_client_lists_reads_and_renders_prompt(tmp_path: Path) -> None:
    (tmp_path / "codex").mkdir()
    note = tmp_path / "codex" / "today.md"
    note.write_text("# Work log\nPhase 6 queue design.", encoding="utf-8")
    (tmp_path / "secret.bin").write_bytes(b"\x00\x01")

    client = LocalDirectoryMCPClient(tmp_path)

    resources = client.list_resources()
    assert [resource.uri for resource in resources] == ["local://codex/today.md"]

    bundle = client.build_context(["local://codex/today.md"], max_chars=12)
    assert bundle.items[0].truncated is True
    assert "local://codex/today.md" in bundle.as_prompt_block()
    assert "# Work log" in bundle.as_prompt_block()


def test_local_directory_mcp_client_blocks_path_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.md"
    outside.write_text("nope", encoding="utf-8")
    client = LocalDirectoryMCPClient(tmp_path)

    with pytest.raises(MCPAccessError):
        client.read_resource("local://../outside.md")


@pytest.mark.asyncio
async def test_remote_mcp_client_calls_stdio_tools_and_reads_resources(tmp_path: Path) -> None:
    server = tmp_path / "fake_mcp_server.py"
    server.write_text(
        """
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "fake"}}
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": "echo_context",
                    "description": "Echo a text value",
                    "inputSchema": {"type": "object"},
                }
            ]
        }
    elif method == "tools/call":
        text = request["params"]["arguments"]["text"]
        result = {"content": [{"type": "text", "text": f"tool said: {text}"}]}
    elif method == "resources/list":
        result = {"resources": [{"uri": "note://today", "name": "today", "mimeType": "text/plain"}]}
    elif method == "resources/read":
        result = {
            "contents": [
                {"uri": request["params"]["uri"], "mimeType": "text/plain", "text": "resource body"}
            ]
        }
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\\n")
    sys.stdout.flush()
""".strip(),
        encoding="utf-8",
    )
    transport = StdioMCPTransport(MCPServerConfig("fake", sys.executable, (str(server),)))
    client = RemoteMCPClient(transport)

    try:
        initialized = await client.initialize()
        tools = await client.list_tools()
        resources = await client.list_resources()
        bundle = await client.build_context(
            resource_uris=["note://today"],
            tool_calls=[MCPToolCall("echo_context", {"text": "hello"})],
        )
    finally:
        await client.close()

    assert isinstance(initialized, dict)
    assert tools[0].name == "echo_context"
    assert resources[0].uri == "note://today"
    prompt_block = bundle.as_prompt_block()
    assert "resource body" in prompt_block
    assert "tool said: hello" in prompt_block


@pytest.mark.asyncio
async def test_sse_mcp_transport_parses_jsonrpc_data_event() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = self.rfile.read(int(self.headers["content-length"]))
            request = json.loads(body)
            response = {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "tools": [
                        {
                            "name": "remote_search",
                            "description": "Search remotely",
                            "inputSchema": {"type": "object"},
                        }
                    ]
                },
            }
            payload = f"event: message\ndata: {json.dumps(response)}\n\n".encode()
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return None

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/mcp"
        client = RemoteMCPClient(SSEMCPTransport(MCPHTTPConfig("fake-http", url)))
        tools = await client.list_tools()
    finally:
        httpd.shutdown()
        thread.join(timeout=5)

    assert tools[0].name == "remote_search"
