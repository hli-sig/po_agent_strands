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

Four files, in the order you need them: a fake model your tests will run against (you can't hit a real LLM offline), the one place a real model gets built, and two tools that need a model at runtime — one calling a server you don't own.

#### `➕ tests/fakes.py` [lesson 9] — a fake Strands `Model` that replays scripted replies, so every later test drives the real agent loop with no network and no API key

A Strands `Model` is any object with `update_config`, `get_config`, `structured_output` and `stream`. Only `stream` matters here — Strands' event loop calls it, assembles the events it yields into an assistant message, runs any tool calls, and calls it again. A fake that just replays a fixed script of replies through that same interface exercises the real loop, the real hooks, and the real interrupt machinery; only the model's "choices" are canned.

**Block helpers** — each returns one block dict, ready to drop into a scripted turn:
- `text(value) -> {"text": value}` — a plain assistant reply.
- `reasoning(value) -> {"reasoningContent": {"text": value}}` — a reasoning block, the kind a thinking model streams before its answer.
- `call(name, **args) -> {"toolUse": {"name": name, "toolUseId": "tooluse_<n>", "input": args}}`, with `<n>` a fresh number every time (a module-level counter).
  - **Why a fresh id:** every `toolUse` needs a `toolUseId` unique within the conversation, because the matching `toolResult` references it by that id (lesson 4). Reusing an id would silently pair a result with the wrong call.
- `structured(schema, **fields) -> call(schema.__name__, **schema(**fields).model_dump(by_alias=True))`.
  - **Why:** structured output is never a special reply type — Strands gets it by adding a hidden tool named after the schema and waiting for the model to call it (lesson 3). So the only way to script a structured reply is to script a tool call to that hidden tool, built by constructing the real Pydantic object and dumping it back to a dict.

**`ScriptedModel(turns=None)`**
- **Does (constructor):** stores `turns` as a list (each entry is *one model call's* reply), an empty `requests` list, and `config = {"model_id": "scripted"}`.
- `update_config(**kw)` / `get_config()`: trivial dict passthrough — present only because the `Model` interface requires them; nothing in this course reads them back.
- `structured_output(...)`: raises `NotImplementedError`.
  - **Why:** Strands never calls this method for `structured_output_model=` — see `structured()` above. If you find yourself calling it, script a `structured()` reply instead.
- **`stream(messages, tool_specs=None, system_prompt=None, **kwargs)`** — an async generator; this is the method Strands actually drives. Steps, in order:
  1. Append one record to `self.requests`: `{"system_prompt": system_prompt, "tools": [spec["name"] for spec in tool_specs or []], "messages": copy.deepcopy(messages)}`.
     - **Why deep-copy:** `messages` is the live conversation list; Strands keeps mutating it after this call returns. Recording a snapshot lets a test later assert what the model was shown *at the time*, not what the list looks like now.
  2. If `self.turns` is empty, raise `AssertionError("ScriptedModel ran out of scripted turns")`.
     - **Why:** a test that runs out of scripted replies should fail loudly and immediately, not hang or silently return nothing.
  3. Pop the next turn off the front of the list (`self.turns.pop(0)`). If it's callable, call it with `messages` and use the returned list of blocks; otherwise the turn *is* the list of blocks.
     - **Why a callable turn:** most scripted replies don't care what's in the conversation, but occasionally a test needs a reply that depends on it (e.g. echoing back an id the model was just given). A plain list can't do that; a function can.
  4. Yield `{"messageStart": {"role": "assistant"}}`.
  5. For each block in the turn's blocks, yield one `contentBlockStart` / `contentBlockDelta` / `contentBlockStop` triple:
     - a `reasoningContent` block → delta `{"reasoningContent": {"text": ...}}`;
     - a `text` block → delta `{"text": ...}`;
     - a `toolUse` block → `contentBlockStart` carries `{"toolUse": {"name", "toolUseId"}}`, and the delta is `{"toolUse": {"input": json.dumps(use["input"])}}` — the arguments as a **JSON string**, not a dict, because that's the wire shape Strands' real parser expects to accumulate and decode. Track that at least one block was a tool call.
  6. Yield `{"messageStop": {"stopReason": "tool_use" if any block was a call else "end_turn"}}`.
  7. Yield one `{"metadata": {"usage": {...all zero...}, "metrics": {"latencyMs": 0}}}` block. The numbers are zero because nothing real happened; only their *shape* has to match what Strands expects.
- **`remaining` property:** `len(self.turns)` — lets a test assert every scripted reply actually got consumed (a leftover turn usually means the code under test made fewer model calls than the test expected).

#### `➕ pmagent/llm.py` [lesson 1, lesson 3] — the one place a model provider gets chosen, plus the one helper for stateless structured-output calls

**`build_model() -> Model`**
- **Does:** branches on `env.LLM_PROVIDER`:
  1. `"anthropic"` → lazily import `AnthropicModel`; return `AnthropicModel(client_args={"api_key": env.ANTHROPIC_API_KEY}, model_id=env.LLM_MODEL, max_tokens=env.LLM_MAX_TOKENS)`.
  2. `"openai"` → read `effort = env.LLM_REASONING_EFFORT`.
     - If `effort == "none"`: lazily import `OpenAIModel`; return it with `params={"reasoning_effort": "none", "temperature": 0.1}`.
     - Otherwise: lazily import `OpenAIResponsesModel`; build `reasoning = {"effort": effort}`, adding `reasoning["summary"] = env.LLM_REASONING_SUMMARY` only when that setting is non-empty; return `OpenAIResponsesModel(..., params={"reasoning": reasoning})`.
  3. Anything else → `raise ValueError(f"Unknown LLM_PROVIDER={env.LLM_PROVIDER!r}. Use 'anthropic' or 'openai'.")`.
- **Why lazy imports:** each provider needs its own SDK installed; importing only the branch you take means you never need the SDK for a provider you don't use.
- **Why the OpenAI split is mandatory, not stylistic:** gpt-5.x rejects function tools on the `/v1/chat/completions` endpoint unless `reasoning_effort` is explicitly sent as `"none"` — verified live as an HTTP 400 ("Function tools with reasoning_effort are not supported ..."). Every lane binds tools, and structured output is itself a hidden tool call, so this app always needs tools to work — the `"none"` branch is not optional. Sending `reasoning_effort` at all also happens to re-enable `temperature` on gpt-5 (a side effect worth knowing, not a feature to rely on).
- **Watch out:** do not add `max_tokens` to the `"none"`-effort `OpenAIModel` branch — gpt-5 rejects that parameter on this endpoint. Any real reasoning effort needs the Responses API (`OpenAIResponsesModel`) instead, because only it supports tools and reasoning together.

**`get_model() -> Model`** — decorated `@functools.cache`
- **Does:** returns `build_model()`, computed once and cached for the lifetime of the process.
- **Why:** correct for a single long-lived CLI process. It is **not** what the web server uses per conversation (R7): the Anthropic model object holds one async HTTP client, and that client must not be shared across the event loops of multiple concurrently running conversations. The web server calls `build_model()` fresh instead.

**`reasoning_available() -> bool`**
- **Does:** returns `True` only when all three hold: `env.LLM_PROVIDER == "openai"`, `env.LLM_REASONING_EFFORT != "none"`, and `env.LLM_REASONING_SUMMARY` is set.
- **Why:** this is the single check other code uses to decide whether there's any reasoning stream worth showing at all (Anthropic's models here never stream reasoning; OpenAI only does with a non-`"none"` effort and a summary mode configured).

**`structured(schema, prompt, *, system_prompt=None, model=None, usage_sink=None) -> T`**
- **Does:**
  1. Build a **brand-new** `Agent` for this call alone: `model=model or get_model()`, `system_prompt=system_prompt`, `messages=[]` (empty — no shared history), `callback_handler=None`, and no `hooks=` argument at all.
  2. Call `agent(prompt, structured_output_model=schema)`.
  3. If `usage_sink` was given, call it with `invocation_usage(result)`.
  4. Return `result.structured_output`.
- **Why a fresh agent every call, not a shared one:** structured output only exists as a hidden tool call inside *some* agent's conversation (lesson 3). Everything about this agent is built to keep that hidden call from leaking anywhere real: `callback_handler=None` stops it from being printed to the CLI; the empty `messages=[]` keeps it out of the shared lane history a user is actually reading; no hooks means no approval gate runs on it, because there is nothing here a human needs to approve.
- **Watch out:** never call `structured()` against a lane's own long-lived `Agent` — that agent already has hooks and shared history, and you'd break exactly the guarantee above.

**`invocation_usage(result) -> dict`**
- **Does:** reads `result.metrics.latest_agent_invocation`; if it exists, takes its `.usage` dict, else uses `{}`. Returns `{"input_tokens": usage.get("inputTokens", 0), "output_tokens": usage.get("outputTokens", 0), "total_tokens": usage.get("totalTokens", 0), "model_calls": len(invocation.cycles) if invocation else 0}`.
- **Why `latest_agent_invocation` and not `accumulated_usage`:** `result.metrics.accumulated_usage` is a running total over the *whole life* of the agent object; since `structured()` builds a fresh agent every time, that would happen to be correct there too, but on a long-lived lane agent it would keep growing across turns. Reading the *latest invocation* instead always gives you just this one call's numbers, which is what a token-usage-per-turn UI (R7) needs.

#### `➕ pmagent/tools/diagram_tools.py` [lesson 2, lesson 3] — turns an approved PRD into a plain-language diagram brief; never talks to Lucid itself

This tool's whole job stops one step short of Lucid: it produces the text a human will later approve and hand to Lucid's own `create_diagram` MCP tool (next file). Keeping that call out of this module is deliberate — it's the boundary between "the model decides what to diagram" and "a human confirms it gets created."

- **`_DIAGRAM_BRIEF_ROLE`** — a module-level string constant: the system-prompt text that tells the model to pick exactly one `diagram_type` (`flowchart`, `entity_relationship`, `architecture`, or `sequence`), invent no node or edge that isn't explicitly in the source text, and write `description` as plain English naming every node and how it connects — written so someone with *no other context* could hand it to a diagramming tool and get the right picture.

**`_build_prompt(prd_text, reporting_intent_text="") -> str`**
- **Does:** joins `[_DIAGRAM_BRIEF_ROLE, f"PRD:\n{prd_text}"]` with blank lines, and appends a `f"Reporting Intent brief:\n{reporting_intent_text}"` section **only** when `reporting_intent_text` is non-empty.
- **Watch out:** the "no brief given" signal is an empty string, not `None` — pass `""`, not `None`, when there isn't one.

**`render_diagram_brief(brief: DiagramBrief) -> str`**
- **Does (plain Python, no LLM call):** builds and joins these lines in order — `Diagram type: <type>`, `Title: <title>`, a blank line, `brief.description`, a blank line, `Nodes:` followed by one `- <id>: <label>` line per node, a blank line, `Edges:` followed by one `- <from> -> <to>` line per edge, with a trailing ` (<label>)` appended only when that edge has a label.
- **Why this is deterministic Python and not the model's job:** the same recurring rule as everywhere else in this app — the LLM does judgment (what belongs on the diagram), and plain code does formatting, so the exact same `DiagramBrief` always renders to the exact same text. This rendered text is also literally what later gets passed as Lucid's `create_diagram` `description` argument, so it needs to be stable and predictable, not restyled by the model on every call.

**`draft_diagram_brief(prd_text, reporting_intent_text="", tool_context=None) -> str`** — decorated `@tool(context=True)`
- **Does:**
  1. Read `tool_context.agent.model` if a `tool_context` was passed, else `None`.
  2. Read `tool_context.invocation_state.get("usage_sink")` the same way (empty dict, so `None`, if there's no context).
  3. Call `structured(DiagramBrief, _build_prompt(prd_text, reporting_intent_text), model=<that model>, usage_sink=<that sink>)`.
  4. Return `render_diagram_brief(brief)`.
- **Why `@tool(context=True)`:** it makes Strands pass a `ToolContext` argument — invisible to the model, so it never appears in the tool's schema — carrying the calling agent, the raw `tool_use` block, and the invocation state. Reading `tool_context.agent.model` means this brief always runs on **whatever model called it**: a lane's real provider in production, or a test's `ScriptedModel` — with no extra plumbing to wire that up per caller.
- **`READ_TOOLS = [draft_diagram_brief]`, `WRITE_TOOLS = []`.** It calls an LLM and renders text; it creates nothing and touches no file, so it's a read. (The Lucid tools this same lane also binds can't be declared here at all — they're discovered at runtime. That's the next file.)

#### `➕ pmagent/tools/mcp_tools.py` [lesson 8] — connects to an MCP server you don't own (Lucid), and hands its tools to the diagram lane, gated closed by default

`MCPClient` runs its session on a background thread, so once it's started, calling its methods from the rest of this synchronous app just works — no `async`/`await` needed anywhere else. The lifecycle this file wraps is meant to be this simple to read:

```python
session = start_lucid()   # start() + list every page of tools, or None on failure
tools   = session.tools   # give THESE to the Agent — never session.client
...
session.close()           # stop the background thread at process exit
```

- **`APPROVED_READ_TOOLS: frozenset[str] = frozenset()`** — starts empty, on purpose. A tool discovered at runtime can't be listed in a module's `READ_TOOLS` the way a local `@tool` can, so the approval gate (R3) treats *every* Lucid tool as a write by default. A tool's name only belongs in this set after a human has actually read what it does and added it here by hand — the gate fails **closed**, never open, on anything nobody has reviewed.

**`MCPSession`** — a `@dataclass` with two fields, `client` and `tools` (the started client, and the list of tools it discovered).
- **`close(self) -> None`**
  - **Does:** calls `self.client.stop(None, None, None)`.
  - **Why three `None`s:** `MCPClient.stop` *is* the client's context-manager `__exit__` method, and `__exit__` always takes three positional arguments — exception type, value, traceback — with no defaults. Calling it directly (instead of through a `with` block) means you must supply all three yourself, even though nothing failed.

**`_list_all_tools(client) -> list`** — a private helper (leading underscore), called only from `start_lucid`.
- **Does, step by step:**
  1. Start with an empty list and `token = None`.
  2. Call `client.list_tools_sync(pagination_token=token)` to get one page.
  3. Extend the running list with that page's tools.
  4. Set `token = page.pagination_token`.
  5. If `token` is `None`, return the accumulated list. Otherwise, go back to step 2.
- **Why:** `list_tools_sync` is paginated — one call only returns the first page. A server exposing more tools than fit on one page would otherwise look like it had fewer tools than it does, silently.

**`start_lucid() -> MCPSession | None`**
- **Does, step by step:**
  1. Import `MCPClient` from `strands.tools.mcp` (lazily — this import only matters when Lucid is actually enabled).
  2. Build `headers`: `{"Authorization": f"Bearer {env.LUCID_MCP_AUTH_TOKEN}"}` if that setting is non-empty, else `None`.
  3. Construct `client = MCPClient(url=env.LUCID_MCP_URL, headers=headers)` — this does not start anything yet.
  4. Inside a `try`: call `client.start()` explicitly, then return `MCPSession(client=client, tools=_list_all_tools(client))`.
  5. In the matching `except Exception`: print a warning that names the exception's type and message and says the diagram lane will continue with its local tool only; then, in a nested `try`/`except` that swallows any further error, call `client.stop(None, None, None)` so a half-started background thread doesn't leak; finally return `None`.
- **Why fail this soft:** `start_lucid()` is only called when `LUCID_MCP_ENABLED` is true (R6's `main.py` decides that — this file doesn't check the flag itself). Lucid is one optional lane's nice-to-have, not a dependency the whole app needs to boot; an unreachable server, a bad token, or any other failure must degrade to "the diagram lane drafts briefs but can't create diagrams," never crash startup.
- **Watch out — pick exactly one owner:** Strands also lets you skip all of this and pass an *un-started* `MCPClient` straight into `Agent(tools=[client])`; the agent will then start and stop it for you. But if you call `client.start()` yourself here **and** also hand that same client to an `Agent`, the agent tries to start it a second time and fails with "the client session is currently running." This file picks the app as the one owner — which is why `start_lucid()` returns `session.tools` (a plain list of already-discovered `MCPAgentTool` objects) for the `Agent` to bind, and `session.client` is only ever touched by `close()`.
- **Watch out — the auth gap is real, not a bug to fix:** Lucid's default auth is per-user OAuth (Dynamic Client Registration), which would need `MCPClient(auth_provider=...)`. Only a static bearer token (`LUCID_MCP_AUTH_TOKEN`) is wired here; that limitation is deliberate and documented, not an oversight to silently work around.

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

#### `➕ pmagent/messages.py` [lesson 4] — small readers over the Bedrock Converse message shape

A Strands conversation is a list of dicts: `{"role": ..., "content": [blocks]}`. This
module never builds or mutates that list — it only reads it, so every other module
that needs to ask "what did the human say?" or "what did this tool return?" goes
through here instead of poking at `content` blocks directly.

**`blocks(message) -> list[dict]`**
- **Does:** returns `message["content"]`, or `[]` if the key is missing/`None`.

**`text_of(message) -> str`**
- **Does:** joins every block's `"text"` value with a space, then strips the result.
- **Watch out:** a tool call, a tool result and an image block have no `"text"` key
  and are silently skipped — this only ever returns prose.

**`tool_uses(message) -> list[dict]`** / **`tool_results(message) -> list[dict]`**
- **Does:** each returns the `"toolUse"` (or `"toolResult"`) value of every block
  that has one, in order.

**`is_tool_result(message) -> bool`**
- **Does:** `True` when the message's role is `"user"` **and** `tool_results(message)`
  is non-empty.
- **Why:** in Strands, a tool result is delivered as a `role: "user"` message, not a
  separate role. Without this check, "the last user message" would sometimes mean
  "what a tool returned," not "what the human typed."

**`last_user_text(messages) -> str`**
- **Does:** walks `messages` from the end backwards; returns the first message that
  is role `"user"` **and not** a tool result (via `is_tool_result`), then its
  `text_of(...)`; `""` if nothing matches or the text is empty.
- **Watch out:** this is the one function in the whole codebase that answers "what
  did the human actually ask for" — the router (R4) depends on it not being fooled
  by tool output.

**`user_message(text) -> dict`** / **`assistant_message(text) -> dict`**
- **Does:** build `{"role": "user"|"assistant", "content": [{"text": text}]}`.
  Trivial constructors — write them last if it helps, but other steps import
  them by these exact names.

**`result_text(result) -> str`**
- **Does:** takes a `toolResult` dict, walks its `content` blocks, and joins one
  line per block: a `"text"` block as-is; a `"json"` block as `str(...)`; a
  `"document"` block as the literal string `[document]`; an `"image"` block as
  `[image: <format>]` (e.g. `[image: png]`) — read the format from
  `block["image"]["format"]`, default to `"?"` if it's missing.
- **Why:** this is what the CLI and web UI show for a tool's output. An image
  can't be printed to a terminal, so it gets a label instead of being dropped
  silently.

#### `➕ pmagent/gate.py` [lesson 6] — the approval gate: nothing that writes runs until a human says yes

This file has two jobs: (1) decide, for any tool name, whether a human must approve
it before it runs, and (2) a hook that actually pauses the agent when the model
asks for a gated call.

**Constants, built once at import time:**

- `TOOL_MODULES`: a tuple of the **six** modules that define `@tool`s —
  `tools/jira/tools_read.py`, `tools/jira/tools_write.py`,
  `tools/confluence_tools.py`, `tools/finance_tools.py`,
  `tools/spreadsheet_tools.py`, `tools/diagram_tools.py`. Import the two Jira
  submodules directly (not the `jira` package). Leave `mcp_tools` out — it
  defines no `@tool` itself; its reviewed tools are handled separately (below).
- `WRITE_TOOL_NAMES`: a hand-written `frozenset[str]` of every tool name that
  changes something outside this process — every Jira write, the spreadsheet
  writes, and `create_fy_budget_csv`.
  - **Why hand-written, not derived:** if it were computed from each module's
    `WRITE_TOOLS` list, adding a write tool would be a one-place change. Writing
    it out by hand makes it a **deliberate two-place** change (declare the tool,
    then separately name it here) that a test can reconcile — one accidental typo
    in either place fails a test instead of silently under-gating a write.
  - **Watch out:** `create_fy_budget_csv` belongs here even though it "just"
    writes a local file — that CSV is real financial data that gets ingested
    into Snowflake, so writing it is not a harmless preview.
- `READ_TOOL_NAMES`: a `frozenset[str]` built by union: every tool name in every
  `TOOL_MODULES` member's `READ_TOOLS` list, plus every name in
  `mcp_tools.APPROVED_READ_TOOLS`.
- `REJECTION_MESSAGE`: the fixed string the model sees after a human rejects a
  call — it should explain nothing was written and invite the model to ask what
  the human wants instead (exact wording is yours).
- `INTERRUPT_NAME = "approval"`, `APPROVE = "approve"`, `REJECT = "reject"`: the
  interrupt's name and the two answer strings. The CLI and the web API send
  `APPROVE`/`REJECT` back verbatim, so keep these as plain constants other code
  can import rather than inlining the strings elsewhere.

**`requires_approval(name, agent) -> bool`**
- **Does, in order:**
  1. If `agent` would not actually run a tool called `name` (see "has a tool"
     below), return `False`.
  2. If `name` is in `WRITE_TOOL_NAMES`, return `True`.
  3. Otherwise return `True` unless `name` is in `READ_TOOL_NAMES` — i.e. gate
     everything **except** a declared read.
- **Why step 1:** a call the agent can't run at all shouldn't pause for a human's
  approval — that would ask them to approve something that will fail regardless.
  This happens in practice: every lane shares one message history, so a read-only
  lane can "remember" a write tool it saw a different lane use earlier in the same
  conversation, without ever having that tool bound to it.
- **Why step 3 is phrased as "gate unless proven safe," not "gate if known
  unsafe":** this is the fail-closed rule. A tool discovered at runtime (an MCP
  server's tools, lesson 8) can't appear in any module's `READ_TOOLS`, so with this
  phrasing it is gated by default until a human reviews it and adds its name to
  `mcp_tools.APPROVED_READ_TOOLS`. Phrasing it the other way around — "gate only
  tools in a known-write list" — is exactly the bug this port is fixing: an
  unreviewed tool would run un-gated.
- **"Has a tool," precisely:** don't use `agent.tool_names` for this check. It
  omits a tool whose spec failed validation but that the agent's executor would
  still attempt to run — so checking `tool_names` could let an unapproved write
  slip through ungated. Check the same two places the executor itself consults
  before running a tool (Strands' `Agent.tool_registry`: its `dynamic_tools` dict
  and its `registry` dict). A name in either one counts as "has."

**`ApprovalGate` — a `HookProvider`, registered on every lane agent by
`pmagent/agents/common.py::make_lane_agent` (R4)**

- **Does:** registers one callback on `BeforeToolsEvent` — the event that fires
  after the model has asked for a batch of tool calls, before any of them run.
  The callback:
  1. Reads every `toolUse` block off the event's message.
  2. Filters to the ones where `requires_approval(call_name, event.agent)` is
     `True`. If none, return immediately — a pure-read batch never interrupts.
  3. Otherwise builds `reason = {"calls": [{"id": <toolUseId>, "name": ...,
     "args": <input>}, ...]}`, listing **only** the gated calls (not the whole
     batch — a read bundled with a write is still fully paused, but the human is
     only shown the part that needs a decision).
  4. Calls `answer = event.interrupt(INTERRUPT_NAME, reason=reason)`.
  5. If `answer != APPROVE`, sets `event.cancel = REJECTION_MESSAGE`.
- **Why the two-phase call matters:** `event.interrupt(...)` *raises* the first
  time it's called — Strands catches that, stops the agent loop, and returns an
  `AgentResult` with `stop_reason == "interrupt"`. Whoever is driving the agent
  (`PMAssistant`, R5) shows `reason` to a human, then resumes the same agent with
  an `interruptResponse` block carrying the human's answer. Strands re-fires
  `BeforeToolsEvent` for the *same* batch, and this time `event.interrupt(...)`
  **returns** the answer instead of raising. Your hook code doesn't need an
  if/else for "first call vs. resumed call" — that's handled for you by
  `interrupt` raising once and returning once.
- **Why `event.cancel`, not just returning:** setting `event.cancel` to a string
  cancels the *whole* batch — every gated tool call in it gets an error result
  equal to that string, and the model is invoked again so it can react to the
  refusal in its next reply. A no-op return would leave the batch to actually run.
- **Watch out:** the exact shape of `reason` (`{"calls": [{"id", "name", "args"},
  ...]}`) is a contract — the CLI's and the web API's approval-rendering code (R6,
  R7) read those three keys by name.

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

#### `➕ pmagent/agents/common.py` [lesson 1, lesson 5] — the one function that turns a `(prompt, tools)` pair into a real Strands `Agent`

**`make_lane_agent(name, system_prompt, tools, *, model=None, hooks=(), messages=None) -> Agent`**
- **Does:**
  1. Build and return `Agent(model=model or get_model(), name=name, system_prompt=system_prompt, tools=tools, hooks=[ApprovalGate(), *hooks], conversation_manager=NullConversationManager(), callback_handler=None, messages=messages or [])`.
- **Why:**
  - `ApprovalGate()` is always the *first* hook, and there is no parameter to leave it out — every lane must be gated, and putting the gate here instead of in each lane module makes that structural, not a rule someone has to remember.
  - `NullConversationManager()` keeps the whole conversation instead of Strands' default sliding window. A ticket batch gets checked against the list of issue keys the user typed at the start of the conversation; a sliding window could silently drop that message once the conversation grows, and the check would then compare drafts against nothing.
  - `callback_handler=None` because printing is the CLI's job (`ConsoleEcho`, lesson 5), not the agent's. R7 adds a `callback_handler` parameter here so the web server can pass its own streaming handler instead.
- **Watch out:** every one of the six lane-building functions below must call this — a lane built by hand with a bare `Agent(...)` has no gate.

#### The five lane declarations [lesson 2, lesson 7]

Every lane module is exactly two module-level constants: `SYSTEM_PROMPT` (a string) and `TOOLS` (a list of `@tool`-decorated functions). `make_lane_agent` does everything else. There is no per-lane logic to write here — the whole exercise is picking the right tool list and prompt.

**The rule that matters:** bind *exactly* the tools the lane's system prompt talks about — no more, no fewer. A tool bound but never mentioned in the prompt won't get used reliably; a capability mentioned in the prompt but not bound makes the model claim it can do something it can't. R6's tests check both directions (every named tool is bound, and vice versa), so get this list right now.

| Lane file | Tools bound | Notes |
|---|---|---|
| `➕ pmagent/agents/query_agent.py` | `query_jira_issues`, `read_jira_issue_details`, `read_jira_issues_by_key`, `list_jira_transitions`, `inspect_fy_budget_inputs`, `read_fy_budget_run`, `read_confluence_page`, `search_confluence` | The read-only catch-all: anything the router can't place lands here. Every read-only tool in the whole app belongs on this list too, not just the obvious "search" ones — a read withheld from this lane looks to the user exactly like a missing feature. Only writes are ever withheld. |
| `➕ pmagent/agents/ticket_agent.py` | `query_jira_issues`, `read_jira_issue_details`, `read_jira_issues_by_key`, `find_jira_user`, `list_jira_transitions`, `search_confluence`, `read_confluence_page`, `validate_ticket_drafts`, `create_jira_issues`, `assign_jira_issue`, `update_jira_issue`, `add_jira_comment`, `transition_jira_issues`, `move_jira_issues_to_sprint`, `remove_jira_issues_from_sprint` | Every Jira write except `create_jira_issue` (the single-issue form — this lane only uses the batch `create_jira_issues`). `SYSTEM_PROMPT` is built with the `ticket` skill injected (see D2/lesson 2 for skill injection), so the drafting standard lives in Markdown, not in this file. |
| `➕ pmagent/agents/sprint_agent.py` | `get_sprint_status_by_number`, `get_sprint_status`, `query_jira_issues`, `read_jira_issue_details`, `read_jira_issues_by_key`, `list_jira_transitions`, `add_jira_comment`, `transition_jira_issues`, `move_jira_issues_to_sprint`, `remove_jira_issues_from_sprint` | The prompt tells the model to reach for `get_sprint_status_by_number` — users say "sprint 31", never the internal Jira sprint id. The two sprint-membership tools live here (not only in the ticket lane) because scope changes ("move these into 31") come up naturally inside a sprint conversation. |
| `➕ pmagent/agents/finance_agent.py` | `inspect_fy_budget_inputs`, `create_fy_budget_csv`, `read_fy_budget_run` | The `fy_budget` skill is injected into `SYSTEM_PROMPT`. All the arithmetic lives in the FY budget tools themselves (D-chapter); the model's job is picking inputs and reading the validation verdict back honestly, never computing anything itself. |
| `➕ pmagent/agents/spreadsheet_agent.py` | `prepare_spreadsheet_approval_queue`, `propose_spreadsheet_cell_update`, `apply_approved_spreadsheet_updates` | No skill injection — the prompt alone is enough here. |

#### `➕ pmagent/agents/router.py` [lesson 3, lesson 4, lesson 7] — decides which lane handles the current turn

**`is_continuation(text: str) -> bool`**
- **Does:**
  1. Strip the text; if it's empty or longer than 60 characters, return `False`.
  2. Lower-case it and extract words with the regex `[a-z']+`.
  3. If there are no words, return `False`.
  4. Return `True` only if *every* extracted word is in the fixed continuation vocabulary below.
  ```text
  y ye yes yep yeah yup ya ok okay k sure fine
  confirm confirmed confirming confirmation approve approved approval
  create creation created make it them these those all both
  do go ahead proceed continue send ship submit push
  please thanks thank you now lgtm looks good correct right agreed exactly
  n no nope nah cancel stop dont abort reject rejected hold wait
  ```
- **Why:** a bare confirmation like "yes, create them" has no topic of its own — classified alone, it can be routed to the wrong lane. This lets a confirmation skip the classifier entirely and just stay on the previous lane.
- **Watch out:** this must be a *whole-message* test, not a prefix test. "yes, and also draft a ticket for FX" has to be classified fresh, because it introduces new work — if you check only a prefix, that sentence would wrongly be treated as a pure confirmation. Anything this function doesn't recognise (a typo, an unanticipated phrasing) is meant to fall through to the classifier below, not be guessed at — that's why the check is conservative (all words, not most).

**`recent_context(messages: list[dict]) -> str`**
- **Does:**
  1. Take the last 6 entries of `messages`.
  2. For each one: if it's a tool-result message, render it as `Tool result: <the result text>`.
  3. Otherwise, render `User: <text>` or `Assistant: <text>`; if an assistant message has no text (it only called tools), render `Assistant: (called tools: a, b)` listing the tool names.
  4. Join the non-empty lines with newlines.
- **Why:** a tool result is a `role: "user"` message in Strands (lesson 4) — labelling it `Tool result:` instead of `User:` stops the classifier from treating tool output as something the human typed.

**`classify(messages: list[dict], previous_route: str = "", model: Model | None = None) -> str`**
- **Does:**
  1. Get the human's latest text with `last_user_text(messages)`.
  2. If there is a `previous_route` and that text passes `is_continuation`, return `previous_route` immediately — no model call.
  3. Otherwise call `structured(RouteDecision, prompt, model=model)`, where `prompt` is: the classification instruction, then (only if there is a previous route) a sentence saying the previous turn was handled by that route and to stay on it unless the request has genuinely moved on, then `recent_context(messages)`, then the latest request text.
  4. Return `decision.route`.
- **Why:** classifying every message independently, with no memory of the previous route, is what caused a confirmed batch of ticket drafts to be misrouted to the read-only query lane in the original. Passing the previous route as a hint (not a hard override) lets the classifier still switch lanes when the topic actually changes.

**`ROUTE_TO_LANE` and `route_to_lane(route: str) -> str`**
- **Does:** `ROUTE_TO_LANE` is a fixed dict mapping every `RouteDecision.route` literal to a lane name: `ticket → ticket_agent`, `sprint → sprint_agent`, `spreadsheet → spreadsheet_agent`, `finance → finance_agent`, `diagram → diagram_agent`, `query → query_agent`, `requirements → requirements` (this one names a workflow, not an `Agent` — see below). `route_to_lane` looks the route up and falls back to `"query_agent"` for anything unmapped.
- **Watch out:** every route your `RouteDecision` schema can produce must have an entry here — a route with no lane raises `KeyError` mid-conversation, which is worse than the safe fallback `route_to_lane` already provides for genuinely unknown values.

#### `➕ pmagent/agents/requirements.py` [lesson 3, lesson 7] — the PRD writer ↔ reviewer loop

This is not a Strands `Agent`. It's a plain Python state machine that makes two stateless `structured()` calls (lesson 3) per pass. It's still an *agent* in the design sense: the reviewer's judgment at runtime decides whether the writer runs again, and how many passes happen.

**`RequirementsState`** — a Pydantic model holding `source_notes`, `company_context`, `prd: PRD | None`, `review: ReviewResult | None`, `iterations: int = 0`, `max_iterations: int = 3`, `markdown: str`.

**`route_after_review(state) -> str`**
- **Does:** return `"render"` if `state.review.approved` is `True`, or if `state.iterations >= state.max_iterations`; otherwise return `"writer"`.
- **Why:** the iteration cap is a safety net, not the normal exit path — most loops end because the reviewer approved, not because they ran out of passes.

**`write(state, model=None) -> RequirementsState`**
- **Does:** call `structured(PRD, prompt, system_prompt=<writer prompt>, model=model)` and return `state` updated with the new `prd` and `iterations + 1`.
- **Behavior of the prompt it builds:** the source notes, the company context (`retrieve_company_context(notes[:200])`, from D7), and — only on a revision pass (i.e. `state.prd` and `state.review` are both already set) — the previous draft plus the reviewer's `missing_requirements` and `issues`.
- **Why:** the writer's system prompt is its own `.md` file with the `prd` skill injected (same pattern as the ticket lane) — the standard lives in Markdown, editable without touching this code.

**`review(state, model=None) -> RequirementsState`**
- **Does:** call `structured(ReviewResult, prompt, system_prompt=<reviewer prompt>, model=model)` and return `state` updated with the new `review`. The prompt is just the source notes and the current draft.

**`render_prd_markdown(prd: PRD | None) -> str`**
- **Does:** deterministic (no LLM) rendering into the Atlassian PRD template, in this order: `# <title>`, a two-column table of target release / owner / stakeholders, `## Objective`, `## Background`, `## Success metrics` (a Goal/Metric table), `## Assumptions` (bullets), `## Requirements` (a numbered table: #, user story, importance, Jira, notes), `## User interaction and design`, `## Open questions` (a table, only when there are any), `## Out of scope` (bullets).
- **Watch out:**
  - Empty sections must still render something sane — `—` for a missing one-line field, `_None defined._` / `_None captured._` / `_Not specified._` for an empty list-backed section — never an empty or broken Markdown table.
  - Every cell value must be escaped: a literal `|` or newline inside a cell would otherwise break the table.
  - `render_prd_markdown(None)` returns `""` (called before the first pass has produced anything).

**`run_requirements_loop(state, model=None) -> RequirementsState`**
- **Does:** loop `write` → `review` → check `route_after_review`; when it says `"render"`, set `state.markdown = render_prd_markdown(state.prd)` and return.

**`run_requirements(notes: str, model=None) -> RequirementsResult`**
- **Does:** build an initial `RequirementsState(source_notes=notes, ...)`, run it through `run_requirements_loop`, and wrap the result as `RequirementsResult(reply, prd, review, iterations)`. If the loop ended on the iteration cap rather than approval, append a note to `reply` saying the reviewer still had open items.
- **Watch out:** test the cap by constructing `RequirementsState(max_iterations=<small number>)` directly and driving the loop — you shouldn't need a scripted model with dozens of turns to exercise it.

#### `➕ pmagent/agents/diagram_agent.py` [lesson 8] — the one lane whose toolset isn't a fixed list

**`LOCAL_TOOLS`** — always `[draft_diagram_brief]`.

**`build_tools(mcp_tools: list | None = None) -> list`**
- **Does:** return `[*LOCAL_TOOLS, *(mcp_tools or [])]`.
- **Why:** unlike the other four lanes, this one's tool list depends on whether an MCP session (Lucid) was reachable at startup — see `pmagent/tools/mcp_tools.py` (R2) for where `mcp_tools` comes from.

**`system_prompt(lucid_connected: bool) -> str`**
- **Does:** return the lane's base `SYSTEM_PROMPT` unchanged if `lucid_connected` is `True`; otherwise append a note containing the exact phrase `Lucid is **not connected**` that tells the model it can only draft a brief, not create anything in Lucid.
- **Watch out:** the model must never claim it can create a diagram when no Lucid tools are actually bound — that's what this note prevents.

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

#### `➕ pmagent/assistant.py` [lesson 4, lesson 6, lesson 7] — the orchestrator: one shared conversation, six lane agents, one interrupt/resume cycle

Everything before this step built a piece in isolation (tools, the gate, one lane,
the router, the requirements loop). This step wires them into the one object the
CLI and the web server both call. Two ideas from earlier lessons meet here for the
first time and make each other harder: **all lanes share one message list**
(lesson 4), and **a write pauses the lane mid-call and must be resumed later**
(lesson 6). Get the sequencing below exactly right, or a resumed lane either loses
history or answers the wrong tool call.

**`TurnResult(route, pending, reply)`** — a dataclass, and all `send`/`resume`
return.
- `route`: the lane the router picked (or kept, on a continuation).
- `pending`: a list of `{"id", "name", "args"}` dicts — the write calls waiting
  on approval. Empty means the turn finished with nothing left to approve.
- `reply`: text the *caller* must print. Only the requirements workflow sets
  this — a lane agent's own reply is printed live by hooks (`cli/echo.py`), not
  returned, because that hook is watching `agent.messages` as it grows.

**Module constant `ABANDONED_MESSAGE`** — the exact string `"Not executed — the
approval for this action was abandoned before the user answered. Nothing was
written."`. Written into the history in place of a tool result when an approval
is abandoned (see `discard_pending` below). Pick your own wording, but every
lane's tests and the CLI expect that a value exists and is user-readable.

**`PMAssistant(model=None, *, classify=None, hooks=(), mcp=None, on_route=None)`**
- **Does:**
  1. Store the constructor arguments as private state (`self._model`,
     `self._custom_classify`, `self._hooks`, `self._mcp`, `self._on_route`).
  2. Initialise the shared, mutable state every other method reads and writes:
     `self.messages = []`, `self.route = ""`, `self.last_prd = None`, plus two
     private fields that track a paused lane: `self._pending_lane = None` and
     `self._pending_interrupts = []`.
  3. Set up an empty cache for lane agents, `self._agents: dict[str, Agent] = {}`
     — agents are built lazily (see `send`/`resume` below), not here.
  4. Build `self.lanes: dict[str, tuple[str, list]]`, mapping each lane name to
     its `(system_prompt, tools)` pair, by importing the six lane modules built
     in R4 and reading their `SYSTEM_PROMPT`/`TOOLS` constants directly — except
     the diagram lane, whose prompt and tools depend on whether an MCP session
     was given: `diagram_agent.system_prompt(lucid_connected=bool(mcp_tools))`
     and `diagram_agent.build_tools(mcp_tools)`, where `mcp_tools = mcp.tools if
     mcp else []`.
- **Why:** Lane agents are expensive to build (each one is a Strands `Agent`
  with its own hooks and tool bindings) and most conversations never touch most
  lanes, so building them lazily on first use, not here, avoids paying for six
  agents when a conversation only ever needs one.
- **Watch out:** `self.lanes` is data (prompt + tool list), not agents. Don't
  confuse it with `self._agents`, the lazy cache — R6/R7 code and your tests
  will refer to both.

**Building (and caching) a lane's agent** — needed by both `send` and `resume`
below, so give it its own small helper, e.g. a private method `agent(lane) ->
Agent`.
- **Does:** if `lane` isn't in `self._agents` yet, look up its
  `(system_prompt, tools)` in `self.lanes`, build it with R4's
  `make_lane_agent(lane, system_prompt, tools, model=self._model,
  hooks=self._hooks)`, and cache it; either way, return the cached agent.
- **Why:** the *same* `Agent` object must be reused across a pause and its
  resume, because a Strands agent's interrupt state lives on that object
  privately — a freshly built agent has no memory of the paused call. Caching
  by lane name is what makes `resume` able to find "the same lane" later.

**`send(text: str) -> TurnResult`** — call this once per user message.
- **Does, in order:**
  1. If a previous turn is still waiting on an approval (`self.pending` is
     `True`), abandon it first: call `self.discard_pending()`.
  2. Classify the route by calling the router on `self.messages + [user_message(text)]`
     — a *new* list built for this call, not appended to `self.messages` yet.
  3. Store the result on `self.route`, and, if `on_route` was given to the
     constructor, call `on_route(route)` — before the lane runs, so a live CLI
     can print `[route: x]` ahead of the lane's own output.
  4. Look up the lane name for that route with `router.route_to_lane(route)`.
  5. If the lane is `"requirements"`, hand off to the requirements workflow
     (see below) and return its result. Otherwise, run the lane agent (see
     `_run_lane` below) and return *its* result.
- **Why step 2 doesn't append to `self.messages` yet:** the classifier must see
  the new message to decide the route, but nothing should land in the shared
  history until you know which lane (or workflow) is going to consume it —
  `_run_lane` and the requirements workflow each append the user message
  themselves, in their own way (see below).

**`_run_lane(lane: str, prompt) -> TurnResult`** — runs one lane agent for one
turn, whether starting a new turn or continuing a resumed one. `prompt` is
either the user's text (from `send`) or a list of `interruptResponse` blocks
(from `resume`, see below) — either is valid input to `agent(...)`.
- **Does, in order:**
  1. Get the lane's agent via the cache helper above.
  2. Point it at the shared conversation: `agent.messages = self.messages`.
     Strands will append to this exact list as the call runs.
  3. Call `agent(prompt)` inside a `try`. In a `finally` clause — so it runs
     even if the call raises — read the list back: `self.messages =
     agent.messages`.
  4. If `result.stop_reason == "interrupt"`: remember which lane is now paused
     (`self._pending_lane = lane`) and its interrupts (`self._pending_interrupts
     = list(result.interrupts or [])`); collect every gated call out of each
     interrupt's `reason["calls"]` into one flat list; return a `TurnResult`
     with that list as `pending`.
  5. Otherwise the turn finished cleanly: clear `self._pending_lane` and
     `self._pending_interrupts`, and return a `TurnResult` with `pending=[]`.
- **Why the `finally`:** if `agent(prompt)` raises partway through (a tool
  crash, a provider error), Strands may already have appended messages — the
  assistant's `toolUse` message, for instance — before the exception surfaces.
  Reading `agent.messages` back regardless keeps `self.messages` in sync with
  whatever the agent actually did, not just with the calls that returned
  cleanly. Skip the `finally` and a crash mid-turn silently drops history.
- **Watch out:** step 2 must happen on *every* call, not just the first —
  `agent.messages` is only "the shared list" for as long as you keep
  re-pointing it there. If another lane ran in between (see the router's
  continuation logic and `send`'s dispatch), this lane's own `agent.messages`
  attribute is stale until this line runs again.

**`resume(approved: bool) -> TurnResult`** — call this exactly once per
`pending` `TurnResult`, after the human has decided.
- **Does:**
  1. If nothing is pending (`self.pending` is `False`), raise a `RuntimeError`
     — there is nothing to resume.
  2. Build one `{"interruptResponse": {"interruptId": interrupt.id, "response":
     answer}}` block per interrupt in `self._pending_interrupts`, where
     `answer` is `gate.py`'s `APPROVE` if `approved` else its `REJECT`.
  3. Call `_run_lane(self._pending_lane, responses)` — the **same** lane name
     that was paused, so the cached, still-paused `Agent` object is the one
     that receives the answer.
- **Watch out:** `resume` does *not* clear `self._pending_lane` /
  `self._pending_interrupts` itself before calling `_run_lane` — it reads them
  to build the response blocks, and `_run_lane`'s own step 4/5 (above) is what
  updates or clears them afterwards, once it knows whether the resumed call
  led to *another* pause (a second write right after an approved one) or
  finished cleanly. If `_run_lane` raises, `self._pending_lane` /
  `self._pending_interrupts` are left exactly as they were — the approval is
  still "pending" from the caller's point of view, and `discard_pending()` is
  the only way out. This is deliberate: don't clear pending state before you
  know the resumed call actually landed.

**`discard_pending() -> None`** — abandon whatever is pending, if anything.
Safe to call when nothing is pending, and safe to call twice in a row.
- **Does:**
  1. Read and immediately clear `self._pending_lane` / `self._pending_interrupts`
     (set both back to "nothing pending"), keeping only a local copy of the
     lane name.
  2. If that local copy is `None` (nothing was pending), stop here — this is
     what makes a second call a no-op.
  3. Otherwise, walk `self.messages` for the last assistant turn's `toolUse`
     blocks that have no matching `toolResult` yet, and append one `role:
     "user"` message with a `toolResult` (`status: "error"`, text =
     `ABANDONED_MESSAGE`) for each — see the free function below.
  4. Drop the paused agent from the cache: `self._agents.pop(lane, None)`. The
     next `send`/`resume` for that lane will build a fresh one.
- **Why every unanswered `toolUse` needs an explicit result:** a Strands/
  Bedrock-shaped conversation is invalid if a `toolUse` block is ever followed
  by a new user turn with no matching `toolResult` (lesson 4) — the next model
  call would be rejected by the provider. Writing an explicit "not executed"
  result keeps the history valid without pretending the write happened.
  Rebuilding the agent (step 4) is necessary on top of that because a Strands
  agent's interrupt state is private: once an agent is paused, there is no
  public method that un-pauses it except answering the interrupt it's actually
  waiting on — so a paused agent that will never be resumed has to be replaced,
  not reset.

**A free function, `_close_unanswered_tool_calls(messages)`** — the mechanics
behind `discard_pending` step 3. Not part of the public class, but easiest to
test on its own, so give it its own name.
- **Does:** finds the last message with `role == "assistant"`; collects the
  `toolUseId` of every `toolUse` block in it; collects the `toolUseId` of every
  `toolResult` already present in any later message; for every id in the first
  set but not the second, appends one `toolResult` block (as described above).
  If every call already has a result — e.g. a `resume` that failed *after* the
  tools actually ran — this appends nothing.
- **Watch out:** it never removes or edits the original `toolUse` message.
  Providers reject a `toolResult` with no matching call just as they reject a
  call with no result, so the fix is always to add the missing result, never
  to delete the call.

**The requirements workflow** — `send` routes here directly; it never goes
through `_run_lane`.
- **Does:** call R4's `run_requirements(text, model=self._model)`; store
  `result.prd` on `self.last_prd`; append the turn to the shared history
  yourself — `self.messages.append(user_message(text))`, then
  `self.messages.append(assistant_message(result.reply))`; return a
  `TurnResult` with `reply=result.reply` (and `pending=[]`, the default).
- **Why it appends messages itself:** the requirements workflow is two
  `structured()` calls (lesson 3), not a Strands `Agent` — there is no
  `agent.messages` for it to share, and no hook watching it, so nothing else
  will put its turn into the shared history or print its reply. That is also
  why `TurnResult.reply` exists at all: it is the one path where the caller,
  not a hook, is responsible for showing the user the answer.

**`reset() -> None`** — start a fresh conversation (the CLI's `/new`).
- **Does:** `discard_pending()` first (never leave a paused agent behind), then
  clear the agent cache (`self._agents.clear()`), then reset `self.messages`,
  `self.route` and `self.last_prd` to their constructor defaults.

**`close() -> None`** — release external resources.
- **Does:** if an MCP session was given, close it (`self._mcp.close()`).
  Nothing else to release.

**Public state, for callers and tests to read directly (no method needed):**
`self.messages` (the shared history), `self.route` (the current lane's route
name), `self.last_prd` (the most recent structured PRD, or `None`), and
`self.pending` — a read-only `@property` that is simply `self._pending_lane is
not None`, so callers never have to know the private field's name.

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

#### `➕ pmagent/cli/echo.py` [lesson 5] — hook that prints the conversation as it happens

**`ConsoleEcho(HookProvider)`**
- **Does:** registers `on_message` on Strands' `MessageAddedEvent`. Give every lane agent one instance in its `hooks=[...]` list (after the gate — see R4).
- **Why:** the default `callback_handler` only sees token-level stream chunks. This hook sees whole messages — including tool *results*, which don't exist at the callback-handler level — which is what a terminal UI needs to print.

**`render_message(message: dict) -> list[str]`**
- **Does:** given one message from `agent.messages`:
  1. If it's an assistant message: one line for its text (if any), then one `-> tool_name(args)` line per tool call it made, with `args` rendered compactly by `brief` (below).
  2. Otherwise (a `role: "user"` message — which in Strands is also where tool *results* live; see lesson 4): one or more `| ...` preview lines per tool result, each result's text capped at 600 characters, with an image labelled rather than dumped.
  3. Returns `[]` for the human's own typed prompt — it's already on the user's screen, so this hook only needs to show what happens *after* it.
- **Watch out:** the stateless structured-output agents (router, PRD writer/reviewer, diagram brief) never get this hook, so their hidden tool calls never print — that's intentional (lesson 3). But if you add a *new* lane agent and forget to give it `ConsoleEcho()`, the symptom looks the same: silence where you expected `-> tool(...)` lines.

**`brief(args: dict) -> str`**
- **Does:** JSON-dumps a dict of tool arguments; if the result is longer than 120 characters, cuts it off and appends `...}`. Used by `echo.py` for tool-call lines and reused by `approval.py`'s fallback case.

---

#### `➕ pmagent/cli/approval.py` — what the human reads before approving a write

Design rule, carried over unchanged from the original project: **an approval prompt shows what could be wrong, not what is there.** Every function below exists to answer one question — *is this the write I meant?* — not to dump the raw call. In practice: show the sprint a ticket is landing in, the source issue each draft claims to derive from, and the full text of any comment (never truncated: approving wording you cannot see is not approval).

**`_describe_write(call: dict) -> str`**
- **Does:** dispatches on `call["name"]` with one `if`-branch per name in `pmagent/gate.py::WRITE_TOOL_NAMES` (R3), each rendering only the fields that distinguish a correct write from a wrong one — e.g. for `create_jira_issue(s)`, the declared `scope` (if any), then per draft: issue type, summary, source issue, assignee, story points, sprint, acceptance criteria, in that order, provenance (`source_issue`) always shown first. A name with no branch falls through to the last case.
- **Watch out:** the fallback case is `f"{name}({brief(args)})"` — raw arguments, no interpretation. It's deliberately unhelpful: if you're reading it, some write tool is missing a case above it.

**`draft_manifest(drafts: list[dict]) -> list[str]`**
- **Does:** returns `[]` when there are 3 or fewer drafts — `_describe_write`'s per-draft detail lines are enough to read at that size. Above 3, returns a compact table instead: one row per draft (index, source issue, assignee, summary), plus a trailing note when one or more drafts name no source issue.
- **Why:** past ~3 detail blocks, a batch reads as a wall of near-identical text, and a batch that grew by one draft is invisible in it. The manifest is a scannable index that makes an odd row stand out before the detail blocks are read at all.

**`unchecked_scope_warning(messages: list[dict], calls: list[dict]) -> str | None`**
- **Does:** filters `calls` to `create_jira_issue(s)` calls made with no `scope` argument. If none, returns `None`. Otherwise, extracts Jira issue keys (regex `[A-Z][A-Z0-9]*-\d+`) from the human's last message (`last_user_text`); if there are 3 or more, returns a `NOTE:` string naming them and how many drafts weren't checked against them. Otherwise `None`.
- **Why:** the scope check (`create_jira_issues(scope=...)`) only runs when the *model* remembers to pass `scope` — nothing forces it to. This is a warning, not a block, on purpose: a heuristic that blocks anything is defeated by its first false positive. Its only job is to make sure "the check didn't run" is never silent.

**`describe_write(call: dict) -> str`**
- **Does:** calls `_describe_write(call)` inside a `try`; on any exception, returns the raw tool name and arguments (via `brief`) plus the exception's type and message instead of propagating.
- **Why:** the caller is about to print this text and then sit on a paused interrupt. A renderer crash must not take down the approval prompt with it — the human still needs to be able to see *something* and say no.

---

#### `➕ main.py` — the terminal frontend

**`build_assistant(on_route) -> PMAssistant`**
- **Does:** if `LUCID_MCP_ENABLED` is set, calls `start_lucid()` (R2 / lesson 8) to get an `MCPSession` (or `None` on failure); returns `PMAssistant(hooks=[ConsoleEcho()], mcp=<that session or None>, on_route=on_route)`.

**A route-printing callable, passed as `on_route`**
- **Does:** remembers the last route it printed; prints `[route: x]` only when the route actually changes between turns.
- **Why it matters:** which lane a turn lands in decides which tools exist. Route it to the read-only query lane by mistake, and "the agent says it can't create a ticket" is indistinguishable from a genuine refusal — until this line tells you which lane actually ran.

**`main() -> int`**
- **Does, in order:**
  1. Calls `_quiet_library_logs()` (below).
  2. Runs `env.validate()` then `env.validate_jira()`. On a `ValueError` from either, prints `Configuration error: <message>` and returns `1` — deliberately not a traceback, since a missing `.env` value is a user mistake, not a bug.
  3. Prints the banner plus the configured Jira base URL/project and model.
  4. Builds the route-printing callable and passes it to `build_assistant`.
  5. Loops reading a line of input. `/exit` or `/quit` returns `0`; `/help` reprints the banner; `/new` calls `assistant.reset()` and clears the route printer's memory; an empty line is ignored; anything else goes to `handle_turn`. A blank/EOF read (Ctrl-D/Ctrl-C at the main prompt) prints `Bye.` and returns `0`.
  6. Whatever way the loop exits, a `finally` calls `assistant.close()`.

**`handle_turn(assistant, user_input, ask=input) -> bool`**
- **Does:**
  1. Calls `assistant.send(user_input)`.
  2. Prints `result.reply` only if it's non-empty. In practice that's only the requirements route: every other lane's reply was already printed as it happened, by `ConsoleEcho`; the requirements workflow runs its own write→review loop outside any `Agent`, so that hook never sees its output (lesson 7).
  3. Calls and returns `resolve_approvals(assistant, result, ask)`.
  4. If `assistant.send` raises `ContextWindowOverflowException`: calls `assistant.discard_pending()`, tells the user to type `/new`, and returns `True` (the CLI keeps running — a fresh conversation is the only fix, per lesson 4's `NullConversationManager` trade-off).
  5. On any other exception: calls `assistant.discard_pending()`, prints the error's type and message, and returns `True`.
- **Why discard first, in both error paths:** an approval left hanging wedges that lane for good (lesson 6 / R5) — clearing it before reporting the error is what keeps the next turn usable.

**`resolve_approvals(assistant, result, ask=input) -> bool`**
- **Does:** while `result.pending` is non-empty: print every pending call via `describe_write`, print `unchecked_scope_warning(...)` if it returns one, ask `Approve? [y/N]` via `ask`, and call `result = assistant.resume(<True/False>)`. Repeats, because one turn can pause more than once. Returns `True` once nothing is pending.
- **Watch out:** only the literal answers `y`/`yes` (case-insensitive) approve — an empty answer or anything else rejects. `EOFError`/`KeyboardInterrupt` (Ctrl-D/Ctrl-C) at the approval prompt calls `assistant.discard_pending()` and returns `False` immediately, instead of crashing or looping.

**`_quiet_library_logs()`**
- **Does:** reads `PMAGENT_LOG_LEVEL` (default `CRITICAL`) and sets it as the level of the `strands` logger.
- **Why:** with no logging configured, Python prints `WARNING`+ records to stderr by default, so Strands' own informational lines (e.g. "moving an image from a tool message for OpenAI compatibility") would otherwise interleave with the chat. Errors still surface through the CLI's own error line regardless.
- **Watch out:** keep this exact function name — R7's `web.py` imports it from `main`.

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

### First, extend what you already have

Five small signature changes to code from earlier steps. Your earlier tests must
stay green after making them.

- **`router.classify_with_reason(messages, previous_route, model, usage_sink) -> (route, reason)`**
  wraps the classifier you already wrote. `reason` is `"continuation"` when the
  fast path kept the previous route with no model call, or `"classifier"` when
  the model decided — the web UI shows this in the thinking chain, the CLI
  doesn't need it. `classify(...)` becomes a one-line wrapper that returns just
  the route. `PMAssistant` reports the route `"custom"` when the caller injected
  its own `classify=` function instead (tests do this to skip the router).
- **`llm.structured(..., usage_sink=None)`**: when given, call
  `usage_sink(invocation_usage(result))` after the call. `draft_diagram_brief`
  passes `tool_context.invocation_state.get("usage_sink")` through, so the
  diagram lane's hidden structured-output call gets counted too.
- **`requirements.run_requirements(..., on_step=None, usage_sink=None)`**: thread
  both through `write`/`review`/the loop, so the web UI can show PRD progress
  and count its tokens the same way.
- **`make_lane_agent(..., callback_handler=None)`**: pass it straight to the
  `Agent`. Still `None` by default, which is why the CLI stays silent between
  `ConsoleEcho`'s own prints.
- **`PMAssistant(..., on_route_decided=None, on_prd_step=None, callback_handler=None)`**:
  `on_route_decided(route, reason)` fires instead of (not in addition to)
  `on_route`, when set; `on_prd_step` is passed through to
  `run_requirements`; `callback_handler` is passed through to every lane agent.
  `TurnResult` grows `route_reason` and `usage` fields so callers can read what
  happened without reaching into private state.

### 1. `➕ pmagent/web/problems.py` — one error shape for the whole API

Purpose: every error response is `application/problem+json` (RFC 9457), so a
client switches on one stable `type` field instead of parsing prose, and a
human who hits an error can open that `type` URL and read what it means.

**`CATALOG: dict[str, tuple[status, title, explanation]]`**
- **Does:** one entry per problem slug (see the "problem slugs" table above),
  each with its HTTP status, a short title, and a longer explanation.
- **Why:** the explanation isn't wasted — `install()` below serves it as a real
  page at `/problems/<slug>`.

**`ProblemError(slug, detail="", headers=None, **extra)`**
- **Does:** an exception you raise from inside any route. It looks up `status`
  and `title` from `CATALOG[slug]` immediately, and keeps `detail`, optional
  response `headers`, and any `**extra` keyword fields (e.g. `turn=turn.id`,
  `approval=approval.id`) to merge into the JSON body later.
- **Why:** routes raise this one exception type instead of building a
  `JSONResponse` by hand each time; one handler (below) turns every instance
  into the same shape.

**`problem_body(slug, detail="", instance="", **extra) -> dict`**
- **Does:** builds `{"type": f"/problems/{slug}", "title": ..., "status": ...}`,
  adding `detail` and `instance` only when given (never as empty strings), then
  merges in `**extra`.

**`problem_response(slug, detail="", instance="", headers=None, **extra) -> JSONResponse`**
- **Does:** wraps `problem_body(...)` in a `JSONResponse` with
  `media_type="application/problem+json"` and the given status and headers.

**`install(app)`**
- **Does, in order:**
  1. Registers a handler for `ProblemError` that turns it into a
     `problem_response` (using `request.url.path` as `instance`).
  2. Registers a handler for FastAPI's `RequestValidationError` that returns the
     `"validation"` slug, with each field's error listed under `errors`.
  3. Registers a handler for Starlette's generic `HTTPException`, mapped by
     status code (`404 → not-found`, `405 → method-not-allowed`,
     `401 → unauthorized`); anything else falls back to a plain
     `{"type": "about:blank", ...}` body rather than inventing a slug for it.
  4. Adds `GET /problems/{slug}`, which renders the matching catalog entry as a
     small HTML page (an unknown slug is a 404, not a crash).
- **Watch out:** for the generic-`HTTPException` fallback, FastAPI's own
  boilerplate detail strings (`"Not Found"`, `"Method Not Allowed"`) are
  dropped rather than shown as `detail` — that field is meant to explain a
  *cause*, not restate the status.

### 2. `➕ pmagent/web/schemas.py` — request and response models

Purpose: every API body is a typed Pydantic model, so FastAPI validates input,
documents output, and the OpenAPI document at `/api/v1/openapi.json` (browsed
at `/api/docs`) matches exactly what the server does. Conventions to follow
throughout:

- snake_case fields; timestamps are ISO-8601 UTC strings ending in `Z`.
- Ids are opaque, prefixed strings (`conv_…`, `turn_…`, `apr_…`) — a client
  must treat them as tokens, never parse them.
- Use an explicit `status` string enum (`Literal[...]`) instead of a pile of
  booleans that would otherwise multiply as states are added.
- Wrap collections as `{"items": [...]}` so a field can be added later without
  breaking a client that reads the list directly.
- Every resource carries the URL(s) a client needs next (`events_url`,
  `decision_url`, `turns_url`) — a client follows links instead of building
  paths itself.

| Model | Fields worth noting |
|---|---|
| `Problem` | the RFC 9457 shape itself: `type`, `title`, `status`, optional `detail`/`instance` |
| `Usage` | `input_tokens`, `output_tokens`, `total_tokens`, `model_calls` — all default to `0` |
| `Health` | just `status: "ok"` |
| `Meta` | model/provider, Jira project, `lanes`, `reasoning_available` + a `reasoning_note` explaining why, `lucid_connected`, `auth_required` |
| `TurnCreate` | `message`, 1–20,000 characters; a `field_validator` also rejects a message that's only whitespace after stripping |
| `DecisionCreate` | `decision: Literal["approve", "reject"]` |
| `ToolCall` | `id`, `name`, `args` — one gated call |
| `ApprovalOut` | `status` is one of `pending/approved/rejected/abandoned`; `descriptions` is the CLI's own `describe_write` text, one per call; `warning` is the scope-check text or `None`; `decision_url` |
| `Event` | `id`, `event` (the kind string), `data` |
| `TurnOut` / `TurnDetail` | `TurnDetail` is `TurnOut` plus `events: list[Event]` — used wherever a turn's full history matters (e.g. rebuilding the page after a reload) |
| `TurnList` | `{"items": list[TurnDetail]}` |
| `ConversationOut` | `status` (`idle/running/awaiting_approval`), `messages: list[TranscriptItem]`, `pending_approval` |

### 3. `➕ pmagent/web/trace.py` — TraceRecorder: agent lifecycle → chain events

Purpose: one object that is *both* a Strands hook provider and a Strands
callback handler for every lane agent in one conversation, turning what the
agent loop does into the flat sequence of `{event, data}` records the SSE
stream sends. It doesn't know about HTTP at all — `sessions.py` points its
`emit` at the turn that's currently running.

Two different ways to watch an agent, used together:
- the **callback handler** gets stream-level chunks as they arrive — text
  deltas and reasoning deltas. This is what makes the UI feel live.
- **hooks** get whole lifecycle events with structure — a tool's name, input,
  result and duration; a completed assistant message.

Two more event kinds come from *outside* the agent loop entirely, reported by
`PMAssistant` callbacks instead of hooks: the route decision, and each step of
the PRD writer/reviewer loop.

**`assistant_kwargs() -> dict`**
- **Does:** returns `{"hooks": [self], "callback_handler": self,
  "on_route_decided": self.route_decided, "on_prd_step": self.prd_step}` —
  everything a `PMAssistant` needs to report into this one recorder.

**`register_hooks(registry, **kwargs)`**
- **Does:** subscribes `_before_tool` to `BeforeToolCallEvent`, `_after_tool` to
  `AfterToolCallEvent`, and `_message_added` to `MessageAddedEvent`.

**`__call__(**kwargs)`** (this *is* the callback handler)
- **Does:** if the chunk carries `reasoningText`, emit `reasoning.delta`;
  else if it carries `data` (plain text), emit `text.delta`. Anything else is
  ignored.

**`_before_tool(event)`**
- **Does:** records `time.monotonic()` against this call's `toolUseId`, then
  emits `tool.started` with `{tool_use_id, name, input}`.

**`_after_tool(event)`**
- **Does:** computes a duration — Strands' own `event.duration` if it gave one,
  else the wall-clock time since `_before_tool` recorded it; truncates the
  result text to 2,000 characters for display (`_preview`, which also reports
  whether it truncated); counts how many `image` blocks are in the result;
  emits `tool.finished` with the tool's name, status, `duration_ms`, the
  preview text, `output_truncated`, and the image count.
- **Watch out:** the truncation is a *display* limit only. The model itself
  still receives the tool's full, untruncated result — only the chain the
  human watches is shortened.

**`_message_added(event)`**
- **Does:** ignores anything that isn't an assistant message; joins the text of
  any `reasoningContent` blocks in it; emits `message.completed` with the
  message's own text, the names of any tools it called, and whether it carried
  reasoning at all.

**`route_decided(route, reason)`** / **`prd_step(kind, data)`**
- **Does:** `route_decided` emits `route.decided` with the route, the lane name
  it maps to, and the reason; `prd_step` emits `prd.step` with
  `{"step": kind, **data}`.

### 4. `➕ pmagent/web/sessions.py` — Turn, Approval, ConversationSession, SessionStore

Purpose: the web twin of `main.py`'s terminal loop — one `PMAssistant` per
conversation, driven by HTTP requests instead of stdin, with the same rules
carried over on purpose: a failed turn calls `assistant.discard_pending()` so
a crash never wedges the conversation on an unanswerable approval, and the
approval text is exactly the CLI's (`describe_write`,
`unchecked_scope_warning`). One difference from the CLI: a new message that
arrives while an approval is waiting is refused with 409 instead of silently
abandoning the approval — an API shouldn't throw away a pending decision as a
side effect of the next request.

Concurrency model: Strands' `agent(...)` call is synchronous, so each turn runs
on its own worker thread; a lock per conversation allows one turn at a time
*within* that conversation while separate conversations run fully in parallel.
Everything lives in memory — a restart forgets every conversation, exactly
like the CLI forgets on exit.

**`Turn`** — one user message and everything the agent did about it: an
append-only, thread-safe event log.

- **`emit(kind, data, status=None) -> None`**
  - **Does:** under one lock, appends `{id, event: kind, data: {ts, turn_id,
    **data}}` to the log, and — in that same critical section, only if a
    `status` was given — updates the turn's status too, then wakes any waiter.
  - **Why:** so no reader can ever observe a new event without the status
    change it caused. `approval.required` and the `awaiting_approval` status
    always arrive together, atomically — a `GET` in between can't see one
    without the other.
- **`wait_for_events(after, timeout) -> (new_events, resting)`**
  - **Does:** blocks on a `threading.Condition` until either there are events
    past index `after`, the turn reaches a resting status (`RESTING =
    {"awaiting_approval", "completed", "failed"}`), or `timeout` elapses; then
    returns the new events and whether the turn is now resting.
  - **Watch out:** this must run on a worker thread, never on the asyncio
    event loop — see the SSE route in `app.py` below for how that's enforced.

**`ConversationSession`**

- **`start_turn(message, idempotency_key=None) -> (Turn, created: bool)`**
  - **Does, in order:** hash the request body; if `idempotency_key` was seen
    before, compare the stored hash — a matching body returns the *original*
    turn with `created=False` (a safe retry), a different body raises
    `idempotency-key-reuse`; otherwise, if a turn is already running, raise
    `turn-in-progress`; otherwise, if an approval is pending, raise
    `approval-pending`; otherwise create the `Turn`, store it, point the
    recorder's `emit` at it, remember the idempotency key (only once the turn
    is guaranteed to exist), emit `turn.started`, and spawn a worker thread
    that calls `assistant.send(message)`.
  - **Why the idempotency check runs first:** a retried request has to return
    the same turn it got the first time, even if, by the time the retry
    arrives, the conversation now looks "in progress" or "pending" from a
    fresh caller's point of view.
- **`decide(approval_id, decision) -> Turn`**
  - **Does:** looks up the approval by id (`not-found` if it doesn't exist);
    if it was already decided the *same* way, returns its turn unchanged (an
    idempotent repeat); if it's not `pending` at all, raises
    `approval-not-pending`; otherwise marks it decided, puts its turn back to
    `running`, emits `approval.decided`, and spawns a worker thread that calls
    `assistant.resume(decision == "approve")`.
- **`discard()`** (backs `DELETE /conversations/{id}`)
  - **Does:** refuses with `turn-in-progress` while a turn is still running;
    otherwise marks any pending approval `abandoned` and calls
    `assistant.discard_pending()`.
- **`_run(turn, work)`** (what actually executes on the worker thread)
  - **Does:** calls `work()`; a `ContextWindowOverflowException` fails the turn
    with the `context-full` slug; any other exception fails it with
    `turn-failed` and that exception's message; a successful result is handed
    to `_finish`, and if `_finish` itself raises, the turn *still* fails with
    `turn-failed` rather than being left stuck in `running` forever.
- **`_finish(turn, result)`**
  - **Does:** records the route and usage on the turn and emits `usage`; if
    the result has pending tool calls, builds an `Approval` — using
    `describe_write` and `unchecked_scope_warning`, the same functions the CLI
    uses, so the two frontends never disagree about what a write does — stores
    it, and emits `approval.required` with status `awaiting_approval`, all
    under one lock; otherwise it emits `turn.completed` with the reply and
    marks the turn `completed`.
  - **Why the approval is stored and announced under one lock:** a `decide()`
    call racing this must never observe the approval before the turn is
    actually resting, or it could try to resume a turn that hasn't finished
    pausing yet.
- **`_fail(turn, slug, detail="")`**
  - **Does:** the same cleanup `main.handle_turn` performs on the CLI —
    abandon any pending approval, then call `assistant.discard_pending()`,
    swallowing any exception *that* raises (a cleanup failure must never mask
    the real error) — then emits `turn.failed` with a problem body.

**`SessionStore`**
- **Does:** an in-memory `{conversation_id: ConversationSession}` map behind a
  lock, with `create()`, `get()` (raises `not-found`), and `delete()` (calls
  `discard()` first, then removes the entry).

### 5. `➕ pmagent/web/app.py` — the FastAPI app: routes, security, lifespan

**`SecurityMiddleware(app, allowed_hosts)`** — plain ASGI middleware, not
FastAPI's `BaseHTTPMiddleware`, specifically so it never buffers the SSE
stream while inspecting it.
- **Does, per request:** picks the CSP (the relaxed `DOCS_CSP` only for paths
  under `/api/docs`, `STRICT_CSP` everywhere else); rejects with `bad-host`
  (400) if the `Host` header's hostname isn't in `allowed_hosts`; for any
  method other than `GET`/`HEAD`/`OPTIONS` that carries an `Origin` header,
  rejects with `forbidden-origin` (403) unless that origin matches this
  server's own scheme+host; otherwise passes the request through, adding the
  CSP plus `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`
  and `X-Frame-Options: DENY` to every response, including its own refusals.
- **Why two separate checks:** they stop two different attacks, and neither
  substitutes for the other. The **Host** check stops DNS rebinding — a
  hostile page that tricks the browser into resolving *its own domain* to
  127.0.0.1 still sends that domain's name in the `Host` header, so checking
  it first refuses the request before anything else runs. The **Origin**
  check stops CSRF — a state-changing request is only honoured if it was
  actually sent by the page this server itself serves, not merely aimed at
  the right host.

**`create_app(assistant_factory=None, *, api_token=None, extra_hosts=(), enable_mcp=False) -> FastAPI`**
- **Does:** builds the `FastAPI` app with a `lifespan` that, only when both
  `enable_mcp` and `env.LUCID_MCP_ENABLED` are true, starts one Lucid MCP
  session for the *whole server's* lifetime (not one per conversation) and
  closes it on shutdown; installs the problem-response handlers
  (`problems.install`); adds `SecurityMiddleware` with allowed hosts
  `{"127.0.0.1", "localhost", *extra_hosts}`; wires an optional bearer-token
  dependency (`require_token` — a no-op when no token is configured, otherwise
  a constant-time `hmac.compare_digest` check so response timing can't leak
  the token); registers every route under `/api/v1`; mounts
  `pmagent/web/static/` at `/static`; and serves `index.html` at `/`.
- **Why `enable_mcp` is separate from `LUCID_MCP_ENABLED`:** only `web.py`
  passes `enable_mcp=True`. Tests and the offline demo build the app through
  this same factory without ever starting a real MCP client, even if
  `LUCID_MCP_ENABLED=true` happens to be set in the test environment — one
  flag is "the feature is turned on", the other is "and this particular
  caller actually wants it".

Route behaviour worth calling out beyond the routes table above:
- `POST …/turns`: 202 + `Location`; the body is validated by `TurnCreate`
  (1–20,000 characters, whitespace-only rejected); an optional
  `Idempotency-Key` header (max 200 characters) makes a retry return the
  *same* turn instead of running the model a second time.
- `GET …/turns/{id}/events` (SSE): resumes from the `Last-Event-ID` header if
  the client reconnects, else from `?after=`; each loop iteration waits for
  new events (off the event loop — see below), forwards any it gets, and
  closes the stream once the turn is resting with nothing left to send, or
  the client disconnects.
- **Why the SSE wait goes through `anyio.to_thread.run_sync(..., limiter=SSE_LIMITER)`:**
  `Turn.wait_for_events` blocks on a `threading.Condition`. Calling it directly
  from an `async def` route would block the *entire* event loop until an event
  arrives — stalling every other request, including ones for unrelated
  conversations. Running it on a worker thread fixes that; the limiter
  (`SSE_LIMITER = anyio.CapacityLimiter(16)`) caps how many idle SSE
  connections can occupy worker threads at once, so a pile of open browser
  tabs can't starve ordinary requests either.
- `GET …/turns` returns every turn *with* its events (`detail=True`) — this is
  what lets the page rebuild both the chat and the full thinking chain after a
  reload, without trying to replay an SSE stream from scratch.

### 6. The page — `➕ pmagent/web/static/index.html`, `app.css`, `app.js`

You write this file's actual content; these are the rules it has to satisfy,
not a function-by-function spec.

**What it must show:**
- A chat window and a thinking-chain window, side by side or toggled.
- Assistant replies rendered as Markdown.
- An approval card that shows the *exact* text `describe_write` produced on
  the server — never re-derived in JavaScript — with Reject and Approve
  buttons, neither pre-selected.
- A token prompt when a request comes back 401.
- After a reload, the chat and chain are rebuilt from `GET …/turns` (each turn
  already carries its own events, per item 5 above) rather than trying to
  resume a live SSE connection.

**What the CSP in `app.py` forbids, and what that means for this file:** no
inline `<script>` tag, no `style="..."` attribute, and no `on*=` handler
attribute anywhere in the HTML or generated by the JS — the
`Content-Security-Policy` header blocks all three outright, so violating this
rule doesn't fail a lint, it fails silently (and confusingly) in the browser
at runtime. All behaviour lives in `app.js`, loaded as an external script; all
styling lives in `app.css`.

**Sanitizing model output — and why the CSP alone isn't enough:** render
model text with `marked`, then pass the resulting HTML through
`DOMPurify.sanitize(html, {FORBID_TAGS: ["img", "style", "form", "iframe",
"svg", "math", "input", "button"]})` before inserting it into the page. The
CSP stops a `<script>` tag from *executing*, but it does nothing about
HTML-injection tricks like `<img src=x onerror="...">` sitting in text the
model produced — DOMPurify is the layer that strips those out before they
ever reach the DOM.

**Vendoring, not a CDN:** download the browser builds yourself — for example
`marked@15.0.12/marked.min.js` and `dompurify@3.2.6/dist/purify.min.js` from
the npm CDN — into `➕ pmagent/web/static/vendor/`, so the page loads no
third-party script at runtime and still works with no network. Record each
file's version, source URL and SHA-256 in
`➕ pmagent/web/static/vendor/README.md`; a test checks the hashes on every
run, so upgrading a library is a deliberate, reviewed edit rather than a
silent supply-chain surface.

### 7. `➕ web.py` — `build_server`, `main`

**`build_server(host="127.0.0.1", port=8000, allowed_hosts=()) -> uvicorn.Server`**
- **Does:** adds `host` itself to the Host allowlist unless it's already
  loopback or a wildcard bind address (`0.0.0.0` / `::` — neither of which a
  browser ever sends as a `Host` header, so listing them there would be
  meaningless); calls `create_app(api_token=env.PMAGENT_API_TOKEN,
  extra_hosts=..., enable_mcp=True)`; wraps it in a single-worker,
  no-reload `uvicorn.Server`.
- **Why single-worker, no reload:** a reload subprocess or extra worker
  process wouldn't inherit whatever the *current* process already did before
  importing this module — in particular, R8's write-blocking `smoke.py`
  patches `requests.Session.request` in-process, and a forked worker would
  simply bypass that patch and be able to write for real.

**`main(argv) -> int`**
- **Does, in order:** parses `--host` (default `127.0.0.1`), `--port`, and
  repeatable `--allowed-host`; runs `env.validate()` and `validate_jira()`,
  printing `Configuration error: …` and returning 1 on failure, exactly like
  `main.py`; if the host isn't loopback, refuses to start unless
  `PMAGENT_API_TOKEN` is set; if the host is a wildcard bind, *also* refuses
  unless at least one `--allowed-host` was given; otherwise prints a
  plaintext-HTTP warning; prints a banner (showing `localhost` in place of a
  wildcard bind, since that's not a URL anyone can put in a browser) and runs
  the server.
- **Why a wildcard bind needs `--allowed-host`:** `0.0.0.0` is only where the
  socket listens — it is never the name a client's browser sends in its
  `Host` header. Without an explicit `--allowed-host`, `SecurityMiddleware`'s
  allowlist would reject every real request that reaches a wildcard-bound
  server, so whatever DNS or LAN name clients will actually type has to be
  named up front.

### 8. `➕ scripts/__init__.py` (empty) and `➕ scripts/demo_web.py`

Purpose: the real app end to end — same `PMAssistant`, same Strands agents,
same approval gate, same web server — with a `ScriptedModel` standing in for
the LLM and a fake Jira client standing in for the real one, so lessons 10–11
need no API key and no Jira. Run as a script, it first puts the project root
on `sys.path` (only the script's own folder is added automatically).

**`DemoJira`**
- **Does:** stands in for the real Jira client. `search_issues_page` always
  returns the same three made-up issues; `add_comments` appends
  `(issue_keys, comment)` to a class-level list instead of writing anywhere.

**`configure()`**
- **Does:** sets `env.LLM_PROVIDER` / `LLM_MODEL` / `JIRA_PROJECT_KEY` to demo
  values, and `env.JIRA_BASE_URL = None` so `/meta` never names a real
  tenant; turns reasoning on (`LLM_REASONING_EFFORT="high"`,
  `LLM_REASONING_SUMMARY="detailed"`) so the thinking chain has something to
  show; points both Jira tool modules' `get_client` at `DemoJira`.

**`scripts() -> dict[str, list]`**
- **Does:** returns a fresh queue of scripted turns per conversation, keyed by
  a word found in the conversation's first message (`"risk"`, `"comment"`,
  `"prd"`). Each queued turn is either a literal list of model blocks, or a
  `messages -> blocks` function — used for the `"comment"` script's second
  turn, which has to look at whether the write was approved or rejected
  before deciding what the model says next.

**`factory(recorder)`**
- **Does:** builds a `PMAssistant` whose `classify=` looks up the route from
  the keyword directly (`ROUTES.get(_key(messages), "query")`), skipping the
  router entirely so no scripted turn is spent classifying; its model's
  `next_turn(messages)` pops the next scripted turn for that conversation's
  keyword, or returns the fallback `UNKNOWN` text once the queue for that
  keyword is empty or no keyword matched at all.

**`main(argv)`**
- **Does:** parses `--host`/`--port`, calls `configure()`, prints the three
  example prompts, and serves `create_app(factory)` directly with uvicorn.
  Unlike `web.py::main`, it never calls `env.validate()` / `validate_jira()` —
  the demo supplies every value itself and needs no real credentials at all.

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

1. `➕ scripts/smoke.py` — a launcher that runs the real CLI or web UI, on the
   real model and the real Jira, with every write physically blocked underneath
   the app. The point is to let you press **y** at an approval prompt and prove
   the gate works, instead of just hoping nobody types it.

   **What it does, in order:**
   1. Sets `LUCID_MCP_ENABLED=false` and puts the project root on `sys.path`,
      **before importing anything from `pmagent`**. Running `scripts/smoke.py`
      directly only adds `scripts/` to the path, not the project root, so this
      has to happen first or the later imports fail.
   2. Wraps `requests.Session.request` — the one method every domain HTTP call
      ends in, whether it's a plain `requests.get(...)` or the Jira/Confluence
      `Session` object — with a **fail-closed allowlist**:

      | Request | Verdict |
      |---|---|
      | `GET`, `HEAD`, `OPTIONS` | allowed |
      | `POST …/rest/api/3/search/jql` | allowed (Jira's search is a POST) |
      | `POST login.microsoftonline.com/…/oauth2/v2.0/token` | allowed (token refresh) |
      | anything else | print `[smoke] BLOCKED <METHOD> <path>` in red and raise `RuntimeError("smoke: write blocked")` |

      Match on the exact path, not the full URL — a query string must not be
      able to smuggle a write past the allowlist by appending something that
      looks like the JQL path.
   3. Replaces `convert_fy_budget` on the FY budget `pipeline` module with a
      function that raises the same error. The FY converter writes local files
      directly; it never goes through `requests`, so the HTTP wrapper above
      can't catch it, and it needs its own block. (The finance tool imports
      `convert_fy_budget` from the module at call time, so patching the module
      attribute is enough — you don't need to patch every caller.)
   4. Runs a **self-check** before starting anything: fire a handful of writes
      (an issue create, a comment, a PUT, a spreadsheet write, …) through the
      wrapped `requests.Session.request` and confirm every one of them raises,
      that the two allowlisted requests still pass, and that the FY patch
      raises too. If any of that fails, the launcher exits instead of starting
      — it should never be possible to run "live" with a block that silently
      isn't there.
   5. Only then imports and runs `main.main()` (or, with `--web`, `web.main()`
      in the same process — the block is a process-wide patch, so one process
      is what makes it cover the whole server).

   **Why:** the block has to be fail-closed (default: blocked) rather than a
   list of writes to catch, because a new write tool you add later should be
   unreachable by default, not silently unblocked until someone remembers to
   list it. And it has to be installed and self-checked *before* `pmagent` is
   imported, because a tool module that builds its Jira client at import time
   would otherwise get a client wired to the real, unblocked `requests`.

   **Watch out:** the LLM SDKs call out over `httpx`, not `requests`, so model
   calls are unaffected by any of this — you're only ever blocking the app's
   own writes, never the conversation itself. Lucid's MCP client also uses
   `httpx`, which is why it's disabled outright rather than patched.
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

### 1. The analyser — plain Python, no Strands, no MCP

#### `➕ docs/learning/examples/sprint_review/analyser.py` — turns a Jira CSV export into the sprint's numbers, deterministically

This is the file the model is never allowed to compute by hand. Same rule as
`pmagent/tools/jira/metrics.py`: **the model does judgment, code does
arithmetic.** Nothing here imports Strands or MCP — it's a pure function you can
unit-test with a CSV on disk and nothing else.

**`normalize_status(status: str) -> str`**
- **Does:** strips the input; if it case-insensitively matches one of the six
  standard statuses (`Done`, `In review`, `In Progress`, `Blocked`, `To Do`,
  `Duplicate`), returns that standard spelling. Otherwise returns the trimmed
  input **unchanged**.
- **Why:** a status your rules don't know about must stay visible under its own
  name, not get silently folded into a known one.

**`parse_jira_date(text: str) -> datetime | None`**
- **Does:** returns `None` for a blank string. Otherwise tries, in order: a set
  of **day-first** formats (`01/Sep/26 9:00 AM`, `14/09/2026`, `14 Sep 2026`,
  each with an optional 12- or 24-hour time), then the same patterns
  **month-first** (`09/14/2026`), then ISO 8601 (dropping any UTC offset —
  `sprint_start` is a calendar date, not a timezone-aware instant). Returns
  `None` if nothing matches.
- **Watch out:** a date that fails to parse is *not* the same as a blank date —
  `analyse()` tracks the two separately (see `unreadable_date_issue_keys`
  below).

**`analyse(csv_path, *, sprint_start=None, exclude_unfinished_assignee_first_names=(), treat_as_in_progress_issue_keys=()) -> dict`**

Raises `CsvError` (see below) for anything unusable. Required columns are
`Issue key`, `Summary`, `Status`; `Assignee`, `Created`, `Resolved` are
optional and default to `""` when the column is missing.

- **Does**, in order:
  1. Read the file's bytes **once**. These exact bytes are both hashed
     (SHA-256, for `source.sha256`) and parsed — never re-read the file a
     second time for parsing, or the hash no longer matches what was analysed.
  2. Decode as `utf-8-sig` with `errors="replace"` (drops a BOM if present;
     never crashes on a non-UTF-8 export, e.g. one saved by Excel). Parse as
     CSV; a row with no fields at all (a blank line) is skipped.
  3. Check the header row: no header at all → `CsvError`; missing any of
     `Issue key` / `Summary` / `Status` → `CsvError` naming the missing column(s)
     and listing what *was* found; a header that's fine but zero data rows
     following it → `CsvError`.
  4. Build a case-insensitive lookup for `exclude_unfinished_assignee_first_names`
     and `treat_as_in_progress_issue_keys`, each keeping the **first spelling**
     it was given (so `"Alex"` beats a later `"alex"` for display purposes,
     but either matches).
  5. For every row, build one issue record:
     - `first_name` = the first whitespace-separated word of `Assignee`, or
       `""` if `Assignee` is blank.
     - `source_normalized_status` = `normalize_status(Status)`.
     - `excluded` = true only when the status **isn't** `Done` **and**
       `first_name` (case-insensitively) is in the excluded set.
     - `override` = true only when the row **isn't** excluded, **isn't**
       `Done`, and its key (case-insensitively) is in the confirmed-in-progress
       set.
     - `normalized_status` = `"In Progress"` when overridden, else
       `source_normalized_status`.
     - Record `exclusion_reason` = `"Unfinished item assigned to excluded first
       name '<first_name>'"` when excluded, else `""`; `status_adjustment` =
       `"User-confirmed as In Progress for this review."` when overridden, else
       `""`.
  6. Split the records into `included` (not excluded) and `excluded_issues`.
  7. Count `included` by `normalized_status`. Build `status_counts` as: the
     six standard statuses **in their fixed order**, each included only if its
     count is nonzero, followed by every non-standard status — grouped
     case-insensitively under the first spelling seen, counts summed, then
     those groups sorted by `casefold()`.
  8. From the counts: `active_flow` = In review + In Progress;
     `started_work` = Done + `active_flow` + Blocked; and
     `done_or_active_pct_of_started` = `round(100 * (Done + active_flow) /
     started_work)` when `started_work > 0`, else `0`. `round()` here is
     Python's banker's rounding (`62.5` rounds to `62`, not `63`) — don't
     replace it with a "round half up" helper, the test table checks this
     exact case.
  9. Only when `sprint_start` is given (a `date`, midnight of that day is the
     window's start, inclusive): for every **included** issue, parse its
     `Created` date, and — only for issues whose `normalized_status` is
     `Done` — its `Resolved` date. Count how many parsed `Created`/`Resolved`
     dates fall on or after the window start
     (`created_since_sprint_start` / `done_resolved_since_sprint_start`).
     Separately, collect into `unreadable_date_issue_keys` any issue whose
     `Created` field was **non-blank but unparseable**, or (for `Done` issues
     only) whose `Resolved` field was non-blank but unparseable. A *blank*
     date field is not an error and is not listed. When `sprint_start` is
     omitted, all three of these fields are `None`.
  10. Assemble the result (shape below), then re-check that `status_sum`
      (the sum of `status_counts`) equals `included`'s count, raising
      `CsvError` if it doesn't. This can't currently fire — `status_counts` is
      built from `included` itself — but it stays as a belt-and-braces guard
      against a future refactor breaking that invariant silently.

- **Return shape** — a dict with schema `"sprint-review-analysis.v1"`:

  | Key | Contents |
  |---|---|
  | `schema_version` | the literal string `"sprint-review-analysis.v1"` |
  | `generated_at_utc` | `datetime.now(timezone.utc).isoformat()` |
  | `source.csv_path` | the resolved, absolute path given to `analyse()` (the server, in step 2, overwrites this with a root-relative name before it ever reaches a model) |
  | `source.sha256` | SHA-256 of the exact bytes read in step 1 |
  | `filters.sprint_start` | the given date as `"YYYY-MM-DD"`, or `None` |
  | `filters.excluded_unfinished_assignee_first_names` | the excluded names, deduplicated case-insensitively, sorted by `casefold()` |
  | `filters.user_confirmed_in_progress_issue_keys` | same, for the override keys |
  | `filters.rule` | the fixed string `"Exclude matching assignees only when status is not Done; retain their Done cards."` |
  | `totals.exported` | every row read |
  | `totals.included` | rows not excluded |
  | `totals.excluded` | rows excluded |
  | `totals.status_sum` | sum of `metrics.status_counts` (must equal `totals.included`) |
  | `metrics.status_counts` | see step 7 |
  | `metrics.done` / `.in_review` / `.in_progress` / `.blocked` / `.to_do` / `.duplicate` | the six standard counts (0 if a status never appears) |
  | `metrics.active_flow` / `.started_work` / `.done_or_active_pct_of_started` | see step 8 |
  | `metrics.created_since_sprint_start` / `.done_resolved_since_sprint_start` / `.unreadable_date_issue_keys` | see step 9; all three `None` without a `sprint_start` |
  | `evidence.included_issues` / `.excluded_issues` | one record per row (the shape built in step 5), for citing a specific card |

**`CsvError(ValueError)`** — raised, with these exact messages, for:
- an empty file: `"CSV is empty: <name>"`
- missing required columns: `"CSV is missing required column(s): <names>. Found: <headers>"`
- a header with no data rows: `"No issue rows found in <name>"`
- the reconciliation check in step 10 (a message naming both counts)

### 2. The server — the guards a host can't be trusted to provide

#### `➕ docs/learning/examples/sprint_review/server.py` — wraps `analyse()` as three MCP tools, with the safety fixes a real design review found missing

Import `MCPServer` from `mcp.server.mcpserver`, `ToolError` from
`mcp.server.mcpserver.exceptions`, and `ToolAnnotations` from `mcp.types`. The
two things this server exists to fix, that a plain wrapper around `analyse()`
would not: **a host could ask for a CSV outside the folder it was given**, and
**a second save could silently overwrite the first one's numbers**. Both guards
live *in the server*, because a server can't assume its host — or its host's
model — will be careful.

**`RefusedError(ToolError)`**
- **Does:** nothing beyond what `ToolError` does — it exists so every refusal
  in this file raises the *same* exception type.
- **Why:** an MCP `ToolError`'s message is sent back to the model as a normal
  (if failed) tool result — the model can read it, explain it, or try
  something else. Any exception that is *not* a `ToolError` is treated as a
  crash: the model sees only `"Error executing tool <name>"`, and the real
  reason stays in the server's own log. Every guard below deliberately raises
  `RefusedError`, never a bare `ValueError` or `OSError`.

Every helper below receives `root` **already resolved** — `create_server`
resolves it exactly once, so every containment check compares two resolved
paths, never a resolved path against a raw one.

**`resolve_csv(root: Path, csv_name: str) -> Path`**
- **Does:**
  1. Resolve `root / csv_name` (this follows both `..` segments and symlinks).
  2. If that resolution itself raises `OSError` or `ValueError` (a NUL byte in
     the name, a path too long, a symlink loop) → refuse: *"not a usable file
     name"*.
  3. If the resolved path is not `.is_relative_to(root)` → refuse: *"outside
     the sprint export folder"*.
  4. If its suffix isn't `.csv` (case-insensitive) → refuse: *"not a .csv
     file"*.
  5. If it doesn't exist as a file → refuse: *"No CSV named '<name>' in the
     sprint export folder"*.
- **Why:** resolving the path *before* comparing it to `root` is what catches a
  symlink that points outside the folder — comparing the raw, unresolved
  string would miss it completely.

**`list_exports(root: Path) -> list[str]`**
- **Does:** walks every path under `root` recursively (subfolders included),
  sorted; keeps only the ones `resolve_csv` would accept; returns them as
  root-relative, forward-slash names.
- **Why:** by reusing `resolve_csv` instead of its own filter, "what gets
  listed" and "what gets accepted" can never drift apart.

**`run_analysis(root, csv_name, sprint_start=None, exclude_unfinished_assignee_first_names=None, treat_as_in_progress_issue_keys=None, include_evidence=True) -> dict`**
- **Does:**
  1. `resolve_csv(root, csv_name)` — any of the refusals above can end this
     call here.
  2. Parse `sprint_start` (a string) as an ISO date; a value that isn't
     `YYYY-MM-DD` → refuse, with a message naming that format.
  3. Call `analyser.analyse(...)` with the resolved path and the filters.
  4. Translate the analyser's own failures at this boundary: a `CsvError` or
     `csv.Error` → refuse *"'<name>' can't be analysed: <reason>"*; an
     `OSError` (e.g. unreadable by the server's own user) → refuse *"'<name>'
     can't be read: <reason>"*.
  5. Overwrite `result["source"]["csv_path"]` with the **root-relative** name
     before returning — the absolute path on the server's machine must never
     reach the model.
  6. When `include_evidence` is `False`, drop the `"evidence"` key entirely.
- **Why:** this function is the seam between "framework-free arithmetic"
  (`analyse()`) and "MCP-shaped refusals" (`RefusedError`) — the analyser
  itself never needs to know it's being called from a server.

**`save_run(root: Path, run_name: str, analysis: dict) -> str`**
- **Does:**
  1. Validate `run_name`: 1–64 characters of letters, digits, `.`, `_`, `-`,
     starting with a letter or digit. Anything else → refuse.
  2. Ensure `root / "runs"` exists (`mkdir(exist_ok=True)`), then confirm it
     resolves to itself — i.e. it is a real folder, not a symlink pointing
     elsewhere. Either failing → refuse: *"runs/ must be a real folder inside
     the sprint export folder"*.
  3. Create `runs/<run_name>/` with a plain `mkdir()` — **no** `exist_ok`.
  4. If that folder already exists, `mkdir()` raises `FileExistsError` →
     refuse: *"Run '<run_name>' already exists. Choose a new run name; an
     existing review is never overwritten."*
  5. Write `metrics.json` inside it (`json.dumps(analysis, indent=2)` plus a
     trailing newline, UTF-8) and return its path relative to `root`.
- **Why the plain `mkdir()` matters:** the "never overwrite" guarantee isn't
  an `if folder.exists(): refuse` check — that would race a second call
  arriving at nearly the same time. A bare `mkdir()` is atomic: the filesystem
  itself guarantees only one caller can win the race, and every other caller
  gets `FileExistsError`.

**`create_server(root: Path) -> MCPServer`**
- **Does:**
  1. Resolve `root` once.
  2. Build an `MCPServer`, and register three tools on it:
     - `list_sprint_exports()` — no arguments, returns `list_exports(root)`,
       annotated `readOnlyHint=True`.
     - `analyze_sprint(csv_name, sprint_start=None,
       exclude_unfinished_assignee_first_names=None,
       treat_as_in_progress_issue_keys=None, include_evidence=True)` — returns
       `run_analysis(...)` directly, also annotated `readOnlyHint=True`.
     - `save_metrics(run_name, csv_name, sprint_start=None,
       exclude_unfinished_assignee_first_names=None,
       treat_as_in_progress_issue_keys=None)` — runs the analysis itself (with
       evidence included), then `save_run(...)`, and returns just
       `{"saved": <path>, "sha256": ..., "totals": ...}`. Annotated as a write
       (`readOnlyHint=False`, plus `destructiveHint=False`,
       `idempotentHint=False`).
- **Why the docstrings matter:** exactly as in lesson 2, a tool's docstring is
  what the model reads to decide when and how to call it — write each one to
  explain the filters and what the tool returns, the same discipline as any
  local `@tool`.
- **Watch out:** never call `print()` anywhere in this file's request path.
  For a stdio server, **stdout is the protocol** — anything printed there
  corrupts the next message. If you need visibility while debugging, write to
  stderr; the client in step 3 passes the server's stderr straight through to
  your terminal.

**`main()`**
- **Does:** parses one required argument, `--root`; if it isn't an existing
  directory, prints a usage error and exits (via `argparse`'s own
  `parser.error`) instead of raising; otherwise calls
  `create_server(root).run()`, which blocks, serving over stdio.

### 3. The client — talking to your own server through the real gate

#### `➕ docs/learning/examples/08_sprint_review_mcp.py` — starts the server as a subprocess, and runs it through a real lane agent

This is the other half of lesson 8: R2 only ever *consumed* someone else's
server. Here the same `make_lane_agent` from R4 is pointed at a server you
just wrote, so the approval gate runs for real against tools nobody but you
has reviewed.

- **Does**, in order:
  1. Copy `sample-jira.csv` into a fresh temporary directory — this becomes
     the server's `--root`, and is deleted at the end regardless of outcome.
  2. Build an `MCPClient` whose transport is
     `stdio_client(StdioServerParameters(command=sys.executable, args=["-m",
     "sprint_review.server", "--root", <temp root>], cwd=<examples folder>))`
     — the server runs as a **separate Python process**, talking over
     stdin/stdout.
  3. `client.start()`, then `client.list_tools_sync()`. One call is enough
     here (only 3 tools) — unlike `mcp_tools.py`'s `_list_all_tools`, there's
     no need to follow a pagination token.
  4. Build a lane agent with `make_lane_agent`, passing it the discovered
     tools and a scripted model whose script is: call `analyze_sprint` (with
     `include_evidence=False`), call `save_metrics` as run `"sprint-35"`,
     call `save_metrics` as `"sprint-35"` **again** (the same name, on
     purpose — this is what exercises the refusal), then a final text turn
     that reads the real numbers back out of the *first* tool result (the
     `analyze_sprint` call) and quotes them — it never invents or recomputes
     a figure.
  5. Before running the agent: for every discovered tool, print its name, the
     read-only hint the **server** declared
     (`tool.tool_spec["annotations"]["readOnlyHint"]`), and whether **your**
     gate actually pauses on it (`requires_approval(tool_name, agent)`) —
     side by side.
  6. Run the agent on the prompt; while `stop_reason == "interrupt"`,
     auto-approve every pending call (safe here — the server can only touch
     the throwaway temp folder) and continue.
  7. Print each tool result's status and a preview of its text, the files the
     server actually wrote under the temp root, and the model's final
     answer.
  8. In a `finally`: stop the client, then remove the temp directory.
- **Why the side-by-side print matters:** this is the concrete demonstration
  of lesson 8's rule. `list_sprint_exports` and `analyze_sprint` are both
  declared read-only by the server, but all three tools — including those two
  — still print `gated=True`, because nothing is in
  `pmagent/tools/mcp_tools.py::APPROVED_READ_TOOLS` yet. **Annotations are a
  hint from the server; only a human adding a name to that frozenset makes a
  discovered tool free.**
- **Watch out:** the second `save_metrics` call is *expected* to come back as
  an error result ("already exists") — that's the point of calling it twice,
  not a bug to fix.

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

#### `➕ scripts/container.py` — the image's one entrypoint, so every way to run the project is a name, not a memorized command line

**Does:** `command(argv) -> (args, cwd)` turns the first CLI argument into a program to `exec` and the folder to run it from:

| Name | Runs | Notes |
|---|---|---|
| `demo` (default) | `scripts/demo_web.py --host 0.0.0.0 --port $PORT` | the offline demo; safe to bind wide open (see **Why**) |
| `web` | `web.py` with `_web_flags()` (below) | the real web UI |
| `smoke-web` | `scripts/smoke.py --web` plus `_web_flags()` | web UI, writes blocked |
| `cli` | `main.py` | the real CLI |
| `smoke` | `scripts/smoke.py` | CLI, writes blocked |
| `sprint-review-mcp` | `python -m sprint_review.server --root $SPRINT_REVIEW_ROOT`, cwd = the examples folder | R9's server over stdio |
| anything else | exec'd exactly as given (e.g. `bash`) | lets you shell into the image for debugging |

`_web_flags()` always passes `--host 0.0.0.0 --port $PORT`, plus `--allowed-host localhost` and one more `--allowed-host` per name in `PMAGENT_ALLOWED_HOSTS` (comma-separated). Extra arguments after the name are appended as given (`docker run po-agent web --allowed-host pm.example.com`).

`examples` is handled separately, in `main()`: it runs every file `docs/learning/examples/[0-9]*.py`, except any name listed in `NEEDS_EXTRAS` (examples needing a package the image doesn't install — in your rebuild that set can be empty), and exits non-zero if any of them fails.

`main(argv)`:
1. if the command is `examples`, run them and exit — nothing below runs.
2. otherwise compute `(args, cwd)` from `command(argv)`.
3. if the command is `sprint-review-mcp`, create `SPRINT_REVIEW_ROOT` first — a fresh bind mount or volume won't have the folder yet.
4. `os.chdir(cwd)`, then `os.execvp(args[0], args)` — this *replaces* the Python process with the target program; it does not spawn a child.
5. if `execvp` raises `FileNotFoundError` (no such program), exit with `Unknown command '<x>'. Commands: …` instead of a traceback.

`APP` is `Path(__file__).resolve().parent.parent` — the project root, computed from where this file actually lives, never hard-coded to `/app`. `PORT` comes from the environment, default `8000`.

**Why exec, not `subprocess.run`:** `docker stop` sends its signal to PID 1 in the container. `execvp` replaces `container.py` with the target program *in the same process*, so that program becomes PID 1 and receives the signal directly. `subprocess.run` would leave `container.py` as PID 1 and the real program as an unsignalled child. (Run the container with `--init`, or compose's `init: true`, so a real init process still reaps zombies — `exec` alone doesn't give you that.)

**Why `0.0.0.0` is safe for `demo` but not for `web`:** inside a container, `127.0.0.1` is the container's *own* loopback — the host can't reach it at all, so the demo (and `smoke-web`) must bind `0.0.0.0` to be reachable. `web.py`'s own rule from R7 still applies on top of that: a non-loopback bind refuses to start without `PMAGENT_API_TOKEN`, and only names in the `--allowed-host` list are accepted as `Host:`. The demo carries no credentials, so binding it wide open has nothing to protect; `web` does, so its rule still stands even inside the container.

#### `➕ .dockerignore` — decides what's in the build context at all

**Contains:** the opposite of the usual pattern. Instead of listing what to exclude, start with `*` (exclude everything), then add back one `!path` line per thing the image actually needs: `pyproject.toml`, `uv.lock`, `.python-version`, `README.md`, `.env.sample`, `main.py`, `web.py`, `pmagent/`, the handful of scripts the entrypoint runs (`scripts/__init__.py`, `scripts/container.py`, `scripts/demo_web.py`, `scripts/smoke.py`), `docs/learning/examples/` (the course examples, including R9's server), and `tests/__init__.py` + `tests/fakes.py` (the scripted model, since the demo and the examples replay through it). Then re-exclude `**/__pycache__/` and `**/*.py[cod]` even inside the paths just added back, plus two real-data paths under `pmagent/tools/fy_budget/` (fixtures and a provenance note) that must never be in an image.

**Why an allowlist, not a denylist:** a denylist (`.env`, `data/`, `*.zip`, …) only blocks what someone thought to list. The first time a teammate drops a real Jira export, a `.env.local`, or a zip of a deck into the project folder, a denylist lets it straight into the image unless someone remembers to add a new rule. An allowlist can't make that mistake: a new file is excluded by default, and adding a legitimate new file to the image is a deliberate, reviewable one-line change.

#### `➕ Dockerfile` — two stages: resolve dependencies, then run as a non-root user

**Contains, in order:**
1. `ARG PYTHON_VERSION` (matching `.python-version`) picks the base image tag for both stages.
2. **Build stage** (`FROM python:${PYTHON_VERSION}-slim AS build`): copy the `uv` binary in from its own official image (not installed via pip), then `uv sync --locked --no-dev` using only `uv.lock` and `pyproject.toml` — copied in as their own layer, before the rest of the app — so editing application code doesn't invalidate the dependency-install layer. `--locked` fails the build outright if `uv.lock` is out of date. Only after that does `COPY . /app` bring in the rest of the source (filtered by `.dockerignore`).
3. **Final stage** (`FROM python:${PYTHON_VERSION}-slim`, no build tools): create a non-root user, copy the already-built `/app` from the build stage, create the writable subfolders under `data/` (FY budget input/output, sprint-review, and a `home/` used for `HOME`) and `chown` only `data/` to that user, then `USER app`. The application code itself stays owned by root and is read-only to the app user; only `/app/data` is writable.
4. `ENTRYPOINT ["python", "scripts/container.py"]` and `CMD ["demo"]` — the entrypoint script above, defaulting to the safe offline demo if no command is given.
5. No `VOLUME` line.

**Why the app user only owns `data/`:** the whole point of a read-only application image is that nothing running inside it can modify its own code. `data/` is the one place with a legitimate reason to write: FY budget files, R9's sprint-review exports and saved runs, and (via `HOME=/app/data/home`) the spreadsheet lane's token cache.

**Why no `VOLUME` line:** a `VOLUME` instruction makes Docker create an *anonymous* volume automatically on every `docker run` that doesn't otherwise mount something there — one you didn't name and will forget to clean up. Naming your own volume explicitly at run time (`-v po-agent-data:/app/data`) gives you a volume you can find, back up, or delete on purpose, so the Dockerfile leaves this to the caller instead.

#### `➕ compose.yaml` (optional) — the two run modes as `docker compose` services

**Contains:** a `demo` service with no profile (so `docker compose up demo` just works, no `.env` needed) exposing `127.0.0.1:8000:8000`, and a `web` service gated behind a `live` profile (`docker compose --profile live up web`) that adds `env_file: .env`, a named data volume (`po-agent-data:/app/data`), and a health check that hits `/api/v1/health` from inside the container.

**Watch out:** the `web` service's comment is a reminder, not an enforced check — `.env` must set `PMAGENT_API_TOKEN`, because `web.py` still refuses a non-loopback bind without one, `compose.yaml` or not.

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
