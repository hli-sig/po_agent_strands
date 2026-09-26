"""PMAssistant end to end: routing, shared history, the gate on every lane, and
abandoned approvals. Real Strands agents; only the model (and, where it would
need one, the router) is scripted.
"""

from __future__ import annotations

import pytest
from strands import tool
from strands.types.exceptions import EventLoopException

from pmagent.agents.router import ROUTE_TO_LANE
from pmagent.assistant import ABANDONED_MESSAGE, PMAssistant
from pmagent.gate import REJECTION_MESSAGE, WRITE_TOOL_NAMES
from pmagent.messages import tool_results
from pmagent.schemas import PRD, ReviewResult
from pmagent.tools.mcp_tools import MCPSession
from tests.fakes import ScriptedModel, call, structured, text

LANE_TO_ROUTE = {lane: route for route, lane in ROUTE_TO_LANE.items()}

ran: list[str] = []


@tool(name="lucid_create_diagram")
def fake_lucid(description: str) -> str:
    """Stands in for a Lucid MCP tool."""
    ran.append("lucid_create_diagram")
    return "created"


class _FakeClient:
    def __init__(self):
        self.stopped = False

    def stop(self, *args):
        self.stopped = True


def fixed_route(route):
    return lambda messages, previous: route


def make(turns, route="query", **kwargs):
    model = ScriptedModel(turns)
    return PMAssistant(model=model, classify=fixed_route(route), **kwargs), model


@pytest.fixture(autouse=True)
def _reset():
    ran.clear()


def all_results(messages):
    return [r for m in messages for r in tool_results(m)]


# -- the gate holds on every lane -------------------------------------------------


def _a_write_tool(assistant, lane):
    names = {t.tool_name for t in assistant.lanes[lane][1]}
    writes = sorted(names & WRITE_TOOL_NAMES)
    return writes[0] if writes else None


@pytest.mark.parametrize("lane", ["ticket_agent", "sprint_agent", "spreadsheet_agent", "finance_agent"])
def test_a_write_pauses_in_every_writing_lane(lane):
    probe = PMAssistant(model=ScriptedModel())
    write = _a_write_tool(probe, lane)
    assert write, f"{lane} has no write tool?"

    assistant, _ = make([[call(write)]], route=LANE_TO_ROUTE[lane])
    result = assistant.send("do the write")

    assert [c["name"] for c in result.pending] == [write]
    assert assistant.pending


def test_the_query_lane_cannot_even_be_asked_to_write():
    # Shared history can make the read-only lane "remember" a write tool. The
    # agent doesn't have it, so no approval prompt — just an unknown-tool error.
    assistant, _ = make([[call("create_jira_issues", drafts=[])], [text("I can't.")]], route="query")
    result = assistant.send("create it")
    assert result.pending == []
    assert "Unknown tool" in all_results(assistant.messages)[-1]["content"][0]["text"]


def test_an_mcp_tool_in_the_diagram_lane_is_gated_and_mcp_is_closed_on_exit():
    client = _FakeClient()
    assistant, _ = make(
        [[call("lucid_create_diagram", description="d")]],
        route="diagram",
        mcp=MCPSession(client=client, tools=[fake_lucid]),
    )
    result = assistant.send("draw it")
    assert [c["name"] for c in result.pending] == ["lucid_create_diagram"]
    assert ran == []
    assistant.close()
    assert client.stopped


def test_without_lucid_the_diagram_lane_is_told_so():
    assistant = PMAssistant(model=ScriptedModel())
    prompt, tools = assistant.lanes["diagram_agent"]
    assert [t.tool_name for t in tools] == ["draft_diagram_brief"]
    assert "Lucid is **not connected**" in prompt


# -- approve / reject ---------------------------------------------------------------


def test_approve_resumes_the_same_lane_and_finishes_the_turn(monkeypatch):
    import pmagent.tools.jira.tools_write as tw

    monkeypatch.setattr(tw, "get_client", lambda: _Jira())
    assistant, _ = make(
        [[call("add_jira_comment", issue_keys=["CSCI-1"], comment="hi")], [text("Posted.")]],
        route="ticket",
    )
    first = assistant.send("comment hi on CSCI-1")
    assert first.pending
    done = assistant.resume(approved=True)

    assert done.pending == [] and not assistant.pending
    assert _Jira.comments == [(["CSCI-1"], "hi")]


def test_reject_writes_nothing_and_the_agent_answers_the_refusal(monkeypatch):
    import pmagent.tools.jira.tools_write as tw

    monkeypatch.setattr(tw, "get_client", lambda: _Jira())
    assistant, model = make(
        [[call("add_jira_comment", issue_keys=["CSCI-1"], comment="hi")], [text("OK, what should change?")]],
        route="ticket",
    )
    assistant.send("comment hi on CSCI-1")
    assistant.resume(approved=False)

    assert _Jira.comments == []
    assert all_results(assistant.messages)[-1]["content"][0]["text"] == REJECTION_MESSAGE
    assert assistant.messages[-1]["content"][0]["text"] == "OK, what should change?"


class _Jira:
    """Fake Jira client; records comments on the class so every instance shares them."""

    comments: list = []

    def add_comments(self, keys, comment):
        _Jira.comments.append((keys, comment))
        return [{"key": k, "error": None} for k in keys]


@pytest.fixture(autouse=True)
def _reset_jira():
    _Jira.comments = []


# -- one shared conversation ------------------------------------------------------


def test_lanes_share_one_conversation():
    routes = iter(["query", "ticket"])
    model = ScriptedModel([[text("CSCI-5 is open.")], [text("Drafting for CSCI-5.")]])
    assistant = PMAssistant(model=model, classify=lambda m, p: next(routes))

    assistant.send("is CSCI-5 open?")
    assistant.send("draft a follow-up for it")

    # The ticket lane's model call saw the query lane's exchange.
    seen = model.requests[1]["messages"]
    assert any("CSCI-5 is open." in str(m) for m in seen)
    # And the two lanes really are different agents with different prompts.
    assert model.requests[0]["system_prompt"] != model.requests[1]["system_prompt"]


def test_the_router_sees_the_new_message_and_the_previous_route():
    seen = []

    def spy(messages, previous):
        seen.append((messages[-1]["content"][0]["text"], previous))
        return "query"

    assistant = PMAssistant(model=ScriptedModel([[text("a")], [text("b")]]), classify=spy)
    assistant.send("first")
    assistant.send("second")
    assert seen == [("first", ""), ("second", "query")]


def test_reset_starts_a_fresh_conversation():
    assistant, _ = make([[text("hi")]])
    assistant.send("hello")
    assistant.reset()
    assert assistant.messages == [] and assistant.route == ""


def test_the_route_is_announced_before_the_lane_runs():
    order = []
    model = ScriptedModel([lambda messages: order.append("model") or [text("x")]])
    assistant = PMAssistant(model=model, classify=fixed_route("sprint"),
                            on_route=lambda r: order.append(f"route:{r}"))
    assistant.send("sprint 31?")
    assert order == ["route:sprint", "model"]


# -- the requirements lane --------------------------------------------------------


def test_the_requirements_reply_is_returned_for_printing_and_kept_in_history():
    assistant, _ = make(
        [[structured(PRD, title="FX feed", objective="Load FX.")],
         [structured(ReviewResult, approved=True)]],
        route="requirements",
    )
    result = assistant.send("notes: we need FX rates")

    assert result.reply.startswith("# FX feed")
    assert assistant.last_prd.title == "FX feed"
    assert assistant.messages[-2]["content"][0]["text"] == "notes: we need FX rates"
    assert assistant.messages[-1]["content"][0]["text"] == result.reply


# -- abandoned approvals (review findings #2 and #4) ------------------------------


def test_a_new_message_abandons_a_pending_approval_and_the_lane_still_works():
    assistant, model = make(
        [[call("add_jira_comment", issue_keys=["CSCI-1"], comment="hi")],
         [text("Fine, something else then.")]],
        route="ticket",
    )
    assistant.send("comment on CSCI-1")
    assert assistant.pending

    result = assistant.send("actually, never mind")  # same lane, new input

    assert result.pending == [] and not assistant.pending
    abandoned = [r for r in all_results(assistant.messages) if r["content"][0]["text"] == ABANDONED_MESSAGE]
    assert len(abandoned) == 1 and abandoned[0]["status"] == "error"
    assert _Jira.comments == []


def test_discard_is_idempotent():
    assistant, _ = make([[call("add_jira_comment", issue_keys=["CSCI-1"], comment="hi")]], route="ticket")
    assistant.send("comment")
    assistant.discard_pending()
    assistant.discard_pending()
    before = len(assistant.messages)
    assistant.discard_pending()
    assert len(assistant.messages) == before
    assert sum(len(tool_results(m)) for m in assistant.messages) == 1


def test_a_resume_that_fails_after_the_tool_ran_adds_no_duplicate_result(monkeypatch):
    import pmagent.tools.jira.tools_write as tw

    monkeypatch.setattr(tw, "get_client", lambda: _Jira())

    def model_breaks(messages):
        raise RuntimeError("model outage")

    assistant, _ = make(
        [[call("add_jira_comment", issue_keys=["CSCI-1"], comment="hi")], model_breaks],
        route="ticket",
    )
    assistant.send("comment")
    # Strands wraps a model failure in EventLoopException.
    with pytest.raises(EventLoopException, match="model outage"):
        assistant.resume(approved=True)  # tool ran, then the next model call failed

    assert assistant.pending  # still pending, so the caller can clean up
    assistant.discard_pending()

    results = all_results(assistant.messages)
    assert len(results) == 1 and results[0]["status"] == "success"  # no fake "abandoned" twin
    assert _Jira.comments == [(["CSCI-1"], "hi")]


def test_resume_without_a_pending_approval_is_an_error():
    assistant, _ = make([])
    with pytest.raises(RuntimeError):
        assistant.resume(True)


def test_abandoning_on_one_lane_and_continuing_on_another():
    """Review finding: the ticket lane pauses, the user moves on to a question.
    The pending call gets exactly one explicit result (not Strands' generic
    placeholder), and the ticket lane accepts plain text again afterwards."""
    routes = iter(["ticket", "query", "ticket"])
    model = ScriptedModel([
        [call("add_jira_comment", issue_keys=["CSCI-1"], comment="hi")],
        [text("CSCI-1 is in progress.")],
        [text("Back in the ticket lane.")],
    ])
    assistant = PMAssistant(model=model, classify=lambda m, p: next(routes))

    assert assistant.send("comment on CSCI-1").pending
    assert assistant.send("what's CSCI-1's status?").pending == []
    assert assistant.send("ok, draft something else").pending == []

    results = all_results(assistant.messages)
    assert [r["content"][0]["text"] for r in results] == [ABANDONED_MESSAGE]
    assert _Jira.comments == []
    assert assistant.messages[-1]["content"][0]["text"] == "Back in the ticket lane."


def test_the_diagram_brief_uses_the_calling_agents_model():
    """draft_diagram_brief reaches the model through ToolContext, so a scripted
    model drives it too — no real provider needed."""
    from pmagent.schemas import DiagramBrief

    assistant, model = make(
        [
            [call("draft_diagram_brief", prd_text="FX feed: source -> Snowflake")],
            [structured(DiagramBrief, diagram_type="flowchart", title="FX feed",
                        description="Source feeds Snowflake.",
                        nodes=[{"id": "a", "label": "Source"}, {"id": "b", "label": "Snowflake"}],
                        edges=[{"from": "a", "to": "b", "label": "loads"}])],
            [text("Here is the brief.")],
        ],
        route="diagram",
    )
    assert assistant.send("draw the FX feed").pending == []  # a read: not gated
    brief = all_results(assistant.messages)[-1]["content"][0]["text"]
    assert "Diagram type: flowchart" in brief and "- a -> b (loads)" in brief
    assert model.remaining == 0
