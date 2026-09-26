"""Lesson 2 — tools.

`@tool` turns a typed, documented function into something the model can call.
The docstring IS the prompt: summary + body become the description, `Args:`
become the input schema. Watch the gotcha: prose placed AFTER `Args:` is
dropped (tests/test_tool_specs.py guards this repo against it).

    uv run docs/learning/examples/02_tools.py [--live]
"""

import json

from _common import banner, model

from strands import Agent, tool
from tests.fakes import call, text

banner("02 tools")


@tool
def story_points_total(points: list[float]) -> float:
    """Add up story points. Deterministic arithmetic — never do this in the model.

    Args:
        points: The story point values to add.

    This sentence comes after Args, so Strands drops it from the spec.
    """
    return sum(points)


print("What the model sees (tool_spec):")
print(json.dumps(story_points_total.tool_spec, indent=2))
kept = "comes after Args" in story_points_total.tool_spec["description"]
print("\nThe 'This sentence comes after Args' line is",
      "in the description." if kept else "MISSING from the description — Strands dropped it.\n")

# Tools stay plain functions — call them directly in tests. (The model gets the
# JSON-serialised result, so the float sum 16.0 arrives as the text "16.0".)
print("direct call:", story_points_total([3, 5, 8]), "  (a float; the model receives it serialised as the text '16.0')")

agent = Agent(
    model=model([
        [call("story_points_total", points=[3, 5, 8])],
        [text("That's 16 points.")],
    ]),
    tools=[story_points_total],
    callback_handler=None,
)
result = agent("What's the total of 3, 5 and 8 story points? Use the tool.")
print("answer:", str(result).strip())
for m in agent.messages:
    for block in m["content"]:
        if "toolUse" in block:
            print("  model asked :", block["toolUse"]["name"], block["toolUse"]["input"])
        if "toolResult" in block:
            print("  tool replied:", block["toolResult"]["content"])
