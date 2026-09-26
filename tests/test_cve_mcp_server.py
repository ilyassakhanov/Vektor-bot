"""Tests for mcp_servers/cve_server — MCP server exposing get_latest_cve.

Uses the real MCPServer.list_tools() (async) via asyncio.run() — no network
access, since the server doesn't call CVE.org unless the tool is invoked.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from mcp_servers.cve_server import build_server

_SERVER_MODULE = (
    Path(__file__).resolve().parent.parent / "mcp_servers" / "cve_server.py"
)

_EXPECTED_DESCRIPTION = (
    "Get the most critical recent CVE (highest CVSS) from official "
    "CVE.org data. Use for latest or most severe CVE questions."
)


def test_server_registers_get_latest_cve_tool() -> None:
    server = build_server()
    tools = asyncio.run(server.list_tools())
    assert len(tools) == 1
    assert tools[0].name == "get_latest_cve"


def test_server_tool_description_matches_cve_tool() -> None:
    server = build_server()
    tools = asyncio.run(server.list_tools())
    assert len(tools) == 1
    assert tools[0].description == _EXPECTED_DESCRIPTION


def test_server_tool_input_schema_is_empty_object() -> None:
    server = build_server()
    tools = asyncio.run(server.list_tools())
    assert len(tools) == 1
    schema = tools[0].input_schema
    assert schema["type"] == "object"
    assert schema.get("required", []) == []


def test_server_module_logs_to_stderr_not_stdout() -> None:
    source = _SERVER_MODULE.read_text(encoding="utf-8")
    assert "print(" not in source
    assert "logging.StreamHandler(sys.stderr)" in source or (
        "StreamHandler(sys.stderr)" in source
    )
