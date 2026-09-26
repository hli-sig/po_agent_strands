# 9. Testing agents offline

## The concept

You can't unit-test an LLM, but you can unit-test **everything around it**: the
loop, the hooks, the gate, the routing, and what your code does with each kind of
answer. The trick is a fake `Model`.

A Strands `Model` must implement four methods. Three are trivial for a fake
(`get_config`, `update_config` and `structured_output`; see how
`tests/fakes.py::ScriptedModel` does them). The one that matters is `stream()`, an
async generator of provider-neutral events:

```
messageStart → (contentBlockStart → contentBlockDelta → contentBlockStop)* → messageStop → metadata
```

`tests/fakes.py::ScriptedModel` replays a script of assistant turns:

```python
from tests.fakes import ScriptedModel, call, text, structured

model = ScriptedModel([
    [call("add_jira_comment", issue_keys=["CSCI-1"], comment="hi")],   # turn 1: a tool call
    [text("Posted.")],                                                  # turn 2: the answer
])
agent = Agent(model=model, tools=[...], hooks=[ApprovalGate()], callback_handler=None)
```

Everything except the model's *choices* is real: the Strands event loop, tool
execution, hooks, interrupts and resume. `model.requests` records what the model was
shown each time (system prompt, tool names, messages), so you can assert on the
context too.

Structured output is scripted as a call to the hidden tool named after the schema:
`structured(ReviewResult, approved=True)`. That is lesson 3's mechanism made
visible.

## What the suite proves, and how

| File | Proves |
|------|--------|
| `tests/test_gate.py` | a write's body never runs before approval; reject never runs it; batches gate as a whole; unknown MCP-like tools gate; unregistered names don't |
| `tests/test_assistant.py` | every writing lane pauses; lanes share history; abandoned approvals leave the lane usable with no duplicate results; the requirements reply is returned |
| `tests/test_cli.py` | the approval prompt renders, only `y`/`yes` approves, Ctrl-D abandons, context overflow says `/new` |
| `tests/test_requirements.py` | the reviewer's missing items drive a revision; the cap ends the loop |
| `tests/test_agent_lanes.py` | every `@tool` is classified read/write; the gate's lists agree; every route resolves |
| `tests/test_tool_specs.py` | no docstring prose is lost on the way to the model |
| `tests/test_jira_tools.py`, `test_confluence_tools.py`, … | the domain code, unchanged from the original; each client is tested through an injected fake session |
| `tests/test_fy_budget_synthetic.py` | the FY converter on made-up workbooks in both real layouts (`tests/fy_synthetic.py`), so it runs with no financial data; expected totals come from the builder's formula, not from the converter |

**Test your tests.** A test that has never failed proves little. During the port the
gate was disabled on purpose, and 7 of 11 gate tests failed. Then it was restored.
Try it yourself (exercise 2).

## Live checks, safely

Unit tests can't tell you the real model follows your prompts, or that Jira accepts
your JQL. `scripts/smoke.py` runs the real CLI against the real model and Jira. It
wraps `requests.Session.request` with a **fail-closed allowlist**: GETs, the JQL
search POST and the token refresh are allowed, and everything else raises
`smoke: write blocked`. That makes it safe to press `y` at an approval prompt.
Transcripts are in `docs/evidence/`.

## How the LangGraph original did it

The original tested pure functions and graph *wiring*. For example, it checked that
every route had an edge, and that a write name was in `WRITE_TOOL_NAMES`. It patched
`get_llm` to return canned structured outputs. It did not run the graph end to end
offline, so a whole turn's behaviour was checked by a human, live.
A fake `Model` is what lets this repo test the actual loop, gate and interrupts.

## Exercise

1. Write a test: a scripted model calls `transition_jira_issues`, the user rejects,
   and the model's *next* request contains the rejection text. (Hint:
   `model.requests[-1]["messages"]`.)
2. Break the gate: in `pmagent/gate.py::ApprovalGate.check_batch`, change `if not
   gated:` to `if True:`. Run `uv run pytest tests/test_gate.py -q`, count the
   failures, then revert it.
3. Write a `ScriptedModel` turn that is a *function* of the messages (see
   `tests/test_assistant.py::test_the_route_is_announced_before_the_lane_runs`) and
   use it to answer differently depending on a tool result.
