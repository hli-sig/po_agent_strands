# Implementation plan — PO_Agent rewritten on Strands Agents

Source project: `/home/hl2/development/PO_Agent` (LangGraph, package `pmagent`).
Target: `/home/hl2/development/po_agent_strands` (this repo), Strands Agents SDK
`strands-agents==1.57.x`. Purpose: same CLI experience (`uv run main.py`) and the
same functionality, with a structure that is clear enough to learn Strands from.

## 1. What the original does (the behaviour contract to preserve)

A terminal assistant. Each user turn:

1. **Classify** the turn into one of 7 routes (`ticket, sprint, query, requirements,
   spreadsheet, diagram, finance`) with structured output (`RouteDecision`).
   - Fast path: a pure-confirmation message ("yes", "create them") reuses the
     previous route with **no model call** (`is_continuation`).
   - Otherwise the classifier sees the last 6 messages + the previous route.
   - Unknown route → read-only query lane.
2. **Run the lane**: an LLM with a lane system prompt + lane tools, looping on tool
   calls until it answers. All lanes share **one conversation history**.
3. **Write gate**: any tool batch containing a tool from `WRITE_TOOL_NAMES` halts
   *before executing*; the CLI renders a human-readable description of every pending
   write (`_describe_write`, draft manifest over 3 drafts, scope warning when the
   user named ≥3 keys and a create declared no `scope`), asks `Approve? [y/N]`.
   Approve → the batch executes. Reject → the write tool gets a result
   "The user rejected this action. Nothing was written. Ask what they would like
   changed." and the agent responds to it.
4. **Requirements lane** is not a tool loop: Writer (structured `PRD`) → Reviewer
   (structured `ReviewResult`) → loop until approved or 3 iterations → deterministic
   `render_prd_markdown`. The markdown (plus a note if still unapproved) is the reply.
5. **Diagram lane**: local `draft_diagram_brief` tool + Lucid MCP tools (optional).
   In the original it is never actually reachable (main.py builds the sync graph),
   so diagram requests fall back to the query lane.
6. CLI: banner, `/new`, `/help`, `/exit|/quit`, `[route: x]` line when the route
   changes, agent text, dim `-> tool(args)` lines, dim `| tool result` previews
   (600 chars), per-turn exception catch, config validation at startup (LLM key +
   Jira creds), Jira URL/project/model printed on start.

All domain logic (Jira REST client, JQL builder, sprint metrics, ADF, Confluence,
FY budget converter, spreadsheet Graph client, prompt/skill Markdown) is
framework-free Python and is the bulk of the code (~6k of ~8.5k lines).

## 2. Approach: port the domain as-is, rewrite only the agent layer

| Original (LangGraph / LangChain)                        | New (Strands)                                                                 |
|---------------------------------------------------------|-------------------------------------------------------------------------------|
| `langchain_core.tools.@tool`                            | `strands.tool` (same docstring-as-spec idea)                                  |
| `llm.get_llm()` → `ChatAnthropic` / `ChatOpenAI`        | `llm.get_model()` → `AnthropicModel` / `OpenAIModel` / `OpenAIResponsesModel` |
| `PMState` + `add_messages` reducer                      | Strands `Agent.messages` (Bedrock-style message dicts), one shared list owned by `PMAssistant` |
| `make_agent_node` + `ToolNode` + router edges           | a Strands `Agent(model, system_prompt, tools)` per lane — the agent loop is built in |
| `interrupt_before=[write nodes]` + `update_state(as_node=…)` | `ApprovalGate` **HookProvider** on `BeforeToolsEvent`: `event.interrupt(...)` → agent returns `stop_reason="interrupt"`; resume with `interruptResponse`; reject → `event.cancel = <rejection text>` |
| `with_structured_output(Model)`                         | `agent(prompt, structured_output_model=Model).structured_output`             |
| `classify_node` + conditional edges                     | `router.classify()` (plain function around a throwaway structured-output Agent) + a dict lookup in `PMAssistant` |
| requirements LangGraph subgraph                         | plain-Python loop over two structured-output Agents (writer, reviewer) — the "workflow" pattern; Strands `GraphBuilder` version shown in the learning material only |
| `Command` + injected `HumanMessage(name=IMAGE_MESSAGE_NAME)` for Confluence images | tool returns a Strands `ToolResult` dict with `{"image": …}` content blocks; Strands' OpenAI provider already moves tool-result images into a user message, so the injection + `_is_injected` bookkeeping is deleted |
| `langchain-mcp-adapters` `MultiServerMCPClient`         | `strands.tools.mcp.MCPClient(url=…)` passed straight into `tools=[...]`       |
| `graph.stream(...)` + `_print_new_messages`             | `ConsoleEcho` HookProvider on `MessageAddedEvent`, `callback_handler=None`    |

### Deliberate behaviour differences (all called out in README)

1. **Gate fails closed for unknown tools.** A tool call is gated when its name is
   registered on the calling agent — checked with the executor's own lookup,
   `name in agent.tool_registry.registry or name in agent.tool_registry.dynamic_tools`
   (not `tool_names`, which skips specs that fail validation) and is not a declared
   read (module `READ_TOOLS` or an MCP server's reviewed `APPROVED_READ_TOOLS`).
   So `WRITE_TOOL_NAMES` *and* any unreviewed MCP tool are gated. A name the agent
   does not have (e.g. the query lane hallucinating `create_jira_issues` it saw in
   shared history) is NOT gated — it cannot execute, and Strands returns its own
   unknown-tool error, matching the original's ToolNode error (no bogus prompt). This fixes the latent fail-open
   defect CLAUDE.md documents for the original; known tools behave identically.
2. **Reject cancels the whole batch** (reads bundled with the write get the
   rejection result too). The original only answered the write calls. Consistent
   with "if a batch has a write, the whole batch goes through the gate".
3. **Diagram lane is reachable.** It always exists with the local
   `draft_diagram_brief` tool; Lucid MCP tools are added only when
   `LUCID_MCP_ENABLED=true` (MCP start-up failure → lane keeps only local tool, with
   a printed warning). Without Lucid the original sent diagram turns to the query
   lane; here they get the brief-drafting lane, which is strictly more useful and
   matches the lane's intent.
4. **History is unbounded**, as in the original (MemorySaver): lane agents use
   `NullConversationManager`. A sliding window would silently drop the user's key
   list mid-batch; the learning lesson explains the trade-off.
5. **Three tool docstrings are re-ordered** (text moved above `Args:`): Strands'
   docstring parser drops free text after the `Args:` block, which would silently
   delete prompt instructions (`read_confluence_page`, `inspect_fy_budget_inputs`,
   `create_fy_budget_csv`). Content unchanged, position changed.
6. No LangSmith. Strands has OpenTelemetry tracing; mentioned in the learning docs.
7. Not ported: `snowflake/` track, `docs/learning` course of the original,
   `TUTORIAL.md`, `tests/test_doc_claims.py`, `langgraph dev`/Studio config, the
   empty `jev.py`/typesafe deps. (Not part of the CLI app.)

## 3. Target layout

```
po_agent_strands/
  main.py                 CLI loop only: input, slash commands, approval prompt loop
  pyproject.toml          strands-agents[anthropic,openai], requests, pydantic, dotenv, pandas, openpyxl, mcp; dev: pytest
  .env.sample             copied from original + LUCID_MCP_ENABLED
  README.md               how to run, architecture map, behaviour differences
  CLAUDE.md               guidance for future edits (short)
  pmagent/
    env.py                copied; + LUCID_MCP_ENABLED; LangSmith line dropped
    llm.py                get_model(): the only place a provider is chosen; structured() helper
    schemas.py            TicketDraft, PRD (+rows), ReviewResult, DiagramBrief(+Node/Edge), RouteDecision  (was state.py minus PMState)
    messages.py           helpers over Strands message dicts: text_of(msg), last_user_text(messages), is_tool_result(msg)
    gate.py               WRITE_TOOL_NAMES, READ_TOOL_NAMES (derived), requires_approval(name, agent), ApprovalGate(HookProvider), REJECTION_MESSAGE
    assistant.py          PMAssistant: shared history, classify → lane, run, interrupt/resume, /new reset   (replaces graph.py)
    agents/
      common.py           make_lane_agent(name, system_prompt, tools, hooks) -> strands.Agent
      router.py           classify(), is_continuation(), recent_context(), route_to_lane()  (was orchestrator.py)
      query_agent.py      QUERY_TOOLS + SYSTEM_PROMPT (orchestrator.md)
      ticket_agent.py     TOOLS + SYSTEM_PROMPT   (unchanged shape)
      sprint_agent.py     "
      finance_agent.py    "
      spreadsheet_agent.py "
      diagram_agent.py    LOCAL_TOOLS, SYSTEM_PROMPT, build_tools()
      requirements.py     run_requirements(notes) -> (markdown, prd, review, iterations); render_prd_markdown (copied verbatim)
    prompts/              *.md copied verbatim; prompts.py copied
    skills/               copied verbatim
    tools/
      adf.py, company_knowledge.py, fy_budget/**   copied verbatim
      jira/{client,fields,jql,matching,metrics,render,validate}.py  copied verbatim (validate imports schemas instead of state)
      jira/tools_read.py, jira/tools_write.py      same bodies; `from strands import tool`
      confluence_tools.py  pure half verbatim; read_confluence_page returns a ToolResult dict with image blocks
      finance_tools.py, spreadsheet_tools.py, diagram_tools.py   decorator swap; diagram brief uses a structured-output Agent
      mcp_tools.py         MCP server config + get_lucid_client() -> MCPClient (with tool_filters), APPROVED_READ_TOOLS
    cli/
      approval.py         describe_write, draft_manifest, unchecked_scope_warning (verbatim logic, Strands message shape)
      echo.py             ConsoleEcho HookProvider (MessageAddedEvent): prints assistant text and
                          "-> tool(args)"; for user-role messages prints ONLY toolResult previews
                          ("| ...", 600 chars, image blocks as "[image: title]") and never echoes
                          the user's own prompt
  tests/                  ported suites + new Strands-specific ones (below)
  docs/
    PLAN.md               this file
    learning/             the course (section 6)
```

## 4. Key designs in detail

### 4.1 Tool call ⇄ Strands message shape
Strands messages: `{"role": "user"|"assistant", "content": [ {"text"}, {"toolUse": {"toolUseId","name","input"}}, {"toolResult": {"toolUseId","status","content":[...]}} ]}`.
`last_user_text` = newest `user` message that has a `text` block and no `toolResult` block.

### 4.2 ApprovalGate (the core safety mechanism)
```python
class ApprovalGate(HookProvider):
    def register_hooks(self, registry): registry.add_callback(BeforeToolsEvent, self._check)
    def _check(self, event):
        calls = [c["toolUse"] for c in event.message["content"] if "toolUse" in c]
        gated = [c for c in calls if requires_approval(c["name"], event.agent)]
        if not gated: return                       # pure reads: never interrupted
        reason = {"calls": [{"id": c["toolUseId"], "name": c["name"], "args": c["input"]} for c in gated]}
        answer = event.interrupt("approval", reason=reason)
        if answer != "approve": event.cancel = REJECTION_MESSAGE
```
- One interrupt per batch ⇒ one approval prompt listing every write, like the original.
- On resume Strands re-fires `BeforeToolsEvent`; `interrupt()` returns the stored answer.
- Registered on **every** lane agent by `make_lane_agent` (not optional per lane).

### 4.3 PMAssistant turn protocol
```python
@dataclass
class TurnResult:
    route: str
    pending: list[dict]        # write calls awaiting approval ([] when done)
    reply: str = ""            # text the CLI must print itself (requirements lane only;
                               # lane agents' text is printed live by ConsoleEcho)

class PMAssistant:
    def __init__(self, model=None, classify_fn=router.classify, echo_hooks=())
    def send(self, text) -> TurnResult      # classify, pick lane agent, agent.messages = self.messages, run
    def resume(self, approved: bool) -> TurnResult   # answers pending interrupts on the same agent
    def reset(self)                          # /new
    messages, route (properties)
```
- Lane agents are built once (lazily per lane) and **share history** by assigning
  `agent.messages = self.messages` before a run and reading it back after (robust to
  a conversation manager that reassigns the list).
- Conversation manager: `NullConversationManager()` on each lane agent (explicit,
  unbounded like the original).
- **Pending-interrupt safety.** Interrupt state is per-Agent and private. PMAssistant
  records `pending_lane`. `send()` while an interrupt is pending first calls
  `discard_pending()`. `discard_pending()` (and `reset()`): if the last assistant
  message has toolUse ids with no toolResult yet, **append one user message** whose
  `toolResult`s (status error, "Not executed — the approval was abandoned.") cover
  exactly those ids; the toolUse message itself is kept, or providers reject the
  history. Idempotent: if `resume()` raised after the tools already ran (Strands'
  `end_tool_cycle` cleared state and results exist), nothing is added. Always clears
  `pending_lane` and rebuilds that lane agent (the only public way to clear the
  private `_interrupt_state`). `main.py`'s per-turn `except` calls
  `discard_pending()`. `describe_write` wraps every per-tool renderer in try/except
  so rendering can never abort a pending approval.

- Requirements route: `run_requirements(last_user_text)` then append the user text
  and the rendered markdown to the shared history as normal messages. Those are added
  outside any Agent, so ConsoleEcho never sees them: PMAssistant returns the markdown
  in `TurnResult.reply` and the CLI prints it explicitly.
- Diagram route when Lucid disabled: lane still exists (local tool only).

### 4.4 Router
`classify(messages, previous_route, model)`: continuation fast path → else
`llm.structured(model, None, prompt, RouteDecision)`.

`llm.structured(model, system_prompt, prompt, Schema)` is the ONE helper for every
stateless structured call (router, PRD writer, PRD reviewer, diagram brief): a fresh
`Agent(model, system_prompt, callback_handler=None, messages=[])` per call, no hooks,
so structured-output tool traffic never reaches the terminal or the shared history.

`recent_context` adapted to Strands shape: a `user` message carrying `toolResult`
renders as `Tool result: …` (never `User:`); a toolUse-only assistant message renders
as `(called tools: a, b)`. Window: last 6 **messages** still, noted in code that one
tool round-trip is 2 messages in Strands (vs 1+N in LangChain), so it covers ≥ the
same span. Tested with a tool-result message. `route_to_lane(route)` maps to
lane names with the query fallback. `is_continuation` + vocabulary copied verbatim.

### 4.5 Models (`llm.get_model`)
Verified live against gpt-5.6-luna (tool call + structured output):
- openai, effort `none` → `OpenAIModel(client_args={"api_key"}, model_id,
  params={"reasoning_effort": "none", "temperature": 0.1})`. Without
  `reasoning_effort: none` the API 400s on function tools (observed). No `max_tokens`.
- openai, effort > none → `OpenAIResponsesModel(..., params={"reasoning": {"effort": e}})`
  (observed working). Note: Strands drops reasoning content between turns there.
- anthropic → `AnthropicModel(client_args={"api_key"}, model_id, max_tokens=LLM_MAX_TOKENS)`,
  new env var `LLM_MAX_TOKENS` default 16000 (PRDs are long).
- Cached per process (`functools.cache`).

### 4.6 Confluence images
`read_confluence_page(page, section="")` returns
`{"status": "success", "content": [{"text": text}, {"image": {"format": "png", "source": {"bytes": b}}} ...]}`
(format derived from media type; unsupported types become a note). The text keeps the
per-image labels and adds "N image(s) follow, in order 1..N" because on the OpenAI
chat path the images are moved to a separate user message and matched by order.
No injected message, no `_is_injected`. Unit-tested with the existing fake client.

### 4.7 MCP (diagram lane)
`MCPClient(url=LUCID_MCP_URL, headers=…)` — **no tool filter** (a filter would hide
`create_diagram`, the lane's purpose). Only created when `LUCID_MCP_ENABLED`.
Explicit lifecycle (the simpler one to teach): `client.start()` +
`tools = client.list_tools_sync()` inside try/except, and the lane receives the
`MCPAgentTool` **list, not the client** (passing a manually-started client to an Agent
makes the Agent call `start()` again → "session is currently running"). Failure →
warning printed, lane keeps its local tool only. PMAssistant owns the client and
calls `client.stop()` at exit (`close()`); lane rebuilds reuse the same tool list.
`APPROVED_READ_TOOLS` (empty until someone reviews Lucid's tools) feeds the gate's
read allowlist; every other Lucid tool is gated. Limitation documented: Lucid's
per-user OAuth (DCR) would need `auth_provider`, not `headers`; the original never
exercised this path either.

## 5. Tests (all offline, no credentials)

Ported (bodies unchanged except imports / invocation style):
`test_jira_tools.py`, `test_adf.py`, `test_finance_tools.py`,
`test_fy_budget_conversion.py` (self-skips without fixtures), `test_spreadsheet_tools.py`,
`test_requirements.py` (render part), `test_confluence_tools.py` (pure half; the
Command-shaped tests rewritten for ToolResult).

Invocation-style changes in ported tests: `tool.invoke({...})` → direct call
`tool(**kwargs)`; `BaseTool`/`.name` → `DecoratedFunctionTool`/`.tool_name`.

Ported and adapted to Strands shapes:
- `test_agent_lanes.py` → lane/tool classification reconciliation (every `@tool`
  classified; `WRITE_TOOLS ⊆ WRITE_TOOL_NAMES`; no read has a mutating prefix;
  query lane read-only and holds every read tool; every route resolves; approval
  line exists for every gated tool; continuation tests; classifier-context tests
  with a fake classifier model).
- `test_approval.py` → describe_write / manifest / scope-warning tests on Strands
  message dicts.

New (Strands-specific, the evidence the rewrite behaves):
- `tests/fakes.py`: `ScriptedModel(Model)` that replays scripted assistant turns
  (text and/or toolUse). Agent-level structured output goes through Strands'
  structured-output *tool*, so a scripted structured reply is a `toolUse` named after
  the Pydantic class (e.g. `RouteDecision`) — documented in the fake.
- `test_tool_specs.py`: for every tool, each docstring paragraph outside `Args:`
  appears in `tool_spec["description"]` (whitespace-normalised comparison) (guards review finding #1).
- `test_gate.py`: end-to-end through a real `strands.Agent` + `ApprovalGate`:
  read-only batch runs without interrupt; write batch interrupts **before** the
  tool body executes (spy tool records calls); approve → executes once; reject →
  tool never executes and the model sees the rejection text; mixed read+write
  batch is gated as a whole; a registered-but-undeclared (MCP-like) tool is gated;
  an unregistered name produces no interrupt and returns Strands' "Unknown tool" error.
- `test_assistant.py`: builds **every lane** through PMAssistant and asserts a
  scripted write interrupts in each; abandoned interrupt → `discard_pending()` →
  lane usable again and history holds the explicit abandoned result; `resume()` that
  raises from the model *after* an approve, then `discard_pending()` → no duplicate
  toolResults; `ContextWindowOverflowException` → CLI prints "context full — use /new"; requirements
  reply returned for printing; PMAssistant routes, continuation keeps lane without calling
  the classifier, history is shared across lanes, `/new` resets, requirements
  route appends markdown, resume after interrupt continues on the same lane.
- `test_requirements.py` additions: loop stops on approval; stops at max
  iterations; revision prompt contains missed requirements (scripted model).

## 6. Learning material (`docs/learning/`)

- `README.md` — course map + how to run the examples.
- Lessons (Markdown, each: concept → minimal snippet → where it lives in this repo
  cited by **symbol** (`pmagent/gate.py::ApprovalGate`, not line numbers, so they
  don't rot; a tiny test checks every cited symbol exists) → how the LangGraph original did it → exercise):
  1. Agent loop & models (`Agent`, `Model`, providers, `callback_handler`)
  2. Tools (`@tool`, docstring → spec, ToolResult dicts, images, `ToolContext`)
  3. Structured output (router, PRD writer/reviewer, diagram brief)
  4. Conversation state (`agent.messages`, message shape, conversation managers, shared history)
  5. Hooks (`HookProvider`, events; ConsoleEcho)
  6. Interrupts & human-in-the-loop (ApprovalGate; vs vended `HumanInTheLoop`)
  7. Multi-agent patterns (router + lanes vs agents-as-tools vs Swarm vs Graph; the requirements loop as workflow vs `GraphBuilder`)
  8. MCP (`MCPClient`, tool filters, fail-closed gating)
  9. Testing agents offline (ScriptedModel)
  - `LANGGRAPH_TO_STRANDS.md` — side-by-side concept map.
- `examples/` — small runnable scripts (offline with ScriptedModel by default;
  `--live` is opt-in and uses no write tools): hello agent, tool, structured output, hook,
  interrupt gate, GraphBuilder reflection loop.

## 7. Implementation order (with a self-review checkpoint after each)

1. Scaffold project (`uv init`, deps), copy verbatim domain files, run a diff to
   prove they are byte-identical except the declared import lines.
2. Swap tool decorators; port pure tests → green.
3. `schemas.py`, `messages.py`, `llm.py`, `gate.py`, `tests/fakes.py`, `test_gate.py` → green.
4. Lanes + router + requirements + assistant + tests → green.
5. CLI (`main.py`, `cli/`) + approval tests → green.
6. Live smoke test **only via `scripts/smoke.py`**: before `main.main()` it wraps
   `requests.Session.request` (every `requests.get/post/...` call, the Jira session,
   Confluence and Microsoft Graph all pass through it) with a **fail-closed URL
   allowlist**: any GET allowed; POST allowed only to `…/rest/api/3/search/jql` (the
   JQL read path is a POST) and the Microsoft login token endpoint; everything else
   raises `RuntimeError("smoke: write blocked")`. Also replaces
   `pmagent.tools.fy_budget.pipeline.convert_fy_budget` (imported inside the tool, so
   patching the module attribute works). The launcher self-checks first: a POST to
   `/rest/api/3/issue` must raise, a search POST must be allowed.
   Domain code stays verbatim. Driven by piped stdin; transcript saved as evidence.
   Exercises read lanes, a reject, and an approve (which must hit the block).
   `main.py` is never run live without the launcher during development.
7. Learning material + examples (run each example offline).
8. Final plan-vs-implementation review; evidence report.

## 8. Done criteria

- `uv run main.py` starts the same CLI and all 7 routes are reachable.
- Every write tool is gated; rejection writes nothing (tested offline, observed live).
- `uv run pytest -q` green offline; ported test count ≥ original's relevant tests.
- Domain modules are verbatim copies (diff evidence).
- Learning docs + examples exist and the examples run.

## 9. Review log

- Review 1: NEEDS CHANGES (0 blocker, 3 major, 10 minor). All 13 addressed:
  #1 docstring loss → §2 diff 5 + `test_tool_specs.py`; #2 abandoned interrupt →
  §4.3 pending-interrupt safety + test; #3 unsafe smoke → §7 step 6 `scripts/smoke.py`;
  #4 → NullConversationManager (§2 diff 4); #5 → gate uses `event.agent.tool_names`
  (§2 diff 1); #6 → `llm.structured` helper (§4.4); #7 → requirements reply printed
  explicitly, ConsoleEcho skips user prompt/toolResult-free echo rules, images shown
  as `[image: title]` (§3 `cli/echo.py`); #8 → `recent_context` adaptation (§4.4);
  #9 → §4.5 pinned and live-verified; #10 → §4.7; #11 → §5; #12 → §6 symbol
  citations; #13 → §4.6.
- Review 2: NEEDS CHANGES (2 major, 5 minor). #1 smoke → `Session.request` URL
  allowlist + self-check (§7.6); #2 MCP → explicit start, pass tool list (§4.7);
  #3 gate uses registry lookup (§2 diff 1, §4.2); #4 discard_pending wording +
  idempotency + test (§4.3, §5); #5 two gate cases (§5); #6 context-full message
  (§5); #7 editorial (§4.2 dict, §4.3 `reply`, merged bullets).
- Review 3: **PASS** (0 blocker, 0 major). Minors folded in as implementation notes:
  1. MCP `client.stop(None, None, None)` (no-default signature), also in the
     start-failure `except`.
  2. `list_tools_sync()` is paginated — loop until `pagination_token is None`.
  3. `smoke.py` forces `LUCID_MCP_ENABLED=false` (MCP uses httpx, not requests).
  4. Smoke allowlist matches method + exact parsed path
     (`urlparse(url).path.rstrip("/").endswith("/rest/api/3/search/jql")`); the
     spreadsheet lane can't be smoke-tested past device-code login (documented).
  5. The "context full — use /new" check lives in `tests/test_cli.py`, driving
     `main`'s turn handler with a stub assistant.
