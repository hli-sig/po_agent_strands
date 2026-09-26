"""Lesson 11 — the thinking chain, offline.

A TraceRecorder is a hook provider AND a callback handler. Here it watches a
scripted agent that reasons, calls a tool and answers, and prints the events the
web UI's thinking chain would receive over SSE.

    uv run docs/learning/examples/11_trace_events.py [--live]
"""

import json

from _common import banner, model

from strands import Agent, tool
from pmagent.web.trace import TraceRecorder
from tests.fakes import call, reasoning, text

banner("11 thinking-chain events")


@tool
def sprint_status(sprint: int) -> str:
    """Status of a sprint by its visible number."""
    return f"Sprint {sprint}: 143.5 of 232.5 points done, 61 blocked."


events = []
recorder = TraceRecorder(emit=lambda kind, data: events.append((kind, data)))

agent = Agent(
    model=model([
        [reasoning("The user wants risk; I need the numbers first."), call("sprint_status", sprint=31)],
        [text("Sprint 31 is at risk: 61 points are blocked.")],
    ]),
    tools=[sprint_status],
    hooks=[recorder],           # structured lifecycle: tools, messages
    callback_handler=recorder,  # live deltas: text and reasoning
)
agent("Is sprint 31 at risk?")

source = {"reasoning.delta": "callback", "text.delta": "callback"}
for kind, data in events:
    shown = {k: v for k, v in data.items() if k not in ("ts", "turn_id")}
    print(f"{source.get(kind, 'hook'):>8}  {kind:<18} {json.dumps(shown)[:90]}")
