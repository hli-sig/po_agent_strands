# Rebuild PO Agent yourself

Lessons 1–11 explain the finished project. This guide has you **build all of it
again from an empty folder**, one layer at a time: first the domain code the
agents call (Jira, Confluence, the FY budget converter, …), then the Strands
agent layer, the CLI, the web UI, your own MCP server and the Docker image.

**You start fresh and copy nothing**, not from the original LangGraph project and
not from this repository. You write every file: the code, the tests, the prompts,
the skills, the web page, the fixtures. This guide is the whole specification.
Every step ends at a **checkpoint**: the tests *you* wrote for that step pass, and
they cover every case in the step's table.

Time: about 5–6 days for the domain layer (R1), 2–3 days for R2–R6 (the CLI app),
1–1½ days for R7 (web), and 2–3 hours each for R8, R9 (your own MCP server) and
R10 (the Docker image).

## How this guide works

- **One folder**, your build, called `$MY` below:
  `export MY=~/development/my_po_agent`.
- **Each step gives the design, the traps, and a table of cases.** Write a test
  for every row. The rows are the behaviour that matters, including the failures
  that bit the original project. Where a later step depends on a name, a
  signature or an exact string, the step says so; otherwise the wording is yours.
- **A checkpoint is: your tests pass and cover the table.** Then break the code
  on purpose once, as the step suggests, and watch a test fail. A test that has
  never failed proves nothing.
- **Use the module names given** (`pmagent/gate.py`, `pmagent/assistant.py`, …).
  Later steps import them.
- **Your tests use a scripted model** (you write it in R2, lesson 9 explains it)
  and fake HTTP sessions (rule 7 of the domain chapter), so every checkpoint runs
  offline, with no API key and no Jira. The one live step (R8) goes through a
  write-blocking launcher you write first.
- `➕ path` marks a file you create.
- Run `unset VIRTUAL_ENV` first if your shell activates another project's venv.

## R0 — Project skeleton (30 min)

```bash
mkdir -p $MY && cd $MY
git init
uv init --bare --python 3.13 --name my-po-agent
uv python pin 3.13
uv add "strands-agents[anthropic,openai]==1.57.0" "mcp>=2.1,<3" "pydantic>=2.11" \
       "python-dotenv>=1.1" "requests>=2.32" "pandas>=2.2,<3" "openpyxl>=3.1,<4"
uv add --dev "pytest>=8"
mkdir tests && touch tests/__init__.py
```

Add to `pyproject.toml`:

```toml
[tool.pytest.ini_options]
pythonpath = ["."]
testpaths = ["tests"]
```

Then write two config files:

- `➕ .gitignore`: `.venv/`, `__pycache__/`, `*.py[cod]`, `.pytest_cache/`, `.env`
  and `.env.*` (but keep `!.env.sample`), `data/` (budget files are real
  financial data), `*.log`, and `*.zip` (handover packages can hold real decks).
- `➕ .env.sample`: every setting from the domain chapter's D0 table, with its
  default and a one-line comment, plus the secrets left blank. It is the
  template a user copies to `.env`.

Pin Strands **exactly** (`==1.57.0`): the SDK moves fast, and this course was
verified against 1.57.0. `mcp` must be 2.x: R9's server imports
`mcp.server.mcpserver`, which 1.x doesn't have. The first `uv add` downloads
packages, so it needs the network even if everything after it is offline.

**Checkpoint R0:** `uv run python -c "import strands; print('ok')"` prints `ok`,
and `git check-ignore .env data/x.csv a.zip` lists all three.

## R1 — Build the domain layer (5–6 days)

The domain layer has its own chapter: [Rebuild the domain layer](rebuild-domain.md),
steps D0–D7. It covers configuration, the Pydantic contracts, the prompts and
skills, the Markdown↔ADF converter, the Jira package, Confluence, the FY budget
converter, the finance tools, the spreadsheet approval queue and the
company-knowledge seam. Each module is plain Python with no Strands in it except
`from strands import tool`.

**Checkpoint R1** is that chapter's last checkpoint: `uv run pytest tests -q`
passes, and every D-step's table is covered. Why it matters: every tool the
agents will call now works, and is tested, before a single agent exists. That is
what "the framework is a thin layer" means in practice.

## R2 — The scripted model, the model layer, and the tools that need a model (1 day)

1. `➕ tests/fakes.py` [lesson 9]: the scripted model every later test uses. A
   Strands `Model` needs `update_config`, `get_config`, `structured_output` and
   `stream`; only `stream` matters.
   - `text(value)` → `{"text": value}`; `reasoning(value)` →
     `{"reasoningContent": {"text": value}}`; `call(name, **args)` →
     `{"toolUse": {"name": name, "toolUseId": "tooluse_<n>", "input": args}}` with
     a fresh id each time; `structured(Schema, **fields)` →
     `call(Schema.__name__, **Schema(**fields).model_dump(by_alias=True))`,
     because structured output is a call to a hidden tool named after the schema
     (lesson 3).
   - `ScriptedModel(turns)`: `get_config`/`update_config` keep a small dict, and
     `structured_output` raises `NotImplementedError` (agents never call it; they
     use the hidden tool). Each turn is a list of blocks, or a function
     `messages -> blocks` for a reply that depends on the conversation.
     `stream(messages, tool_specs=None, system_prompt=None, **kw)` is an async
     generator. It records `{"system_prompt", "tools" (the spec names),
     "messages" (a deep copy)}` in `self.requests`, pops the next turn (an
     `AssertionError` if there is none), and yields:
     `{"messageStart": {"role": "assistant"}}`; then per block a
     `contentBlockStart` (with `{"start": {"toolUse": {"name", "toolUseId"}}}` for a
     tool call, else `{"start": {}}`), one `contentBlockDelta` (`{"delta": {"text":
     …}}`, `{"delta": {"reasoningContent": {"text": …}}}` or `{"delta": {"toolUse":
     {"input": <the args as a JSON string>}}}`) and a `contentBlockStop`; then
     `{"messageStop": {"stopReason": "tool_use"}}` if any block was a call, else
     `"end_turn"`; then `{"metadata": {"usage": {"inputTokens": 0, "outputTokens":
     0, "totalTokens": 0}, "metrics": {"latencyMs": 0}}}`. A `remaining` property
     counts the turns left.
2. `➕ pmagent/llm.py` [lesson 1, lesson 3]:
   - `build_model()` picks from env: `AnthropicModel(client_args={"api_key": …},
     model_id=LLM_MODEL, max_tokens=LLM_MAX_TOKENS)`; for OpenAI with reasoning
     effort `none`, `OpenAIModel(…, params={"reasoning_effort": "none",
     "temperature": 0.1})` (gpt-5.x rejects function tools on chat completions
     without that effort); any other effort, `OpenAIResponsesModel(…,
     params={"reasoning": {"effort": …}})`, adding `"summary"` only when
     `LLM_REASONING_SUMMARY` is set. An unknown provider
     raises `ValueError`. Provider imports are lazy.
   - `get_model()` caches `build_model()` for the process.
   - `structured(schema, prompt, *, system_prompt=None, model=None)` runs a
     **fresh** `Agent` per call: `callback_handler=None`, `messages=[]`, no hooks,
     `structured_output_model=schema`, and returns `result.structured_output`. A
     fresh agent keeps the hidden output tool out of every real conversation.
   - `invocation_usage(result)` reads `result.metrics.latest_agent_invocation`
     and returns `{"input_tokens", "output_tokens", "total_tokens",
     "model_calls"}` (the last is its number of cycles).
   - `reasoning_available()`: true only for OpenAI with a reasoning effort other
     than `none` and a reasoning summary set.
3. `➕ pmagent/tools/diagram_tools.py` [lesson 2, lesson 3]:
   - `_DIAGRAM_BRIEF_ROLE`: instructions to pick one diagram type and describe it
     using only what the source says.
   - `_build_prompt(prd_text, reporting_intent_text="")` adds a
     `Reporting Intent brief:` section only when one is given.
   - `render_diagram_brief(brief)` is plain Python: `Diagram type: …`, `Title: …`,
     the description, `Nodes:` (`- id: label`) and `Edges:` (`- a -> b (label)`,
     no brackets when there's no label).
   - `draft_diagram_brief(prd_text, reporting_intent_text="")` is
     `@tool(context=True)`: it calls `structured(DiagramBrief, …,
     model=tool_context.agent.model)`, so the brief uses the calling lane's model,
     including a test's scripted one. `READ_TOOLS = [draft_diagram_brief]`.
4. `➕ pmagent/tools/mcp_tools.py` [lesson 8]: `APPROVED_READ_TOOLS =
   frozenset()`; `MCPSession(client, tools)` with `close()` (it calls
   `client.stop(None, None, None)`: `stop` is a context-manager exit and has no
   defaults); and `start_lucid()` (R6's `main.py` calls it only when
   `LUCID_MCP_ENABLED` is true),
   which builds `MCPClient(url=LUCID_MCP_URL, headers={"Authorization": "Bearer
   …"} or None)`, calls `start()` explicitly, lists **every page** of tools
   (`list_tools_sync(pagination_token=…)` until the token is `None`), and on any
   failure prints a warning, stops the client and returns `None`.

**Your tests must cover:**

| File | Cases |
|---|---|
| `➕ tests/test_fakes.py` | a text turn gives that reply; a call turn runs the tool and the next turn answers; a turn function sees the messages; `structured()` fills `result.structured_output`; `requests` records the system prompt and tool names; running out of turns fails loudly |
| `➕ tests/test_model_layer.py` | each provider → its model class and params (construct only, no network); unknown provider refused; `reasoning_available` both ways; `invocation_usage` keys and `model_calls == 1` for one reply; `start_lucid` follows two pages and sends the bearer header (monkeypatch `strands.tools.mcp.MCPClient` with a fake); a failing start → `None`, a warning, and the client stopped; `APPROVED_READ_TOOLS` is empty |
| `➕ tests/test_diagram_tools.py` | the exact rendering; the reporting-intent section only when given; the tool, run through a real `Agent` with one scripted model (the call, then the structured reply, then text), returns the rendered brief |
| `➕ tests/test_tool_specs.py` | for every tool in every tool module, every prose paragraph of its docstring, **wherever it sits** except inside the `Args:` block itself, appears in `tool.tool_spec["description"]`. Prose below `Args:` is exactly what Strands drops, so the test must look for it there |

**Checkpoint R2:** your tests pass. Break it: move one tool's "this WRITES"
sentence below `Args:` and watch `test_tool_specs.py` fail.

## R3 — Messages and the approval gate (3–4 h)

1. `➕ pmagent/messages.py` [lesson 4]: `blocks`, `text_of`, `tool_uses`,
   `tool_results`, `is_tool_result`, `last_user_text` (it **skips**
   `toolResult` user messages: in Strands a tool result is a `role: "user"`
   message), `user_message`, `assistant_message` and `result_text` (text of a
   tool result; an image becomes `[image: png]`).
2. `➕ pmagent/gate.py` [lesson 6]:
   - `TOOL_MODULES`: the six modules that define `@tool`s:
     `tools/jira/tools_read.py`, `tools/jira/tools_write.py`,
     `confluence_tools.py`, `finance_tools.py`, `spreadsheet_tools.py` and
     `diagram_tools.py` (the two Jira submodules, not the package; `mcp_tools`
     defines no tool, and its reviewed reads arrive through
     `APPROVED_READ_TOOLS`).
   - An explicit `WRITE_TOOL_NAMES` (every write tool's name, listed by hand, so
     adding a write is a deliberate two-place change); `READ_TOOL_NAMES` derived
     from each module's `READ_TOOLS` plus `mcp_tools.APPROVED_READ_TOOLS`;
     `REJECTION_MESSAGE`; `APPROVE = "approve"` and `REJECT = "reject"`.
   - `requires_approval(name, agent)`: a name the agent doesn't have → `False`
     (it can't run anyway); a write → `True`; anything not declared a read →
     `True` (fail closed). Check "has" the way the executor does, with
     `agent.tool_registry.registry` and `.dynamic_tools`: `agent.tool_names`
     leaves out a tool whose spec failed validation but which would still run.
   - `ApprovalGate(HookProvider)` on `BeforeToolsEvent`: collect the gated calls
     of the batch; if any, `answer = event.interrupt("approval", reason={"calls":
     [{"id": <toolUseId>, "name": …, "args": <input>}, …]})` listing only the gated
     ones; if the answer isn't `APPROVE`, set `event.cancel = REJECTION_MESSAGE`.
     The CLI and the web API read those three keys.

**Your tests (`➕ tests/test_messages.py`, `➕ tests/test_gate.py`) must cover:**

| Case | Expect |
|---|---|
| `last_user_text` on a history ending in tool results | the human's last words, not the tool output |
| `result_text` on a result with an image | `[image: png]` in the text |
| stand-in tools (a read and a write you define in the test, added to the gate's sets with `monkeypatch`) | classified like the real ones |
| a read-only batch | runs without an interrupt |
| a write | stops before the tool body runs (`stop_reason == "interrupt"`) |
| approve | the write runs exactly once |
| reject | the write never runs and the model sees `REJECTION_MESSAGE` |
| a read bundled with a write | the whole batch waits; the interrupt lists only the write |
| a second write after an approved one | interrupts again (answers aren't reused) |
| a tool the agent has but nobody declared | gated |
| a name the agent doesn't have | not gated (and it can't run) |

**Checkpoint R3:** your tests pass. Break it: change `if not gated: return` to
always return, and count the failures.

## R4 — Lanes, router and the PRD workflow (4–5 h)

1. `➕ pmagent/agents/common.py`: `make_lane_agent(name, system_prompt, tools, *,
   model=None, hooks=(), messages=None)` returns `Agent(model=model or
   get_model(), name=…, system_prompt=…, tools=…, hooks=[ApprovalGate(), *hooks],
   conversation_manager=NullConversationManager(), callback_handler=None,
   messages=messages or [])`. The gate is not optional. `NullConversationManager`
   keeps the whole history, because a batch is checked against keys the user typed
   at the start (lesson 4). (R7 adds a `callback_handler` parameter.)
2. **The lane declarations.** Each lane module is two constants, `SYSTEM_PROMPT`
   and `TOOLS`; `make_lane_agent` does the rest. **Bind exactly the tools the
   lane's prompt names**, and your R6 tests check it both ways:
   - `➕ pmagent/agents/query_agent.py`: the read-only lane: `query_jira_issues`,
     `read_jira_issue_details`, `read_jira_issues_by_key`, `list_jira_transitions`,
     `inspect_fy_budget_inputs`, `read_fy_budget_run`, `read_confluence_page` and
     `search_confluence`, with the orchestrator prompt.
   - `➕ pmagent/agents/ticket_agent.py`: `query_jira_issues`,
     `read_jira_issue_details`, `read_jira_issues_by_key`, `find_jira_user`,
     `list_jira_transitions`, both Confluence tools, `validate_ticket_drafts`, and
     every Jira write except `create_jira_issue`; the ticket prompt with the
     `ticket` skill injected.
   - `➕ pmagent/agents/sprint_agent.py`: both sprint-status tools,
     `query_jira_issues`, `read_jira_issue_details`, `read_jira_issues_by_key`,
     `list_jira_transitions`, `add_jira_comment`, `transition_jira_issues` and the
     two sprint moves (scope changes happen in sprint conversations).
   - `➕ pmagent/agents/finance_agent.py`: the three finance tools, with the
     `fy_budget` skill injected. `➕ pmagent/agents/spreadsheet_agent.py`: the three
     spreadsheet tools.
3. `➕ pmagent/agents/router.py` [lesson 7]:
   - `is_continuation(text)`: true only when the message is at most 60 characters
     and **every** word (regex `[a-z']+` on the lowercased text) is in the
     continuation vocabulary. It is a whole-message test: "yes, and also draft a
     ticket for FX" brings new work and must be classified. The vocabulary:

     ```text
     y ye yes yep yeah yup ya ok okay k sure fine
     confirm confirmed confirming confirmation approve approved approval
     create creation created make it them these those all both
     do go ahead proceed continue send ship submit push
     please thanks thank you now lgtm looks good correct right agreed exactly
     n no nope nah cancel stop dont abort reject rejected hold wait
     ```
   - `recent_context(messages)` renders the last 6 messages: `User: …`,
     `Assistant: …`, a tool-only assistant turn as `Assistant: (called tools: a,
     b)`, and a tool result as `Tool result: …`, **never** `User:`.
   - `classify(messages, previous_route="", model=None)`: if there is a previous
     route and the latest human text is a continuation, return the previous route
     with no model call. Otherwise `structured(RouteDecision, prompt)`, where the
     prompt holds the task, then (when there is one) "The previous turn was handled
     by the '<route>' route. Stay on it unless this request has genuinely moved
     on", then the recent context, then the latest request.
   - `ROUTE_TO_LANE`: `ticket → ticket_agent`, `sprint → sprint_agent`,
     `spreadsheet → spreadsheet_agent`, `finance → finance_agent`,
     `diagram → diagram_agent`, `query → query_agent`, `requirements →
     requirements` (a workflow, not an agent). `route_to_lane(route)` falls back to
     `query_agent`.
4. `➕ pmagent/agents/requirements.py` [lesson 7]:
   - `RequirementsState` (`source_notes`, `company_context`, `prd`, `review`,
     `iterations`, `max_iterations=3`, `markdown`) and
     `route_after_review(state)`: `"render"` when approved or the iteration cap is
     reached, else `"revise"`.
   - `write(state, model=None)` and `review(state, model=None)`: two
     `structured` calls that return the updated state. The writer's system prompt
     and the reviewer's are their `.md` files with the `prd` skill (`SKILL.md`)
     injected. The writer's input is the notes, the company context
     (`retrieve_company_context(notes[:200])`, from D7), and on a revision the
     previous draft plus the reviewer's missing items and notes; the reviewer's is
     the notes and the draft.
   - `render_prd_markdown(prd)`: the Atlassian template, in plain Python: `# title`,
     a two-column table of target release, owner and stakeholders, then Objective,
     Background, Success metrics (a Goal/Metric table), Assumptions, Requirements
     (a numbered table: #, user story, importance, Jira, notes), User interaction
     and design, Open questions (a table when there are some) and Out of scope.
     Empty parts read `—` or `_None…_`, never a broken table; `|` and newlines in
     a cell are escaped. `render_prd_markdown(None)` returns `""`.
   - `run_requirements_loop(state, model=None)` loops write → review until
     `route_after_review` says render, then renders the Markdown.
     `run_requirements(notes, model=None)` wraps it and returns
     `RequirementsResult(reply, prd, review, iterations)`; when the cap ends the
     loop, the reply says so. Test the cap through `RequirementsState(max_iterations=…)`.
5. `➕ pmagent/agents/diagram_agent.py`: `LOCAL_TOOLS = [draft_diagram_brief]`,
   `build_tools(mcp_tools)` (local plus discovered), and
   `system_prompt(lucid_connected)`, which without Lucid appends a note containing
   `Lucid is **not connected**` and says the lane can only draft a brief.

**Your tests (`➕ tests/test_router.py`, `➕ tests/test_requirements.py`) must cover:**

| Area | Cases |
|---|---|
| continuations | "yes please", "confirm creation" are; "yes, also draft a ticket for FX" isn't; a confirmation keeps the lane with no model call (the scripted model has no turns); an unrecognised confirmation falls through to the classifier; rejections stay in the lane; with no previous route, a confirmation is classified |
| classifier prompt | it contains the previous route phrase and earlier messages, a tool-only turn, and `Tool result:` (never `User:`) for tool output; a tool result is never taken as the user's request |
| routes | every `RouteDecision` route has a destination and every destination exists; unknown → `query_agent` |
| review loop | approved → render; unapproved → revise; the cap stops it and is not hard-coded; the reviewer's missing items reach the next write; the writer and reviewer get their own system prompts |
| rendering | the template sections in order; requirements numbered; pipes and newlines don't break a table; empty sections render placeholders; optional tables appear when populated; `None` renders `""` |

**Checkpoint R4:** your tests pass. Break it: make `is_continuation` a prefix test,
and watch the "yes, also…" case fail.

## R5 — PMAssistant: one conversation, many agents (3–4 h)

`➕ pmagent/assistant.py` [lesson 4, lesson 6, lesson 7]:

- `TurnResult(route, pending, reply)` and a module constant `ABANDONED_MESSAGE`
  ("Not executed — the approval for this action was abandoned before the user
  answered. Nothing was written.").
- `PMAssistant(model=None, *, classify=None, hooks=(), mcp=None, on_route=None)`.
  `lanes` maps each lane name to `(prompt, tools)` (the diagram lane's from
  `build_tools(mcp.tools)` and `system_prompt(bool(mcp and mcp.tools))`); lane agents are
  built lazily with `make_lane_agent`. `classify` defaults to the router's;
  `on_route(route)` is called before a lane runs.
- `send(text)`: discard any pending approval, classify on
  `self.messages + [user_message(text)]` (don't append it yet), announce the
  route, then either the requirements workflow (the user message and its reply
  are appended to the shared history, and the reply is returned) or
  `_run_lane(lane, text)`.
- `_run_lane(lane, prompt)`: point the lane at **the shared list**
  (`agent.messages = self.messages`), call `agent(prompt)` (Strands appends the
  user message), and read the list back (in a `finally`, so it happens even when
  the call raises). On `stop_reason == "interrupt"`, remember the lane and its
  interrupts and return the pending calls.
- `resume(approved)`: `_run_lane` on the **same** lane with a list of
  `{"interruptResponse": {"interruptId": …, "response": APPROVE or REJECT}}`
  blocks as the prompt. Resuming with nothing pending is an error.
- `discard_pending()`: close any unanswered tool call with a "Not executed"
  result (every `toolUse` needs a `toolResult`, or the provider rejects the
  history), then drop the paused agent. Its interrupt state is private, so
  rebuilding the agent is the only way to clear it. Calling it twice is safe.
- `reset()` (a fresh conversation) and `close()` (closes the MCP session).
- Public state: `messages`, `route`, `last_prd`, and `pending` (a `bool`
  property).

**Your tests (`➕ tests/test_assistant.py`, with `classify=lambda messages,
previous: "<route>"`, a route name such as `"ticket"`, to skip the router) must
cover:**

| Case | Expect |
|---|---|
| a write in each writing lane | pauses with a pending call |
| the query lane asked to write | the tool isn't there, so nothing is pending |
| an MCP tool in the diagram lane | gated; `close()` closes the session |
| no MCP session | the diagram prompt says Lucid is not connected |
| approve | the same lane resumes and finishes the turn |
| reject | nothing written; the agent answers the refusal |
| two lanes, then back (sprint → query → sprint) | the revisited lane sees every message, including the other lane's |
| the router | sees the new message and the previous route |
| `reset()` | a fresh conversation |
| `on_route` | called before the lane runs |
| the requirements lane | the reply is returned and kept in history |
| a new message while an approval waits | the approval is abandoned with `ABANDONED_MESSAGE`, and the lane still works |
| `discard_pending()` twice | safe |
| a resume that fails after the tool ran | no duplicate tool result |
| `resume` with nothing pending | an error |
| abandon on one lane, continue on another | works |
| the diagram brief | uses the calling agent's model |

**Checkpoint R5:** your tests pass. If they do, the heart of the app works.
Break it: remove `agent.messages = self.messages`, and the sprint → query → sprint
case should fail.

## R6 — The CLI (3–4 h)

1. `➕ pmagent/cli/echo.py` [lesson 5]: `ConsoleEcho(HookProvider)` on
   `MessageAddedEvent`. `render_message(message) -> list[str]` gives assistant text,
   `-> tool(args)` for a call, and `| …` previews of a tool result (at most 600
   characters, images labelled), and **nothing for the user's own prompt**.
2. `➕ pmagent/cli/approval.py`: what the human reads at the approval prompt.
   The rule is **show what could be wrong, not what is there**: the sprint a
   ticket lands in, the source issue each draft came from, the full comment text.
   - `_describe_write(call)` has one case per gated tool (every name in
     `WRITE_TOOL_NAMES`), and a readable fallback for anything else gated.
   - `draft_manifest(drafts)` gives one line per draft (index, source issue,
     assignee, summary) when there are more than 3, flagging a draft with no
     source.
   - `unchecked_scope_warning(messages, calls)` notes when the user's last message
     named 3 or more issue keys but a create call declared no `scope`.
   - `describe_write(call)` wraps them and **never raises**: a renderer crash
     falls back to the tool name and its arguments.
3. `➕ main.py`: the banner, `/new`, `/help` and `/exit`, and a `[route: x]`
   printer.
   - `build_assistant(route_printer)` returns a `PMAssistant` with
     `hooks=[ConsoleEcho()]`, `on_route=route_printer` (it prints `[route: x]` when
     the lane changes) and, when `LUCID_MCP_ENABLED`, the session from
     `start_lucid()`. `main()` first runs `env.validate()` and `validate_jira()`,
     and prints `Configuration error: …` and returns 1 on a `ValueError`.
   - `handle_turn(assistant, user_input, ask=input) -> bool` calls `send`, prints
     `result.reply` only for the requirements route (lane replies were already
     printed by `ConsoleEcho`; the workflow runs outside any agent, so the hook
     never sees it), runs `resolve_approvals`, and on any error prints it and calls
     `discard_pending()`; a context overflow tells the user to `/new`. It returns
     `False` when the user quit mid-approval.
   - `resolve_approvals(assistant, result, ask=input) -> bool` loops
     `describe_write` → `Approve? [y/N]` → `resume`. Only `y`/`yes` approves.
     Ctrl-D/Ctrl-C at the prompt calls `discard_pending()` and returns `False`.
   - `_quiet_library_logs()` sets the library loggers' level from
     `PMAGENT_LOG_LEVEL` (default `CRITICAL`). R7's `web.py` imports it from
     `main`, so keep the name.

**Your tests (`➕ tests/test_lanes.py`, `➕ tests/test_cli.py`) must cover:**

| Area | Cases |
|---|---|
| wiring | every module under `pmagent/tools` that defines a tool (walk it with `pkgutil`, and look for Strands `DecoratedFunctionTool` objects) is in `TOOL_MODULES` and declares both lists; the lists don't overlap; every write is in `WRITE_TOOL_NAMES` and every name there is declared; no read has a mutating name; every tool is classified |
| lanes | every tool a prompt names is bound (and the sprint prompt names `get_sprint_status_by_number`); the query lane is read-only and holds every read-only Jira tool; only the ticket lane creates issues; both ticket and sprint lanes can move, edit, transition, comment and read tickets; reading content or transitions isn't gated; the finance lane pairs its write with the reads; confluence reads wherever documentation is needed |
| prompts | the ticket prompt carries the standard and the approval step; the finance prompt carries the partition rules and forbids inventing figures |
| approval text | every gated tool has a readable line; a draft's source issue appears; more than 3 drafts get a manifest that flags a missing source; 3 or fewer don't; a renderer crash still shows something; a scopeless batch after 3+ named keys is flagged, a scoped one isn't, no keys isn't, a tool result isn't read as the user, a non-create write isn't flagged |
| CLI | a context overflow says `/new`; any other error is reported and the pending approval closed; the approval loop describes and resumes; anything but `y`/`yes` rejects; Ctrl-D abandons; a requirements reply is printed; one whole turn with a real assistant and a scripted model; your own prompt isn't echoed; images labelled; long results previewed |

**Checkpoint R6: the CLI app is rebuilt.** Your tests pass, and `uv run main.py`
with no `.env` prints `Configuration error: …` instead of a traceback. Break it:
make `resolve_approvals` accept anything starting with `y`, and the "anything but
y/yes rejects" case should fail.

## R7 — The web UI and API (1–1½ days)

Lessons 10–11 are the design. The names below are the ones the lessons use;
they're yours to change, since you write both the server and the page. Run
`uv add fastapi uvicorn sse-starlette anyio`.

| | |
|---|---|
| routes (under `/api/v1`) | `GET /health`, `GET /meta`; `POST /conversations` (201), `GET` and `DELETE /conversations/{cid}`; `POST …/{cid}/turns` (202 + `Location`), `GET …/turns`, `GET …/turns/{tid}`, `GET …/turns/{tid}/events` (SSE); `GET …/approvals/{aid}`, `POST …/approvals/{aid}/decision`. Outside it: `/` (the page), `/problems/{slug}`, `/api/docs` |
| event kinds | `turn.started`, `route.decided`, `reasoning.delta`, `text.delta`, `tool.started`, `tool.finished`, `message.completed`, `approval.required`, `approval.decided`, `prd.step`, `usage`, `turn.completed`, `turn.failed` |
| turn states | `running`, then `awaiting_approval`, `completed` or `failed`; an approval is `pending`, `approved`, `rejected` or `abandoned` |
| problem slugs | `not-found`, `validation`, `turn-in-progress`, `approval-pending`, `approval-not-pending`, `idempotency-key-reuse`, `context-full`, `turn-failed`, `unauthorized`, `bad-host`, `forbidden-origin`, `method-not-allowed` |

First extend what you have (your earlier tests must stay green):

- `router.classify_with_reason(messages, previous_route, model, usage_sink)`
  returns `(route, "continuation" | "classifier")`; `classify` becomes a thin
  wrapper. `PMAssistant` reports `"custom"` when an injected classifier decided.
- `llm.structured(..., usage_sink=None)` calls `usage_sink(invocation_usage(…))`;
  `draft_diagram_brief` passes `tool_context.invocation_state.get("usage_sink")`.
- `requirements.run_requirements(..., on_step=None, usage_sink=None)`.
- `make_lane_agent(..., callback_handler=None)`; lane calls pass
  `invocation_state={"usage_sink": …}`.
- `PMAssistant(..., on_route_decided=None, on_prd_step=None, callback_handler=None)`;
  `TurnResult.route_reason` and `.usage`.

Then the web package:

1. `➕ pmagent/web/problems.py`: an RFC 9457 `CATALOG` of problem slugs (title,
   status), `ProblemError(slug, detail="", headers=None, **extra)`, exception handlers
   that answer `application/problem+json`, and a `/problems/<slug>` page for each.
2. `➕ pmagent/web/schemas.py`: request and response models (a message of 1–20,000
   characters; conversation, turn, event, approval with `ToolCall(id, name, args)`,
   health and meta).
3. `➕ pmagent/web/trace.py`: `TraceRecorder`, a hook provider **and** a callback
   handler, turning the loop into events (route, reasoning deltas, tool started and
   finished with durations, approval, PRD steps, usage); `assistant_kwargs()`
   returns the `PMAssistant` keyword arguments that wire it in.
4. `➕ pmagent/web/sessions.py`: `Turn` (an append-only event log with
   `emit(kind, data, status=None)` and `wait_for_events`), `Approval`,
   `ConversationSession` (a worker thread per turn, idempotency keys, `decide`,
   cleanup on failure) and `SessionStore`.
5. `➕ pmagent/web/app.py`: `create_app(assistant_factory=None, *, api_token=None,
   extra_hosts=(), enable_mcp=False)`, where a factory is `factory(recorder) ->
   PMAssistant(..., **recorder.assistant_kwargs())`. A `SecurityMiddleware`
   (Host allowlist of `127.0.0.1`, `localhost` and `extra_hosts`; Origin check on
   writes; a strict CSP and security headers everywhere, relaxed only on the API
   docs); an optional bearer token; with `enable_mcp=True` (only `web.py` sets it)
   and `LUCID_MCP_ENABLED`, one Lucid session for the server's lifetime, never per
   conversation; the routes under `/api/v1`; and SSE through
   `anyio.to_thread.run_sync(…, abandon_on_cancel=True, limiter=SSE_LIMITER)`, so
   an open stream never blocks the event loop.
6. **The page**, which you write: `➕ pmagent/web/static/index.html`, `app.css`
   and `app.js`. A chat window and a thinking-chain window; a reply rendered as
   Markdown; an approval card showing the exact `describe_write` text with Reject
   and Approve (nothing approved by default); a token prompt on a 401; and after a
   reload, the chat rebuilt from `GET …/turns`. Rules: **no inline `<script>`, no
   `style=` and no `on*=` attributes** (the CSP forbids them); model output is
   rendered with `marked` and then sanitised with `DOMPurify`, with `img`
   forbidden. Download the browser builds that define globals yourself, for
   example `marked@15.0.12/marked.min.js` and `dompurify@3.2.6/dist/purify.min.js`
   from the npm CDN, into `➕ pmagent/web/static/vendor/`, and record each file's
   version, source URL and SHA-256 in `➕ vendor/README.md`.
7. `➕ web.py`: `build_server(host, port, allowed_hosts)` and `main(argv)` with
   `--host` (default `127.0.0.1`), `--port` and `--allowed-host`. Like `main.py` it
   first runs `env.validate()` and `validate_jira()`. Then it refuses a
   non-loopback host without `PMAGENT_API_TOKEN`, and `0.0.0.0` without an
   `--allowed-host` (the wildcard is a bind address, never a Host name). The
   banner shows `localhost` for a wildcard bind.
8. `➕ scripts/__init__.py` (empty) and `➕ scripts/demo_web.py`: the web UI
   offline. Run as a script, it first puts the project root on `sys.path` (a
   script's own folder is what Python adds). A `DemoJira` fake client (`search_issues_page`, `add_comments`, a
   class-level `comments` list), `configure()` pointing the Jira tools'
   `get_client` at it, `factory(recorder)` returning a `PMAssistant` with a
   `ScriptedModel` that answers three prompts ("Is the FX epic at risk?" →
   reasoning and a tool call; "Post a comment on DEMO-101" → the approval card;
   "Write a PRD from these notes: …" → the writer/reviewer loop) and a fallback,
   and `main()` with `--host` and `--port`.

**Your tests (`➕ tests/test_web_api.py` with FastAPI's `TestClient`,
`➕ tests/test_web_entry.py`, `➕ tests/test_web_concurrency.py`) must cover:**

| Area | Cases |
|---|---|
| API | create, get, delete a conversation; a turn is accepted (202 + Location) and streams its chain; approve resumes the same turn and runs the write once; reject never runs it; the approval text is exactly the CLI's; two approvals in one turn get distinct ids; a message while an approval waits is a 409 naming it; decisions are idempotent but can't be changed; a turn while one runs is a 409, and delete waits; an idempotent retry returns the same turn and runs the model once; the PRD loop appears in the chain |
| failures | a failed resume abandons the approval and the conversation keeps working; a failure with an approval pending marks it abandoned; delete abandons it; a context overflow is its own problem type; a crash while finishing fails the turn instead of leaving it running; a decision racing `approval.required` isn't overwritten |
| input | invalid messages are 422 problems; a `text/plain` body is refused; problem types resolve to a page |
| security | the bearer token when configured; a foreign Host is refused (DNS rebinding); a foreign Origin can't approve; security headers strict on the page and API, relaxed only on docs, and present on the middleware's own refusals |
| page | served at `/`; no inline script, style or handlers; the vendored files match their recorded hashes; the OpenAPI document lists every route |
| entry | loopback answers loopback names only; a wildcard bind uses the allowed names; unsafe binds are refused |
| concurrency | with a real uvicorn server, an open event stream doesn't block other requests |
| demo | the three demo prompts and the fallback |

**Checkpoint R7:** your tests pass, and `uv run scripts/demo_web.py` serves the
offline demo at http://127.0.0.1:8000. Click through all three prompts.

## R8 — First live run, safely (2–3 h)

Before your app ever touches a real Jira, write the launcher that makes a write
impossible.

1. `➕ scripts/smoke.py`: the real CLI (or with `--web`, the web UI via
   `web.main`), with **every HTTP write blocked below the app**. Every request
   the domain code makes ends in `requests.Session.request`, so wrap that one
   method with a **fail-closed allowlist**:

   | Request | Verdict |
   |---|---|
   | `GET`, `HEAD`, `OPTIONS` | allowed |
   | `POST …/rest/api/3/search/jql` | allowed (Jira's search is a POST) |
   | `POST login.microsoftonline.com/…/oauth2/v2.0/token` | allowed (token refresh) |
   | anything else | print `[smoke] BLOCKED <METHOD> <path>` in red and raise `RuntimeError("smoke: write blocked")` |

   Install the block, and set `LUCID_MCP_ENABLED=false` (Lucid's client doesn't
   use `requests`), **before** anything imports `pmagent`; then put the project
   root on `sys.path` (running `scripts/smoke.py` adds only `scripts/`). Before
   handing over, run a self-check: a blocked POST must raise, or the launcher
   refuses to start. The FY converter
   writes local files, so replace `convert_fy_budget` in the FY `pipeline`
   module with a function that raises the same error. The LLM SDKs use `httpx`, so model
   calls are unaffected.
2. `cp .env.sample .env`, then fill in an LLM key and Jira credentials.
3. `uv run scripts/smoke.py` (or `--web`). Ask about a sprint, ask for a comment,
   press **y**, and watch the `[smoke] BLOCKED POST …` line prove nothing was
   written.

**Your tests (`➕ tests/test_smoke.py`) must cover:** a GET passes, the JQL search
POST passes, the token POST passes, a comment POST raises and is reported, a PUT
and a DELETE raise, and the block is in place before `pmagent` is imported (run the
launcher's setup in a subprocess and import a Jira tool after it). Break it: allow
every POST, and several cases should fail.

**Never run plain `main.py` or `web.py` against a production Jira to test a
write.** That rule is lesson 9's "live checks, safely".

## R9 — Your own MCP server: the sprint review (2–3 h)

Lesson 8's case study. R2 connected to a server someone else owns (Lucid). Here
you write the server, and a lane agent built with R4's `make_lane_agent` talks to
it through the gate.

```bash
mkdir -p docs/learning/examples/sprint_review
touch docs/learning/examples/sprint_review/__init__.py
```

- `➕ docs/learning/examples/_common.py`: the examples' helper. It puts the
  project root on `sys.path`; `LIVE = "--live" in sys.argv`; `model(script)`
  returns `get_model()` when live, else `ScriptedModel(script)`; `banner(title)`
  prints the example's title and which model it uses.
- `➕ docs/learning/examples/sprint_review/sample-jira.csv`, a synthetic export:

  ```text
  Issue key,Summary,Status,Assignee,Created,Resolved
  DEMO-1,Build DimCustomer,Done,Alex Smith,01/Sep/26 9:00 AM,04/Sep/26 3:00 PM
  DEMO-2,Build FactSales,In Progress,Blair Jones,02/Sep/26 9:00 AM,
  DEMO-3,Review mapping,In review,Casey Brown,03/Sep/26 9:00 AM,
  DEMO-4,Confirm source dependency,Blocked,Drew Green,04/Sep/26 9:00 AM,
  DEMO-5,Prepare metadata,To Do,Alex Smith,05/Sep/26 9:00 AM,
  DEMO-6,Load product feed,Done,Emery White,30/Aug/26 9:00 AM,06/Sep/26 3:00 PM
  ```

  With `sprint_start=2026-09-01` and Alex's unfinished cards excluded, it must
  give: 6 exported, 5 included, 2 Done, 2 in active flow, 1 Blocked, 80% of 5
  started, 4 created since the start, 2 resolved since the start.

1. `➕ docs/learning/examples/sprint_review/analyser.py`, which is plain Python
   with no Strands and no MCP:
   - `normalize_status`: the six standard statuses (`Done`, `In review`,
     `In Progress`, `Blocked`, `To Do`, `Duplicate`) matched case-insensitively
     and returned in that spelling. Anything else is trimmed and **kept**.
   - `parse_jira_date`: day-first dates (`01/Sep/26 9:00 AM`, `14/09/2026`,
     `14 Sep 2026`, with 12- or 24-hour times), then month-first, then ISO 8601
     (offset dropped). `None` when it can't parse.
   - `analyse(csv_path, *, sprint_start=None,
     exclude_unfinished_assignee_first_names=(), treat_as_in_progress_issue_keys=())`.
     Read the file **once**: hash those bytes, and decode them as `utf-8-sig` with
     `errors="replace"`. Required columns are `Issue key`, `Summary` and `Status`;
     `Assignee`, `Created` and `Resolved` are optional. It returns:

     ```text
     schema_version  "sprint-review-analysis.v1"      generated_at_utc  ISO timestamp
     source   csv_path, sha256
     filters  sprint_start ("YYYY-MM-DD" or None),
              excluded_unfinished_assignee_first_names, user_confirmed_in_progress_issue_keys
                (sorted; the first spelling given is kept),
              rule = "Exclude matching assignees only when status is not Done; retain their Done cards."
     totals   exported, included, excluded, status_sum
     metrics  status_counts (standard statuses in order, then others case-insensitively),
              done, active_flow, in_review, in_progress, blocked, to_do, duplicate,
              started_work, done_or_active_pct_of_started,
              created_since_sprint_start, done_resolved_since_sprint_start,
              unreadable_date_issue_keys   (these three are None without a sprint_start)
     evidence included_issues, excluded_issues: one dict per row with key, summary,
              status, source_normalized_status, normalized_status, assignee, created,
              resolved, excluded, exclusion_reason, status_adjustment
     ```

     `sprint_start` is a `datetime.date` (the server converts the ISO string);
     the date windows are inclusive (`>=` midnight of the start). An override
     applies only to a card that isn't `Done` and isn't excluded.
     `unreadable_date_issue_keys` checks `Created` on every included card and
     `Resolved` on `Done` cards only; a blank date is not unreadable. Unknown statuses are grouped
     case-insensitively under the first spelling seen, then sorted by
     `casefold()`. `CsvError` is also raised when `status_sum != included` (a
     belt-and-braces check). `active_flow` = In review + In Progress; `started_work` = Done +
     `active_flow` + Blocked; `done_or_active_pct_of_started` = `round(100 * (Done
     + active_flow) / started_work)` (Python's `round` is banker's, 62.5 → 62), or
     0 when nothing has started. `status_counts` leaves out zero counts. An
     excluded assignee keeps their `Done` cards; an excluded card can't be
     rescued by an override. `exclusion_reason` is `"Unfinished item assigned to
     excluded first name '<name>'"`, with the name as the CSV spells it; `status_adjustment` is `"User-confirmed as In
     Progress for this review."`; both `""` otherwise.
   - `CsvError(ValueError)`: `"CSV is empty: …"`, `"CSV is missing required
     column(s): …"`, `"No issue rows found in …"`.
2. `➕ docs/learning/examples/sprint_review/server.py`, with
   `from mcp.server.mcpserver import MCPServer`,
   `from mcp.server.mcpserver.exceptions import ToolError` and
   `from mcp.types import ToolAnnotations`:
   - `RefusedError(ToolError)`: a `ToolError`'s message reaches the model; any
     other exception reaches it only as "Error executing tool …".
   - `resolve_csv(root, name)`, where `root` is already resolved: resolve the path
     first (following `..` and symlinks), then refuse it if it is outside the
     root, not a `.csv` (any case), or not a file. An `OSError`/`ValueError` from
     the path itself is refused as "not a usable file name".
   - `list_exports(root)`: exactly the names `resolve_csv` accepts, in subfolders
     too, sorted.
   - `run_analysis(root, csv_name, sprint_start=None, …, include_evidence=True)`:
     a non-ISO `sprint_start` is refused with a message containing `YYYY-MM-DD`; a
     `CsvError` or `csv.Error` becomes "can't be analysed", an `OSError` "can't be
     read"; `source.csv_path` becomes the root-relative name.
   - `save_run(root, run_name, analysis)` writes `runs/<run_name>/metrics.json`
     and returns that relative path. A run name is 1–64 characters of
     `[A-Za-z0-9._-]`, starting with a letter or digit. The run folder is created
     with `mkdir()` without `exist_ok` (atomic), so an existing run is refused
     ("already exists"); a `runs` that is a symlink or a file is refused ("real
     folder").
   - `create_server(root)` resolves `root` once and registers
     `list_sprint_exports()`; `analyze_sprint(csv_name, sprint_start=None,
     exclude_unfinished_assignee_first_names=None,
     treat_as_in_progress_issue_keys=None, include_evidence=True)` (annotated
     read-only); and `save_metrics(run_name, csv_name, sprint_start=None, …the same
     filters)` (annotated as a write), which runs the analysis itself and returns
     `{"saved", "sha256", "totals"}`. `main()` parses `--root`, refuses one that
     isn't a folder, and calls `.run()`.
     Never `print()` in the server: stdout is the protocol.
3. `➕ docs/learning/examples/08_sprint_review_mcp.py`: copies the sample into a
   temporary root, starts the server with
   `StdioServerParameters(command=sys.executable, args=["-m",
   "sprint_review.server", "--root", root], cwd=<the examples folder>)`, lists its
   tools, and prints each tool's read-only hint (`t.tool_spec["annotations"]
   ["readOnlyHint"]`; on the mcp object it is `.read_only_hint`) next to whether
   your gate would gate it (all three: annotations are hints, not trust). It then runs a lane
   agent from `make_lane_agent` with a scripted model: analyze, save as
   `sprint-35`, save `sprint-35` again, then an answer that quotes the server's
   numbers. It approves each pause and prints what came back: two successes, then
   an error saying the run already exists.

**Your tests (`➕ tests/test_sprint_review.py`) must cover:** the sample's eight
figures above; excluding an assignee drops only their unfinished cards; no filters
include every row, with no date metrics; unknown statuses kept, counted, listed
after the standard ones; an override counts as In Progress but keeps its source
status; 62.5 → 62; nothing started → 0; the hash is of the analysed bytes (read
once); a BOM, a quoted comma and missing optional columns; a non-UTF-8 file read
with replacement; an excluded card not rescued; the first spelling kept; unreadable
dates listed; each date format; bad dates → `None`; each unusable export; each
refusal of `resolve_csv` including a symlink out; exports listed exactly; results
cite the name, not the host path; bad input and an oversized field or unreadable
file refused; a saved run never overwritten; a `runs` symlink or file refused;
bad run names refused; and the example, run in a subprocess, printing three pauses,
the refusal and the figures.

**Checkpoint R9:** your tests pass, and `uv run
docs/learning/examples/08_sprint_review_mcp.py` shows the three pauses, the
refusal and the answer. Break it: give the run folder's `mkdir()` `exist_ok=True`,
and see which tests catch the overwrite.

## R10 — Package it: the Docker image (2–3 h)

The project ships as one Docker image, and the image is where "offline first"
and "never bake in a secret" become mechanical. You need R7, R8 and R9.

1. `➕ scripts/container.py`, the entrypoint. `command(argv)` maps a name to
   `(program + args, working folder)`:
   - `demo` (the default) runs the offline demo on `0.0.0.0`. A container's
     `127.0.0.1` can't be reached from the host, and the demo holds no
     credentials, so this is safe.
   - `web` and `smoke-web` run `web.py` on `0.0.0.0` with `--allowed-host
     localhost`, plus any names in `PMAGENT_ALLOWED_HOSTS` (comma-separated).
     `web.py`'s own rule still applies: no `PMAGENT_API_TOKEN`, no start.
   - `cli` and `smoke` run the CLI.
   - `sprint-review-mcp` runs R9's server over stdio, with its root at
     `SPRINT_REVIEW_ROOT` (default `data/sprint-review` in the project), creating
     the folder first.
   - `examples` (handled in `main()` rather than `command()`) runs every
     numbered example except those in `NEEDS_EXTRAS` (examples that need extra
     packages; in your rebuild that set can be empty), and exits non-zero if any
     fails.
   - Anything else is exec'd as given. `main()` `exec`s the command, so it
     receives `docker stop`'s signal; if no such program exists (the exec raises
     `FileNotFoundError`), it exits with `Unknown command '<x>'. Commands: …`
     instead of a traceback.
   - `APP` is the project folder, derived from `__file__` (never a hard-coded
     `/app`, because your tests run it from your checkout); `PORT` comes from the
     environment, default `8000`.
2. `➕ .dockerignore` is an **allowlist**: `*` first, then `!` for each path the
   image needs (`pyproject.toml`, `uv.lock`, `.python-version`, `main.py`,
   `web.py`, `pmagent/`, the scripts the entrypoint runs, the examples folder, and
   the scripted model in `tests/`), then `**/__pycache__/` and `**/*.py[cod]`. A denylist would let
   the next file someone drops in the folder, `.env` or a zip of real data, into
   the image.
3. `➕ Dockerfile`, in two stages. The build stage copies `uv` from its official
   image and runs `uv sync --locked --no-dev` from the lock file alone, then copies
   the app. The final stage creates a non-root user; the code stays root-owned
   (read-only); only `/app/data` is writable, and `HOME` points into it. There is
   no `VOLUME` line (it would leave an anonymous volume behind every run).
   `ENTRYPOINT ["python", "scripts/container.py"]`, `CMD ["demo"]`, and
   `ARG PYTHON_VERSION` matches `.python-version`.
4. `➕ compose.yaml` (optional): a `demo` service, and a `web` service under a
   `live` profile with `env_file: .env`, a named data volume and a health check.

**Your tests (`➕ tests/test_docker.py`) must cover:** the app's files are in the
build context and `.env`, `data/`, zips, tests other than the scripted model and
stray files are not (implement Docker's rule: the last matching pattern wins, `*`
stays within one path segment, `**` spans any); the Dockerfile's promises
(`--locked --no-dev`, a non-root `USER`, the entrypoint, `CMD ["demo"]`, no
`VOLUME`, no `COPY` of `.env`, the Python version); `examples` covers every
example but the extras; a mistyped command lists the commands; the MCP command
creates its root; each command's arguments; the web commands allow `localhost` plus
the configured hosts; `web` in a container still refuses to start without a token;
and the default command, started for real in a subprocess, answers
`/api/v1/health`.

**Checkpoint R10:** your tests pass (no Docker needed for them). Break it:
delete the leading `*` from `.dockerignore`, and the "not in the image" cases
should fail. Then the live check, if you have Docker:

```bash
docker build -t my-po-agent . && docker run --rm --init my-po-agent examples
docker run --rm --init -p 127.0.0.1:8000:8000 my-po-agent     # the demo
```

## You have rebuilt it — what next

That is everything: the domain layer, the agents, both frontends, an MCP server
and the image, every file written by you, every behaviour covered by a test you
wrote.

Part 3 (lessons 12–17) adds what the project has *designed* but not built:
logging, persistence, long-term memory, evals, a Claude Code harness and Jev. You
build those on top of your rebuild.
