"""Lesson 7 — the PRD writer/reviewer loop as a Strands Graph.

pmagent/agents/requirements.py runs this loop in plain Python. Here the same
shape is handed to strands.multiagent.GraphBuilder: nodes are Agents, edges
can carry conditions, cycles are allowed, and set_max_node_executions is the
safety net. Compare the two and decide which you find clearer.

    uv run docs/learning/examples/07_graph_reflection.py [--live]
"""

from _common import banner, model
from pydantic import BaseModel

from strands import Agent
from strands.multiagent import GraphBuilder
from tests.fakes import structured, text


class Verdict(BaseModel):
    approved: bool
    feedback: str = ""


banner("07 graph reflection loop")

# Two agents, so two scripts: the writer answers in text, the reviewer returns
# a Verdict (structured output). Live, both use the real model.
writer_model = model([[text("PRD v1: daily FX feed.")], [text("PRD v2: daily FX feed by 7am.")]])
reviewer_model = model([
    [structured(Verdict, approved=False, feedback="Missing the 7am SLA.")],
    [structured(Verdict, approved=True)],
])

writer = Agent(model=writer_model, name="writer", callback_handler=None,
               system_prompt="Write a short PRD from the notes. On revision, apply the reviewer feedback.")
reviewer = Agent(model=reviewer_model, name="reviewer", callback_handler=None,
                 system_prompt="Review the PRD against the notes.", structured_output_model=Verdict)


def verdict(state):
    node = state.results.get("reviewer")
    return node.result.structured_output if node else None


def needs_revision(state):
    v = verdict(state)
    return v is not None and not v.approved


builder = GraphBuilder()
builder.add_node(writer, "writer")
builder.add_node(reviewer, "reviewer")
builder.add_edge("writer", "reviewer")
builder.add_edge("reviewer", "writer", condition=needs_revision)  # the cycle
builder.set_entry_point("writer")
builder.set_max_node_executions(6)   # safety net, like max_iterations
builder.reset_on_revisit(False)      # keep each agent's memory across passes

graph = builder.build()
result = graph("Notes: Finance wants daily FX rates in Snowflake by 7am AEST.")

print("status        :", result.status)
print("execution path:", " -> ".join(n.node_id for n in result.execution_order))
print("final verdict :", verdict(graph.state))
