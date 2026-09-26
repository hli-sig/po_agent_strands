# 8. MCP: tools from another process

## The concept

The Model Context Protocol lets a separate process or service expose tools. Strands'
`MCPClient` connects to one, discovers its tools, and wraps each as an `MCPAgentTool`
that an `Agent` uses like any `@tool`.

```python
from strands.tools.mcp import MCPClient

client = MCPClient(url="https://mcp.example.com/mcp")          # streamable HTTP
# or: MCPClient(lambda: stdio_client(StdioServerParameters(command="python", args=["server.py"])))

# Option A: the agent manages the lifecycle
agent = Agent(tools=[client])

# Option B: you manage it (what this repo does)
client.start()
tools = client.list_tools_sync()        # paginated: follow .pagination_token
agent = Agent(tools=list(tools))
...
client.stop(None, None, None)
```

Pick **one** owner. If you `start()` a client yourself *and* pass the client to an
agent, the agent tries to start it again and fails with "the client session is
currently running". That exact trap was caught in plan review.

`MCPClient` runs the session on a background thread, so everything stays
synchronous for you.

## How the LangGraph original did it

The original used `langchain-mcp-adapters`' `MultiServerMCPClient`. Its discovery
was `async`, which forced the whole graph onto `.ainvoke`. `main.py` never
adopted that, so the original's diagram lane never actually ran. Its write gate
also failed *open* on discovered tools (next section).

## The safety problem MCP creates

Your `@tool`s are declared in `READ_TOOLS` / `WRITE_TOOLS`. A server's tools are
discovered at runtime, and nothing tells you which ones write. The original gate
only knew a list of write names, so an MCP tool fell through as a **read** and
ran ungated. That is the latent defect its CLAUDE.md warned about.

This repo's gate fails closed instead (`pmagent/gate.py::requires_approval`):

- A tool is free only if it's a declared read, or listed in
  `pmagent/tools/mcp_tools.py::APPROVED_READ_TOOLS` after a human reviewed it.
- Every other tool the agent has, including every Lucid tool until reviewed,
  pauses for approval.

`tests/test_gate.py::test_a_registered_but_undeclared_tool_is_gated` proves it.

## In this repo

- `pmagent/tools/mcp_tools.py::start_lucid`: explicit start, paginated discovery,
  warning and `None` on failure, so an optional lane never breaks startup.
- `pmagent/tools/mcp_tools.py::MCPSession`: holds the client and tools. `close()` is
  called by `pmagent/assistant.py::PMAssistant.close` at exit.
- `pmagent/agents/diagram_agent.py::build_tools`: local tool plus discovered tools.
  Without Lucid, `pmagent/agents/diagram_agent.py::system_prompt` tells the lane it
  can't create diagrams.
- Off by default: set `LUCID_MCP_ENABLED=true`. Limitation: Lucid's per-user OAuth
  would need `MCPClient(auth_provider=...)`. Only a static bearer token is wired.

## Case study: a server you own (the sprint review)

Lucid is a server someone else wrote. The other half of MCP is writing your own.
This case study comes from a real tool: a sprint review builder that turned a
Jira CSV export into a stakeholder deck. It started as an MCP server. It was
later handed over as a **skill plus a deterministic script** instead, and the
design review behind that decision is a good lesson in what MCP is for.

The example rebuilds the part that belongs in a server, the arithmetic, on
synthetic data:

- `docs/learning/examples/sprint_review/analyser.py::analyse`: a Jira CSV in and
  `sprint-review-analysis.v1` metrics out, in plain Python. It has no Strands and
  no MCP, for the same reason `pmagent/tools/jira/metrics.py::compute_sprint_metrics`
  has none: **the model does judgment, code does arithmetic.**
- `docs/learning/examples/sprint_review/server.py::create_server`: an `MCPServer`
  with three tools. `list_sprint_exports` and `analyze_sprint` read;
  `save_metrics` writes `runs/<run_name>/metrics.json`.
- `docs/learning/examples/08_sprint_review_mcp.py`: starts the server over stdio
  and hands its tools to a lane built by
  `pmagent/agents/common.py::make_lane_agent`, which puts the real gate on it.

### What the design review found, and where each fix lives

| Finding in the original server | The fix | Where |
|---|---|---|
| Accepted any host path | every CSV must resolve (symlinks included) to a `.csv` file under one `--root` | `docs/learning/examples/sprint_review/server.py::resolve_csv` |
| Overwrote a predictably named output | each run gets a **new** folder; an existing run name is refused | `docs/learning/examples/sprint_review/server.py::save_run` |
| Counted every row; the scope rule lived in people's heads | an exclusion list that drops only *unfinished* cards, applied before every metric and recorded in the output | `docs/learning/examples/sprint_review/analyser.py::analyse` |
| Unknown Jira statuses could be misfiled | kept under their own name and counted (the original's check that counts sum to the included rows is kept too) | `docs/learning/examples/sprint_review/analyser.py::normalize_status` |
| No provenance for a number on a slide | the SHA-256 of the exact bytes analysed, the filters, and the denominator (`started_work`) are in every result; a date it can't read is listed, not silently dropped | `docs/learning/examples/sprint_review/analyser.py::analyse` |
| No tests | the handover's self-test plus the rules above | `tests/test_sprint_review.py` |

Each guard lives **in the server**. A server can't assume its host model is
careful, or that its host is this app at all.

### Three things the example shows

Run `uv run docs/learning/examples/08_sprint_review_mcp.py`:

```
discovered tools, the server's readOnlyHint, and what the gate does:
  list_sprint_exports  readOnlyHint=True   gated=True
  analyze_sprint       readOnlyHint=True   gated=True
  save_metrics         readOnlyHint=False  gated=True
  PAUSED before: analyze_sprint → approve
  ...
  [error] Error executing tool save_metrics: Run 'sprint-35' already exists. ...
```

You'll also see a `Tool 'save_metrics' failed: …` line somewhere in the output.
That is the server process logging the refusal on its stderr, which the stdio
client passes through to your terminal.

1. **Annotations are hints, not trust.** The server marks two tools
   `readOnlyHint=True` (MCP `ToolAnnotations`). Strands passes annotations
   through on the tool spec untouched, and the gate ignores them: every
   discovered tool pauses until a human lists it in
   `pmagent/tools/mcp_tools.py::APPROVED_READ_TOOLS`. A server that lies, or has
   a bug, must not be able to exempt itself from approval.
2. **A refusal the model can read versus a crash.** In `mcp` 2.x, a tool that
   raises `ToolError` sends its message to the model as an error result. Any
   other exception is a crash: the model sees only "Error executing tool
   save_metrics" and the details stay in the server's log.
   `docs/learning/examples/sprint_review/server.py::RefusedError` is a
   `ToolError` on purpose, so the model can explain the refusal or pick a new run
   name. Strands turns `isError` into a `toolResult` with `status: "error"`. The
   server's own guards are wrapped the same way: a NUL byte in a name, a
   `runs` folder that is really a symlink, or a file where a folder should be
   are all refusals with a reason, not crashes.
3. **The numbers are the server's.** The answer quotes `done`, `active_flow` and
   `done_or_active_pct_of_started` from the tool result; the system prompt
   forbids computing them. With `--live`, a real model gets the same tools and
   prompt.

### Shipping the server

One reason the handover left MCP behind was installation: the recipients had no
Python, no `uv`, and no MCP setup. A container removes that cost. This project's
Docker image runs the same server as a named command (`scripts/container.py`):

```bash
docker run --rm --init -i --network none -v po-agent-data:/app/data po-agent sprint-review-mcp
```

`-i` keeps stdin open, because stdio is the transport. The `--root` is
`/app/data/sprint-review`, so exports and saved runs live in the volume. The
volume starts empty, so copy an export in first:

```bash
docker run --rm -v po-agent-data:/app/data po-agent \
  cp docs/learning/examples/sprint_review/sample-jira.csv data/sprint-review/
```
 Any MCP
host that can launch a process can use it. Here it is from Strands:

```python
MCPClient(lambda: stdio_client(StdioServerParameters(
    command="docker",
    args=["run", "--rm", "--init", "-i", "--network", "none",
          "-v", "po-agent-data:/app/data", "po-agent", "sprint-review-mcp"])))
```

The client code doesn't change; only the command that starts the server does.
The container adds a boundary of its own too. The server sees the image and the
volume, not your home folder, and `--network none` removes the network, which
this server never needs.

### MCP server or skill?

The handover made the skill its primary delivery, keeping the MCP server only
as a legacy option, because making the deck was never one deterministic call. It meant filtering scope, adding user context, reusing an
approved reference deck, writing sources into speaker notes, rendering every
slide and checking the layout, over several rounds. That is a **workflow with
judgment in it**, and a skill (Markdown instructions the host model follows)
fits it. The figures still came from a script. Put simply:

| Put it in… | when it is… | Sprint review examples |
|---|---|---|
| an **MCP server** (or a local `@tool`) | a deterministic capability several hosts can call, with guards that must hold whatever the model does | `analyze_sprint`, `save_metrics` |
| a **skill** or prompt | a procedure, house rules, or judgment: when to ask, what goes on which slide, how to label user-supplied context | "separate Jira facts, user context and interpretation"; "render and inspect every slide" |

This repo already does both. Its skills are Markdown in `pmagent/skills/`, put
into a lane's prompt by `pmagent/prompts/prompts.py::inject_skill`. Its tools
are `@tool`s and MCP tools. MCP earns its extra process only when the capability
must be **shared across hosts** or **owned by someone else**. For one app, a
local `@tool` calling the same `analyse` function would do.

## Exercise

1. Run `08_mcp_local.py`. Read `docs/learning/examples/mcp_server.py`: that's a
   whole MCP server.
2. Add a second tool to `mcp_server.py` (e.g. `days_between(a, b)`) and confirm it
   is discovered.
3. Give that local server to the diagram lane. In a scratch script, start the
   client as `08_mcp_local.py` does, then build
   `PMAssistant(model=ScriptedModel([[call("fibonacci_round", estimate=6)], [text("8")]]),
   classify=lambda m, p: "diagram", mcp=MCPSession(client, tools))`. The scripted
   model is from lesson 9; `classify=` skips the router. Call `send(...)`: is the
   result `pending`, i.e. gated? Then add `"fibonacci_round"` to
   `APPROVED_READ_TOOLS` **in `pmagent/tools/mcp_tools.py` itself** and run the
   script again. Patching it at runtime won't work, because `pmagent/gate.py`
   builds `READ_TOOL_NAMES` once, at import. Revert the edit afterwards.
4. Run `08_sprint_review_mcp.py` and count the pauses. Add `"list_sprint_exports"`
   and `"analyze_sprint"` to `APPROVED_READ_TOOLS` in
   `pmagent/tools/mcp_tools.py` and run it again: only the two `save_metrics`
   calls should pause. Revert the edit (`tests/test_sprint_review.py` expects
   three pauses).
5. In `docs/learning/examples/sprint_review/server.py`, make `RefusedError`
   subclass `ValueError` instead of `ToolError` and run the example again. What
   does the model see for the second save now, and where did the reason go? Run
   `uv run pytest tests/test_sprint_review.py -q` to see which test catches it,
   then revert.
6. **(think)** The handover's skill says "never overwrite a final deck", "cite the
   source in speaker notes" and "render and inspect every slide". Which of these
   could a server *enforce*, and which can only be *instructions* to the model?
   What does that tell you about where each belongs?
