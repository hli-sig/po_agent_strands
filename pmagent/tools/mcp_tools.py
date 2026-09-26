"""
Standard pattern for pulling an external MCP server's tools into a Strands lane.

STRANDS CONCEPT — `strands.tools.mcp.MCPClient` opens a session to an MCP server
(on its own background thread, so everything stays synchronous for us) and
wraps each remote tool as an `MCPAgentTool` — the same `AgentTool` interface a
local `@tool` has, so a lane binds both in one `tools=[...]` list.

LangGraph original: `langchain-mcp-adapters`' `MultiServerMCPClient`, whose
discovery was `async`, which forced the whole graph onto `.ainvoke` — and
because `main.py` never did that, the diagram lane never ran. The Strands client
is sync-friendly, so the lane simply exists.

Lifecycle, made explicit on purpose (the simplest version to reason about):

    session = start_lucid()        # start() + list every tool, or None on failure
    tools   = session.tools        # give THESE to the Agent — not the client
    ...
    session.close()                # stop the background thread at exit

Why not pass the `MCPClient` itself in `tools=[...]`? Strands supports that too
(the Agent then starts and stops the client for you), but a client you started
by hand and *also* pass to an Agent gets started twice and fails with "the
client session is currently running". Pick one owner; here it is the app.

**Gating.** Tools discovered at runtime cannot be listed in any module's
`READ_TOOLS`, so the approval gate treats every one of them as a write unless its
name is in `APPROVED_READ_TOOLS` below — the gate fails *closed* on tools nobody
reviewed. Add a name there only after reading what the tool does.
"""

from __future__ import annotations

from dataclasses import dataclass

from pmagent import env

# Lucid tools a human has reviewed and confirmed are read-only. Empty until then:
# every Lucid tool (search, create_diagram, share, ...) goes through the gate.
APPROVED_READ_TOOLS: frozenset[str] = frozenset()

@dataclass
class MCPSession:
    """A started MCP client plus the tools it exposes."""

    client: object
    tools: list

    def close(self) -> None:
        # `stop` has no default arguments — it is the context manager's __exit__.
        self.client.stop(None, None, None)


def _list_all_tools(client) -> list:
    """list_tools_sync` is paginated; follow the token to the end."""
    tools: list = []
    token = None
    while True:
        