"""The approval gate, end to end through a real Strands Agent.

Only the model is scripted (tests/fakes.py). The agent loop, the hook, the
interrupt and the resume are all real Strands, so these tests prove the property
that matters: a write tool's body does not run until a human approves it, and
never runs if they reject it.
"""

from __future__ import annotations

import pytest
from strands import Agent, tool

from pmagent.gate import (
    APPROVE,
    READ_TOOL_NAMES,
    REJECT,
    REJECTION_MESSAGE,
    WRITE_TOOL_NAMES,
    ApprovalGate,
    requires_approval,
)
from tests.fakes import ScriptedModel, call, text

executed: list[str] = []


@tool(name="query_jira_issues")
def fake_read(jql: str) -> str:
    """Read-only stand-in."""
    executed.append("query_jira_issues")
    return "CSCI-1 Done"


@tool(name="create_jira_issues")
def fake_write(drafts: list[dict]) -> str:
    """Write stand-in."""
    executed.append("create_jira_issues")
    return "Created CSCI-99."


@tool(name="lucid_create_diagram")
def fake_mcp_tool(description: str) -> str:
    """Stands in for a tool discovered from an MCP server: declared nowhere."""
    executed.append("lucid_create_diagram")
    return "diagram created"


@pytest.fixture(autouse=True)
def _reset():
    executed.clear()


def make_agent(turns, tools=(fake_read, fake_write, fake_mcp_tool)):
    model = ScriptedModel(turns)
    agent = Agent(model=model, tools=list(tools), hooks=[ApprovalGate()], callback_handler=None)
    return agent, model


def respond(result, answer):
    return [{"interruptResponse": {"interruptId": i.id, "response": answer}} for i in result.interrupts]


def tool_results(agent) -> list[dict]:
    return [
        block["toolResult"]
        for message in agent.messages
        for block in message["content"]
        if "toolResult" in block
    ]


def test_the_stand_in_names_are_classified_like_the_real_tools():
    assert "query_jira_issues" in READ_TOOL_NAMES
    assert "create_jira_issues" in WRITE_TOOL_NAMES


def test_a_read_only_batch_runs_without_asking():
    agent, _ = make_agent([[call("query_jira_issues", jql="x")], [text("done")]])
    result = agent("search")
    assert result.stop_reason == "end_turn"
    assert executed == ["query_jira_issues"]


def test_a_write_stops_before_the_tool_body_runs():
    agent, _ = make_agent([[call("create_jira_issues", drafts=[{"summary": "s"}])]])
    result = agent("create it")

    assert result.stop_reason == "interrupt"
    assert executed == []  # the whole point
    (interrupt,) = result.interrupts
    assert interrupt.reason["calls"][0]["name"] == "create_jira_issues"
    assert interrupt.reason["calls"][0]["args"] == {"drafts": [{"summary": "s"}]}


def test_approving_runs_the_write_exactly_once():
    agent, _ = make_agent([
        [call("create_jira_issues", drafts=[{"summary": "s"}])],
        [text("Created CSCI-99.")],
    ])
    first = agent("create it")
    final = agent(respond(first, APPROVE))

    assert final.stop_reason == "end_turn"
    assert executed == ["create_jira_issues"]
    assert tool_results(agent)[-1]["status"] == "success"


def test_rejecting_never_runs_the_write_and_the_model_sees_why():
    agent, model = make_agent([
        [call("create_jira_issues", drafts=[{"summary": "s"}])],
        [text("Understood — what should change?")],
    ])
    first = agent("create it")
    final = agent(respond(first, REJECT))

    assert final.stop_reason == "end_turn"
    assert executed == []
    result = tool_results(agent)[-1]
    assert result["status"] == "error"
    assert result["content"][0]["text"] == REJECTION_MESSAGE
    # The model was called again, with the rejection in its context.
    last_seen = model.requests[-1]["messages"][-1]
    assert last_seen["content"][0]["toolResult"]["content"][0]["text"] == REJECTION_MESSAGE


def test_a_read_bundled_with_a_write_is_gated_as_a_batch():
    agent, _ = make_agent([
        [call("query_jira_issues", jql="x"), call("create_jira_issues", drafts=[])],
        [text("ok")],
    ])
    first = agent("search and create")
    assert first.stop_reason == "interrupt"
    assert executed == []  # not even the read ran ahead of the gate

    agent(respond(first, REJECT))
    assert executed == []
    assert all(r["content"][0]["text"] == REJECTION_MESSAGE for r in tool_results(agent))


def test_the_interrupt_lists_only_the_gated_calls():
    agent, _ = make_agent([[call("query_jira_issues", jql="x"), call("create_jira_issues", drafts=[])]])
    first = agent("go")
    assert [c["name"] for c in first.interrupts[0].reason["calls"]] == ["create_jira_issues"]


def test_an_approval_is_not_reused_for_the_next_write():
    agent, _ = make_agent([
        [call("create_jira_issues", drafts=[])],
        [call("create_jira_issues", drafts=[])],
        [text("done")],
    ])
    first = agent("create two")
    second = agent(respond(first, APPROVE))
    assert second.stop_reason == "interrupt"  # asked again
    assert executed == ["create_jira_issues"]


def test_a_registered_but_undeclared_tool_is_gated():
    """An MCP tool nobody reviewed is treated as a write — the gate fails closed."""
    agent, _ = make_agent([[call("lucid_create_diagram", description="d")]])
    result = agent("draw it")
    assert result.stop_reason == "interrupt"
    assert executed == []


def test_a_tool_the_agent_does_not_have_is_not_gated_and_cannot_run():
    """Shared history means a read-only lane may 'remember' a write tool. It cannot
    execute it, so asking a human to approve it would be a meaningless prompt."""
    agent, _ = make_agent(
        [[call("create_jira_issues", drafts=[])], [text("I can't do that here.")]],
        tools=[fake_read],
    )
    result = agent("create it")
    assert result.stop_reason == "end_turn"
    assert executed == []
    error = tool_results(agent)[-1]
    assert error["status"] == "error"
    assert "Unknown tool" in error["content"][0]["text"]


def test_requires_approval_rules():
    agent, _ = make_agent([], tools=[fake_read, fake_write, fake_mcp_tool])
    assert requires_approval("create_jira_issues", agent) is True
    assert requires_approval("lucid_create_diagram", agent) is True
    assert requires_approval("query_jira_issues", agent) is False
    assert requires_approval("not_registered", agent) is False
