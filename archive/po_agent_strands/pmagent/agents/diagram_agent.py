"""
Diagram Agent — turns an approved PRD into a diagram brief, and (optionally)
draws it in Lucid through Lucid's MCP server.

Same shape as the other lanes, with one difference: part of its toolset is
discovered at runtime from an MCP server (`pmagent/tools/mcp_tools.py`), so the
tool list is built by a function, not a constant.

    LOCAL_TOOLS                 always: draft_diagram_brief (an LLM call; creates nothing)
    + Lucid MCP tools           only when LUCID_MCP_ENABLED and the server is reachable

Every Lucid tool is gated unless reviewed into `mcp_tools.APPROVED_READ_TOOLS`.

LangGraph original: discovery was async, so this lane only existed in
`build_graph_async()`, which nothing called — diagram requests fell back to the
query lane. With Strands' synchronous MCPClient the lane is always present.
"""

from pmagent.prompts import prompts
from pmagent.tools.diagram_tools import draft_diagram_brief

LOCAL_TOOLS = [draft_diagram_brief]
SYSTEM_PROMPT = prompts.diagram_agent_system_prompt


def build_tools(mcp_tools: list | None = None) -> list:
    """This lane's full toolset: local tools + any discovered MCP tools."""
    return [*LOCAL_TOOLS, *(mcp_tools or [])]


_NO_LUCID_NOTE = """

## This session

Lucid is **not connected** (LUCID_MCP_ENABLED is off, or the server was
unreachable at startup). You can draft and refine the brief, but you have no
Lucid tools: do not offer to create the diagram — tell the user the brief is
ready to paste into Lucid, or to enable LUCID_MCP_ENABLED and restart.
"""


def system_prompt(lucid_connected: bool) -> str:
    return SYSTEM_PROMPT if lucid_connected else SYSTEM_PROMPT + _NO_LUCID_NOTE
