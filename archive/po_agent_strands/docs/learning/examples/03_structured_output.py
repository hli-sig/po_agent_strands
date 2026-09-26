"""Lesson 3 — structured output.

`agent(prompt, structured_output_model=Schema)` makes the model return a
validated Pydantic object (result.structured_output). Under the hood Strands
adds a hidden tool named after the schema. This repo wraps it in
`pmagent.llm.structured` and uses it for routing, the PRD writer/reviewer and
the diagram brief.

    uv run docs/learning/examples/03_structured_output.py [--live]
"""

from typing import Literal

from _common import banner, model
from pydantic import BaseModel, Field

from pmagent.llm import structured as structured_call
from pmagent.schemas import RouteDecision
from tests.fakes import structured

banner("03 structured output")


class Estimate(BaseModel):
    points: Literal[1, 2, 3, 5, 8, 13] = Field(description="Fibonacci story points")
    reason: str


m = model([
    [structured(Estimate, points=5, reason="Two tables plus a test suite.")],
    [structured(RouteDecision, route="sprint")],
])

estimate = structured_call(Estimate, "Estimate: add a column to two dbt models and test it.", model=m)
print(type(estimate).__name__, estimate)

route = structured_call(RouteDecision, "Classify: how is sprint 31 going?", model=m)
print("route:", route.route)
