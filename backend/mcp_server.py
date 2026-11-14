"""The same travel tools, served over MCP so Claude Code or any MCP client can use them.

    claude mcp add tradewinds -- python backend/mcp_server.py
"""
from mcp.server.mcpserver import MCPServer  # mcp>=2 (was FastMCP in 1.x)

import tools

mcp = MCPServer("tradewinds")


@mcp.tool()
async def geocode(place: str) -> dict:
    """Look up latitude/longitude for a town or region name."""
    return await tools.run_tool("geocode", {"place": place})


@mcp.tool()
async def climate_for_window(lat: float, lon: float, start_date: str, end_date: str) -> dict:
    """Typical weather (highs, lows, rain days, sunshine) for a location over a date window."""
    return await tools.run_tool("climate_for_window",
                                {"lat": lat, "lon": lon, "start_date": start_date, "end_date": end_date})


@mcp.tool()
async def ocean_conditions(lat: float, lon: float) -> dict:
    """Live 7-day wave outlook for a coastal point."""
    return await tools.run_tool("ocean_conditions", {"lat": lat, "lon": lon})


if __name__ == "__main__":
    mcp.run()  # stdio transport
