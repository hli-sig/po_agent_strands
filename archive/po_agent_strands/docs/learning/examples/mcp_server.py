"""A minimal MCP server for lesson 8 (stdio transport). Run by 08_mcp_local.py."""

from mcp.server.mcpserver import MCPServer

server = MCPServer("pm-helpers")


@server.tool()
def fibonacci_round(estimate: float) -> int:
    """Round an effort estimate up to the next Fibonacci story point."""
    for points in (1, 2, 3, 5, 8, 13, 21):
        if estimate <= points:
            return points
    return 21


if __name__ == "__main__":
    server.run()
