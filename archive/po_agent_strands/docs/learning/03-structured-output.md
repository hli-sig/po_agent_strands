# 3. Structured output

## The concept

When you need **data**, not prose, from the model (a route, a PRD, a review
verdict), ask for a Pydantic object:

```python
from pydantic import BaseModel
from typing import Literal

class RouteDecision(BaseModel):
    route: Literal["ticket", "sprint", "query", ...]

result = agent("Classify: how is sprint 31?", structured_output_model=RouteDecision)
result.structured_output          # RouteDecision(route='sprint'), validated
```

How it works: Strands adds a **hidden tool** named after the schema
(`RouteDecision`) whose input schema is the Pydantic model, and tells the model to
call it. The tool's input is validated. If validation fails, the error goes back to
the model to fix. The loop ends as soon as a valid object arrives.

Two consequences:

1. It works with any provider that supports tool calling. No provider-specific JSON
   mode is needed.
2. It is a **tool call in the conversation**. If you run it on an agent that holds
   your real conversation, that hidden tool traffic lands in the history. So this
   repo never does that.

## In this repo

- `pmagent/llm.py::structured` is the one helper for stateless structured calls. It
  builds a **fresh** Agent per call with:
  - an empty message list, so hidden tool traffic never reaches the user's
    conversation;
  - `callback_handler=None`, so nothing prints;
  - no hooks, so there is nothing to approve.
- It is used for:
  - `pmagent/agents/router.py::classify`: the route (`RouteDecision`).
  - `pmagent/agents/requirements.py::write`: the PRD (`PRD`).
  - `pmagent/agents/requirements.py::review`: the verdict (`ReviewResult`).
  - `pmagent/tools/diagram_tools.py::draft_diagram_brief`: `DiagramBrief`.
- The schemas live in `pmagent/schemas.py`. `ReviewResult.missing_requirements` is
  what drives the PRD loop. The model's judgment arrives as data, and plain Python
  decides what to do with it.

**The recurring rule:** the LLM does judgment, and plain Python does arithmetic and
formatting. `pmagent/agents/requirements.py::render_prd_markdown` turns the
structured PRD into Markdown deterministically. The model never formats the
tables.

## How the LangGraph original did it

`get_llm().with_structured_output(Schema).invoke(prompt)`. Same idea; in Strands it
is a per-call argument instead of a wrapped model.

## Exercise

1. Run `03_structured_output.py`, then (**live**) with `--live`.
2. **live** Add a field `confidence: float` (0–1) to `Estimate` in a copy of that
   example, and print it with `--live`. Offline, also add `confidence=0.8` to the
   scripted `structured(Estimate, ...)` reply, or validation fails. Why?
3. Read `tests/fakes.py::structured` (the scripted model's helper; lesson 9 covers
   the scripted model fully). Why does a scripted structured reply have to be a
   *tool call*?
