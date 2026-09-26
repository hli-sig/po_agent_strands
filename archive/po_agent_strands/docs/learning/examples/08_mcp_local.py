"""Lesson 8 — MCP: tools from another process.

Starts a tiny local MCP server (mcp_server.py, stdio transport), discovers its
tools with strands.tools.mcp.MCPClient and hands them to an agent. The
explicit start/list/stop lifecycle is the one pmagent/tools/mcp_tools.py uses
for Lucid. Discovered tools declare nothing about being reads or writes — which
is why this repo's gate treats unknown tools as writes.

    uv run docs/learning/examples/08_mcp_local.py [--live]
"""

import sys
from pathlib import Path

from _common import banner, model
from mcp import StdioServerParameters, stdio_client

from strands import Agent
from strands.tools.mcp import MCPClient
from tests.fakes import call, text

banner("08 MCP (local stdio server)")

server = Path(__file__).with_name("mcp_server.py")
client = MCPClient(lambda: stdio_client(StdioServerParameters(command=sys.executable, args=[str(server)])))

client.start()
try:
    tools = client.list_tools_sync()
    print("discovered:", [t.tool_name for t in tools])
    agent = Agent(
        model=model([[call("fibonacci_round", estimate=6)], [text("6 rounds up to 8 points.")]]),
        tools=list(tools),
        callback_handler=None,
    )
    print("answer:", str(agent("Round an estimate of 6 to a Fibonacci story point.")).strip())
    for message in agent.messages:
        for block in message["content"]:
            if "toolResult" in block:  # this came back from the server process
                print("tool result from the MCP server:", block["toolResult"]["content"])
finally:
    client.stop(None, None, None)
