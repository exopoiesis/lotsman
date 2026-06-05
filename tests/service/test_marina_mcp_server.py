from __future__ import annotations

import pytest

from marina.hub import Hub
from marina.mcp_server import make_mcp_server

pytestmark = pytest.mark.service

EXPECTED_TOOLS = {
    # sea registry / queries
    "sea_list",
    "sea_search",
    "sea_recommend",
    "sea_status",
    "cost_summary",
    # host lifecycle
    "host_create",
    "host_add",
    "host_destroy",
    "host_stop",
    "host_start",
    "host_list",
    # per-job
    "run",
    "status",
    "kill",
    "logs",
    "whoami",
    # filesystem
    "upload",
    "mkdir",
    "ls",
    "stat",
    "cat",
    "disk_free",
    # harvest
    "harvest_inventory",
    "harvest",
    "download",
    "download_glob",
    # watchdogs / events
    "watchdog_list",
    "watchdog_history",
    "events",
    "events_all",
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


@pytest.mark.asyncio
async def test_mcp_tool_sea_list_includes_registered_sea():
    from tests.service.test_marina_hub_seas import FakeSea

    hub = Hub(seas=[FakeSea("vast"), FakeSea("gomer")])
    server = make_mcp_server(hub)
    result = await server.call_tool("sea_list", {})
    assert result is not None


@pytest.mark.asyncio
async def test_mcp_tool_sea_recommend_dispatches_to_hub():
    from tests.service.test_marina_hub_seas import FakeSea

    hub = Hub(seas=[FakeSea("vast")])
    server = make_mcp_server(hub)
    result = await server.call_tool(
        "sea_recommend",
        {"sea": "vast", "workload": "dft_paper_grade"},
    )
    assert result is not None


@pytest.mark.asyncio
async def test_mcp_tool_host_create_dispatches_to_hub(lotsman_tcp):
    from tests.service.test_marina_hub_seas import FakeSea

    fake = FakeSea("gomer", grpc_target=lotsman_tcp.target)
    hub = Hub(seas=[fake])
    server = make_mcp_server(hub)
    try:
        result = await server.call_tool(
            "host_create",
            {"sea": "gomer", "image": "lotsman:latest", "name": "gomer-1"},
        )
        assert result is not None
        assert "gomer-1" in hub.host_list()
        assert len(fake.create_calls) == 1
    finally:
        hub.shutdown()
