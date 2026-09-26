# 7. Multi-agent patterns

## The menu

Strands supports several ways to make agents work together. They differ in **who
decides what runs next**:

| Pattern | Who decides | Strands API | Good for |
|---------|-------------|-------------|----------|
| **Router + lanes** | your code (after one classification call) | plain Python + several `Agent`s | a chat app with distinct specialist modes, where each mode's tools must be controlled |
| **Workflow** | your code, fixed steps | plain Python | a known sequence or loop (write → review → revise) |
| **Agents as tools** | an orchestrator *agent* | pass an `Agent` in another agent's `tools=[...]` (or `agent.as_tool()`) | open-ended tasks where the model should pick which specialist to consult |
| **Swarm** | the agents, by handing off | `strands.multiagent.Swarm` | collaborative problem-solving with no fixed order |
| **Graph** | edges + conditions you declare | `strands.multiagent.GraphBuilder` | explicit pipelines/cycles that you want the framework to run, trace and bound |

## What this repo uses, and why

**Router + lanes** for the chat (`pmagent/assistant.py::PMAssistant`):

- `pmagent/agents/router.py::classify` makes one structured-output call per turn,
  with a pure-Python fast path. `is_continuation` means "yes, create them" reuses
  the previous lane with **no model call**. Asking a model to classify "yes"
  is asking it to guess.
- Each lane is its own `Agent` with its own prompt and tools. Tool access is a
  **safety boundary**: the query lane cannot write because it doesn't *have* write
  tools.
- All lanes share one message list (lesson 4).
- Why not agents-as-tools? An orchestrator agent would decide which specialist runs
  and could chain several in one turn, and the approval gate would have to work
  across nested agents. The original's deterministic per-turn routing is simpler to
  reason about and easy to test, so the port kept it.

**Workflow** for the PRD (`pmagent/agents/requirements.py::run_requirements_loop`):
write, then review, then either render or write again. The reviewer's structured
verdict decides. `max_iterations` is a safety net, not the control flow.

**Graph** is shown as the alternative for that same loop:
`docs/learning/examples/07_graph_reflection.py` builds it with `GraphBuilder`. The
pieces:
- `add_edge("reviewer", "writer", condition=needs_revision)` for the cycle;
- `set_max_node_executions(6)` for the safety net;
- `reset_on_revisit(False)` so the writer remembers its first draft.

Read both. The Graph version gives you tracing, timeouts and a framework-owned
execution order. The plain-Python version is shorter and easier to unit-test.
For a 2-node loop, plain code wins. For a 6-stage pipeline with parallel branches,
a Graph starts to pay for itself. An example is the original project's roadmap:
twelve planned agents from requirements capture through data modelling to
governance review, of which only the first few exist.

## How the LangGraph original did it

Everything was a LangGraph `StateGraph`: a `classify` node with conditional edges to
seven lanes, and the PRD loop as a compiled subgraph. LangGraph makes the graph the
only way to compose. Strands makes the agent the unit and lets you choose the
composition. The port's lesson is that most of the original graph was plumbing that
plain Python expresses in fewer lines.

## Exercise

1. Run `07_graph_reflection.py` offline, then (**live**) with `--live`. Change
   `set_max_node_executions(6)` to `3`. What happens to the status?
2. Sketch (on paper) what `PMAssistant` would look like as agents-as-tools: one
   orchestrator agent with `tools=[ticket_agent, sprint_agent, ...]`. Where would the
   approval gate have to live, and what happens to `[route: x]`?
3. Add an 8th lane, e.g. a "release notes" lane with read-only Jira tools:
   - a module in `pmagent/agents/`;
   - a route in `pmagent/schemas.py::RouteDecision`;
   - an entry in `pmagent/agents/router.py::ROUTE_TO_LANE` and in
     `PMAssistant.lanes`.

   `uv run pytest tests/test_agent_lanes.py` will tell you what you forgot.
