"""
Local (non-MCP) half of the Diagram Agent's toolset.

`draft_diagram_brief` never talks to Lucid — it only turns an approved PRD
(and, optionally, its Reporting Intent brief) into a `DiagramBrief`: the
hand-off artifact the agent later passes as the `description` argument to
Lucid's MCP `create_diagram` tool, once a human has confirmed it. Keeping the
Lucid call out of this tool mirrors `snowflake/procs/draft_diagram_brief.py`
exactly, just with Strands structured output (`llm.structured`) in place of
prompted JSON + manual parsing (Cortex has no structured-output binding;
Strands does).

Split of responsibilities: the LLM does judgment (what to diagram, how nodes
relate); `render_diagram_brief` is plain Python formatting. Never let the
model hand-format the text a human will read.
"""

from __future__ import annotations

from strands import ToolContext, tool

from pmagent.llm import structured
from pmagent.schemas import DiagramBrief

_DIAGRAM_BRIEF_ROLE = """
You are the Diagram Brief Agent. Given an approved PRD (and, if provided, its
Reporting Intent brief), decide what single diagram would best communicate
the design, and describe it precisely enough that a diagramming tool could
draw it without seeing the source documents.

Pick exactly one diagram_type:
- "flowchart" - a process or user flow.
- "entity_relationship" - data entities and how they relate.
- "architecture" - systems/services and how they connect.
- "sequence" - a time-ordered interaction between actors/systems.

Rules:
- Every node must come from something explicitly stated in the source text -
  do not invent systems, steps, or entities that aren't there.
- Every edge needs a label if the relationship isn't obvious from the node
  names alone (e.g. "writes to", "triggers", "1-to-many").
- The description field is the most important output: a few plain-English
  sentences naming every node and how they connect, written so someone with
  no other context could hand it to a diagramming tool and get the right
  picture. Do not reference "the PRD" or "the source" inside it.
"""


def _build_prompt(prd_text: str, reporting_intent_text: str = "") -> str:
    parts = [_DIAGRAM_BRIEF_ROLE, f"PRD:\n{prd_text}"]
    if reporting_intent_text:
        parts.append(f"Reporting Intent brief:\n{reporting_intent_text}")
    return "\n\n".join(parts)


def render_diagram_brief(brief: DiagramBrief) -> str:
    """Deterministically format a `DiagramBrief` for display and for handing
    to Lucid's `create_diagram` tool as its `description` argument."""
    lines = [
        f"Diagram type: {brief.diagram_type}",
        f"Title: {brief.title}",
        "",
        brief.description,
        "",
        "Nodes:",
    ]
    lines += [f"- {n.id}: {n.label}" for n in brief.nodes]

    lines += ["", "Edges:"]
    for e in brief.edges:
        label = f" ({e.label})" if e.label else ""
        lines.append(f"- {e.from_} -> {e.to}{label}")

    return "\n".join(lines)


@tool(context=True)
def draft_diagram_brief(
    prd_text: str, reporting_intent_text: str = "", tool_context: ToolContext | None = None
) -> str:
    """Turn an approved PRD (and optional Reporting Intent brief) into a
    plain-language diagram brief ready to hand to Lucid's create_diagram MCP
    tool. Does not create or modify anything in Lucid.

    Args:
        prd_text: The approved PRD text.
        reporting_intent_text: Optional Reporting Intent brief text.
    """
    # STRANDS CONCEPT — ToolContext. `@tool(context=True)` makes Strands pass a
    # `tool_context` (hidden from the model's schema) carrying the calling agent,
    # the tool_use and the invocation state. Here it lets the brief use the SAME
    # model as the lane that called it (so a test's scripted model, or the web
    # server's per-conversation model, reaches this call too), and report its
    # token usage to whoever passed `invocation_state={"usage_sink": ...}`.
    model = tool_context.agent.model if tool_context else None
    state = tool_context.invocation_state if tool_context else {}
    brief = structured(
        DiagramBrief,
        _build_prompt(prd_text, reporting_intent_text),
        model=model,
        usage_sink=state.get("usage_sink"),
    )
    return render_diagram_brief(brief)


# `draft_diagram_brief` calls an LLM and renders text; it creates nothing in
# Lucid and touches no file, so it is a read. The Lucid MCP tools the diagram
# lane also binds are discovered at runtime from someone else's server and
# cannot be declared here — see `tools/mcp_tools.py`.
READ_TOOLS = [draft_diagram_brief]
WRITE_TOOLS = []
