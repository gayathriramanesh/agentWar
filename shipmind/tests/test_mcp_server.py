import asyncio
import json
import sys
from pathlib import Path

import pytest

mcp = pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

SERVER = Path(__file__).resolve().parent.parent / "mcp_server" / "server.py"


def test_mcp_server_exposes_tools_and_answers():
    async def go():
        params = StdioServerParameters(command=sys.executable, args=[str(SERVER)])
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                names = {t.name for t in (await s.list_tools()).tools}
                res = await s.call_tool("check_inventory", {"sku": "SKU-ECHO"})
                return names, json.loads(res.content[0].text)

    names, inv = asyncio.run(go())
    assert {"allocate_inventory", "track_shipment", "get_disruptions", "check_inventory", "list_warehouses"} <= names
    assert inv["sku"] == "SKU-ECHO" and inv["total_units"] > 0
