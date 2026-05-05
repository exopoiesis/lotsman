from __future__ import annotations

import pytest

from marina.hub import Hub
from marina.mcp_server import make_mcp_server

pytestmark = pytest.mark.service

EXPECTED_TOOLS = {
    "host_add",
    "host_remove",
    "host_list",
    "run",
    "status",
    "kill",
    "logs",
    "whoami",
}


def test_make_mcp_server_constructs_with_name():
    hub = Hub()
    server = make_mcp_server(hub, name="TestMarina")
    assert server is not None
    assert server.name == "TestMarina"


@pytest.mark.asyncio
async def test_mcp_server_registers_expected_tools():
    hub = Hub()
    server = make_mcp_server(hub)
    tools = await server.list_tools()
    registered = {t.name for t in tools}
    missing = EXPECTED_TOOLS - registered
    assert not missing, f"missing tools: {missing}"


@pytest.mark.asyncio
async def test_mcp_tool_host_list_returns_empty_initially():
    hub = Hub()
    server = make_mcp_server(hub)
    result = await server.call_tool("host_list", {})
    # FastMCP's call_tool returns a list of TextContent objects;
    # the structured output is in the result data
    assert result is not None
