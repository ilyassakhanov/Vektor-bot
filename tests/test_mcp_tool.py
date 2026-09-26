"""Tests for McpTool — the registry-facing Tool adapter for an MCP client."""

from __future__ import annotations

from llm.base import ToolSpec
from tests.fakes import FakeMcpClient
from tools.mcp import McpTool


def _spec(
    name: str = "get_latest_cve",
    description: str = "Get the most critical recent CVE.",
    parameters: dict[str, object] | None = None,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        parameters=parameters
        if parameters is not None
        else {"type": "object", "properties": {}, "required": []},
    )


def test_mcp_tool_name_from_client_spec() -> None:
    tool = McpTool(
        client=FakeMcpClient(specs=[_spec(name="get_latest_cve")]),
        spec=_spec(name="get_latest_cve"),
    )
    assert tool.name == "get_latest_cve"


def test_mcp_tool_description_from_client_spec() -> None:
    tool = McpTool(
        client=FakeMcpClient(
            specs=[_spec(description="Get the most critical recent CVE.")]
        ),
        spec=_spec(description="Get the most critical recent CVE."),
    )
    assert tool.description == "Get the most critical recent CVE."


def test_mcp_tool_parameters_from_client_spec() -> None:
    params: dict[str, object] = {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
    }
    tool = McpTool(
        client=FakeMcpClient(specs=[_spec(parameters=params)]),
        spec=_spec(parameters=params),
    )
    assert tool.parameters == params


def test_mcp_tool_execute_returns_call_result() -> None:
    client = FakeMcpClient(specs=[_spec()], call_result="fact sheet text")
    tool = McpTool(client=client, spec=_spec())
    assert tool.execute() == "fact sheet text"


def test_mcp_tool_execute_maps_exception_to_error_string() -> None:
    client = FakeMcpClient(specs=[_spec()], call_error=RuntimeError("boom"))
    tool = McpTool(client=client, spec=_spec())
    result = tool.execute()
    assert result == "Error: boom"


def test_mcp_tool_execute_passes_kwargs_to_call_tool() -> None:
    client = FakeMcpClient(specs=[_spec()], call_result="ok")
    tool = McpTool(client=client, spec=_spec())
    tool.execute(foo="bar")
    assert client.calls == [("get_latest_cve", {"foo": "bar"})]
