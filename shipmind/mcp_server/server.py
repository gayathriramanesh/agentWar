"""ShipMind MCP server (stdio). Exposes the warehouse/inventory/tracking system to any MCP client.

Run standalone:  python mcp_server/server.py
Inspect:         npx @modelcontextprotocol/inspector python mcp_server/server.py
ADK connects to it automatically via McpToolset (see logistics_agent/agent.py).
NOTE: never print() to stdout here; stdout carries the MCP protocol.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server  # noqa: E402
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # noqa: E402

from core import logistics  # noqa: E402

mcp = _Server("shipmind-logistics")

for _fn in logistics.DATA_TOOLS:
    mcp.tool()(_fn)

if __name__ == "__main__":
    mcp.run()  # stdio transport
