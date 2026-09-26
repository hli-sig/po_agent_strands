"""The diagram brief: the model decides what to draw, Python renders it."""

from __future__ import annotations

from strands import Agent

from pmagent.schemas import DiagramBrief, DiagramEdge, DiagramNode
from pmagent.tools import diagram_tools
from tests.fakes import ScriptedModel, call, structured, text

BRIEF = dict(
    diagram_type="flowchart", title="Order flow",
    description="A customer places an order, which the warehouse picks.",
    nodes=[{"id": "c", "label": "Customer"}, {"id": "w", "label": "Warehouse"}],
    edges=[{"from": "c", "to": "w", "label": "places order"}, {"from": "w", "to": "c"}],
)


def test_the_brief_is_rendered_by_python_not_the_model():
    brief = DiagramBrief(diagram_type="architecture", title="T", description="D",
                         nodes=[DiagramNode(id="a", label="API")],
                         edges=[DiagramEdge(**{"from": "a", "to": "b"}), DiagramEdge(from_="b", to="a", label="calls")])
    assert diagram_tools.render_diagram_brief(brief) == (
        "Diagram type: architecture\nTitle: T\n\nD\n\nNodes:\n- a: API\n\nEdges:\n- a -> b\n- b -> a (calls)")


def test_the_prompt_includes_the_reporting_intent_only_when_given():
    assert "Reporting Intent brief:" not in diagram_tools._build_prompt("PRD text")
    prompt = diagram_tools._build_prompt("PRD text", "intent")
    assert prompt.endswith("Reporting Intent brief:\nintent") and "PRD:\nPRD text" in prompt


def test_draft_diagram_brief_uses_the_calling_lanes_model():
    # One scripted model plays both the lane and the structured-output call the
    # tool makes through tool_context.agent.model.
    model = ScriptedModel([
        [call("draft_diagram_brief", prd_text="Customers order; the warehouse picks.")],
        [structured(DiagramBrief, **BRIEF)],
        [text("Here is the brief.")],
    ])
    agent = Agent(model=model, tools=[diagram_tools.draft_diagram_brief], callback_handler=None)
    agent("Draft a diagram brief.")
    result = next(b["toolResult"] for m in agent.messages for b in m["content"] if "toolResult" in b)
    assert result["status"] == "success"
    assert result["content"][0]["text"].startswith("Diagram type: flowchart\nTitle: Order flow")
    assert "- c -> w (places order)" in result["content"][0]["text"]
    assert model.remaining == 0
    assert [t.tool_name for t in diagram_tools.READ_TOOLS] == ["draft_diagram_brief"]
