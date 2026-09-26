# PO Agent — Strands edition

A local multi-agent project-management assistant: Jira ticket drafting and editing,
sprint health, natural-language Jira and Confluence queries, PRD writing with a
writer↔reviewer loop, FY budget data preparation, spreadsheet change proposals and
diagram briefs. **Anything that writes pauses for your approval first.**

This is a rewrite of [`PO_Agent`](../PO_Agent) (LangGraph) on the
[Strands Agents SDK](https://strandsagents.com). The functionality and the CLI
experience are the same. The domain code was carried over (see
`docs/evidence/verbatim_report.txt` for the few files that changed), and the agent
layer is rebuilt as idiomatic Strands. It doubles as a Strands course: start at
[`docs/learning/README.md`](docs/learning/README.md).

## Run it

```bash
uv sync
cp .env.sample .env     # then fill in LLM + Jira credentials
uv run main.py          # the CLI
uv run web.py           # the web UI → http://127.0.0.1:8000  (API docs: /api/docs)
uv run scripts/demo_web.py   # the web UI fully offline: scripted model, fake Jira, no keys
uv run pytest -q        # full suite: offline, no credentials needed
```

CLI commands: `/new` (fresh conversation), `/help`, `/exit`.

### Run with Docker

The project ships as one image. It holds Python, the locked dependencies and the
app, and runs as a non-root user. `.env` is **never** in the image: the build
context is an allowlist (`.dockerignore`), so credentials are passed when you run
it. The entrypoint, `scripts/container.py`, gives each way to run the project a
name:

```bash
docker build -t po-agent .

docker run --rm --init -p 127.0.0.1:8000:8000 po-agent             # demo: offline web UI, no keys
docker run --rm --init -p 127.0.0.1:8000:8000 --env-file .env po-agent web
docker run --rm --init -it --env-file .env po-agent cli
docker run --rm --init -it --env-file .env po-agent smoke          # CLI, every write blocked
docker run --rm --init -p 127.0.0.1:8000:8000 --env-file .env po-agent smoke-web
docker run --rm --init po-agent examples                           # the course examples, offline
docker run --rm --init -i --network none -v po-agent-data:/app/data po-agent sprint-review-mcp   # lesson 8's MCP server, stdio
docker run --rm --init -it po-agent bash                           # anything else runs as given
```

- **`web` binds `0.0.0.0` inside the container**, because the host can't reach
  the container's own loopback. So `web.py`'s rule for a non-loopback bind
  applies: `.env` must set `PMAGENT_API_TOKEN`, and the page asks for it once.
  `localhost` is an allowed Host; add others with
  `PMAGENT_ALLOWED_HOSTS=pm.example.com,…`. Keep `-p 127.0.0.1:…` unless TLS is in
  front of it.
- **`--env-file` is not python-dotenv.** Docker takes each line literally:
  write `KEY=value` with no quotes, no inline `# comments` and no `export`. A
  quoted value would reach the app with its quotes.
- **Keep `/app/data` in a volume.** It holds FY budget files, the sprint review
  server's exports and runs, and `HOME` (the spreadsheet lane's token cache).
  Name a volume (`-v po-agent-data:/app/data`). A bind mount of a host folder
  needs `--user "$(id -u):$(id -g)"` so the app can write to it. To give the
  sprint review server an export, copy it into the volume:
  `docker run --rm -v po-agent-data:/app/data po-agent cp docs/learning/examples/sprint_review/sample-jira.csv data/sprint-review/`.
- `docker compose up demo` runs the demo. `docker compose --profile live up web`
  runs the real web UI with `.env`, a data volume and a health check
  (`compose.yaml`).
- The image has no test tools. Run `uv run pytest -q` on a checkout;
  `tests/test_docker.py` checks the build context, the Dockerfile and every
  entrypoint command without needing Docker.

### The web UI

The same assistant in a browser, styled after a retro desktop: ink on paper,
monospace chrome, windowed panels. There are two windows:

- **PM Agent**: the chat. Replies are rendered Markdown, so PRD tables render as
  tables. When the agent wants to write, a pink **approval card** shows exactly
  what it will do (the same text as the CLI) with Reject / Approve. Nothing is
  approved by default.
- **Thinking chain**: every step of the selected turn as it happens:
  - which lane was chosen, and why;
  - the model's reasoning summary, when it shares one;
  - each tool call with its input, duration and output;
  - approvals;
  - the PRD writer/reviewer passes;
  - tokens used across all model calls.

The page survives a reload: it rebuilds the chat and chains from the API.
`docs/evidence/ui/` has screenshots from a live run.

Reasoning only appears when the model returns it. For OpenAI set
`LLM_REASONING_EFFORT=high` and `LLM_REASONING_SUMMARY=detailed`. At the default
(`none`) there is none, and the page says so. See
`docs/learning/11-thinking-chain.md`.

### The HTTP API (`/api/v1`, OpenAPI at `/api/v1/openapi.json`)

| Method & path | Returns |
|---------------|---------|
| `GET /api/v1/health` · `GET /api/v1/meta` | liveness · model, project, lanes, reasoning availability |
| `POST /api/v1/conversations` | **201** + `Location` |
| `GET` / `DELETE /api/v1/conversations/{cid}` | the chat + any pending approval · **204** |
| `POST /api/v1/conversations/{cid}/turns` `{"message"}` (optional `Idempotency-Key`) | **202** + `Location`; the agent works in the background |
| `GET /api/v1/conversations/{cid}/turns` · `…/turns/{tid}` | every turn with its events · one turn |
| `GET …/turns/{tid}/events` | **SSE** thinking chain; resumes from `Last-Event-ID`; closes when the turn rests |
| `GET …/approvals/{aid}` · `POST …/approvals/{aid}/decision` `{"decision":"approve"\|"reject"}` | the pending writes · **202**, the turn resumes |

- **Errors** are RFC 9457 `application/problem+json`, and each `type` (e.g.
  `/problems/approval-pending`) opens a page explaining it.
- **Security:**
  - loopback-only Host allowlist (DNS rebinding);
  - Origin check on state-changing requests (CSRF);
  - a strict CSP on the page;
  - vendored JS libraries;
  - optional `PMAGENT_API_TOKEN` bearer auth.

The walkthrough is `docs/learning/10-serving-over-http.md`.

A turn looks like this:

```
You: add the comment "chasing this" to CSCI-1934
  [route: ticket]
  -> add_jira_comment({"issue_keys": ["CSCI-1934"], "comment": "chasing this"})

------------------------------------------------------------
APPROVAL REQUIRED — the agent wants to make a change

  Comment on 1 issue(s): CSCI-1934
    | chasing this
------------------------------------------------------------
Approve? [y/N]
```

### Configuration (`.env`, read only in `pmagent/env.py`)

| Variable | Notes |
|----------|-------|
| `LLM_PROVIDER` | `anthropic` (default) or `openai` |
| `LLM_MODEL` | overrides the per-provider default |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | only the selected provider's key is needed |
| `LLM_REASONING_EFFORT` | OpenAI only. `none` (default) uses chat completions; anything else uses the Responses API |
| `LLM_MAX_TOKENS` | Anthropic only, default 16000 |
| `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` | required. There is no offline mode |
| `JIRA_PROJECT_KEY`, `JIRA_*_FIELD`, `JIRA_SPRINT_NAME_TEMPLATE`, `JIRA_STUCK_THRESHOLD_DAYS` | as in the original. **Verify the `customfield_*` ids for your tenant** |
| `CONFLUENCE_BASE_URL`, `CONFLUENCE_SPACE_KEY` | optional; same Atlassian credentials |
| `FY_BUDGET_INPUT_DIR`, `FY_BUDGET_OUTPUT_DIR` | FY budget converter folders |
| `SPREADSHEET_TENANT_ID`, `SPREADSHEET_CLIENT_ID` | Microsoft Graph spreadsheet lane |
| `LUCID_MCP_ENABLED` | `true` to bind Lucid's MCP tools to the diagram lane (off by default) |
| `PMAGENT_LOG_LEVEL` | show Strands' own logs (e.g. `DEBUG`); hidden by default |
| `LLM_REASONING_SUMMARY` | OpenAI with effort ≠ none: `auto` / `concise` / `detailed` reasoning summaries for the web thinking chain (unset = none) |
| `PMAGENT_API_TOKEN` | web server: require `Authorization: Bearer <token>`. Needed for any non-loopback bind, together with `web.py --allowed-host <name>` for `0.0.0.0`, and TLS in front off-machine |

### Live smoke test (safe against a production Jira)

```bash
uv run scripts/smoke.py          # the CLI
uv run scripts/smoke.py --web    # the web UI (in-process server, same block)
```

This is the same CLI, with every HTTP write blocked below the app. It uses a
fail-closed allowlist on `requests.Session.request`: GETs, the JQL search POST and
the Microsoft token refresh are allowed, and anything else raises `smoke: write blocked`. The FY converter and
Lucid MCP are disabled too. Answering `y` at an approval prompt is therefore safe.
Transcripts are in [`docs/evidence/`](docs/evidence).

## Architecture

```
main.py                      terminal I/O + the approval prompt loop (only)
web.py                       the web UI + HTTP API (uvicorn, in-process, loopback)
pmagent/
  assistant.py               PMAssistant: classify → lane → run; one shared history; interrupt/resume
  gate.py                    ApprovalGate hook: pauses any tool batch with a write (fails closed)
  llm.py                     get_model() — the one place a provider is chosen; structured()
  messages.py                readers over Strands' message dicts
  schemas.py                 TicketDraft, PRD, ReviewResult, DiagramBrief, RouteDecision
  agents/
    common.py                make_lane_agent(): every lane = strands.Agent + ApprovalGate
    router.py                classify(): continuation fast-path + structured-output router
    query_agent.py  ticket_agent.py  sprint_agent.py  finance_agent.py
    spreadsheet_agent.py  diagram_agent.py          each lane = SYSTEM_PROMPT + TOOLS
    requirements.py          writer ↔ reviewer loop + deterministic PRD renderer
  cli/
    approval.py              describe_write, draft manifest, scope warning
    echo.py                  ConsoleEcho hook: prints the conversation
  web/
    app.py                   FastAPI: routes, security middleware, SSE endpoint
    sessions.py              conversations → turns (event log, worker thread) → approvals
    trace.py                 TraceRecorder: hooks + callback handler → thinking-chain events
    problems.py              RFC 9457 problem+json, and a page per problem type
    schemas.py               request/response models → OpenAPI
    static/                  index.html, app.css, app.js, vendor/ (marked, DOMPurify)
  tools/                     domain code, carried over from the original
    jira/  confluence_tools.py  finance_tools.py  fy_budget/  spreadsheet_tools.py
    diagram_tools.py  mcp_tools.py  adf.py  company_knowledge.py
  prompts/  skills/          Markdown prompts and domain knowledge
scripts/smoke.py             write-blocked live launcher (CLI, or --web)
scripts/web_drive.py         drive the live API and record the thinking chain (JSONL)
scripts/ui_screenshots.py    drive the page in Chromium and screenshot it
scripts/build_course.py      docs/learning/*.md → docs/learning/course.html
scripts/demo_web.py          the web UI offline (scripted model + fake Jira) — used by lessons 10–11
scripts/course_figures.py    screenshots of the offline demo for the published course
scripts/container.py         the Docker image's entrypoint (demo, web, cli, smoke, sprint-review-mcp, …)
Dockerfile  .dockerignore  compose.yaml   the image: uv-locked, non-root, allowlisted build context
tests/                       offline suite; tests/fakes.py = ScriptedModel
docs/learning/               the Strands course (17 lessons, runnable examples, course.html)
docs/PLAN.md                 the reviewed implementation plan (CLI)
docs/PLAN_WEB.md             the reviewed plan for the web UI + API
docs/EVIDENCE.md             what was verified, and how
```

Routes: `ticket`, `sprint`, `query`, `requirements`, `spreadsheet`, `diagram`,
`finance`. An unknown route falls back to the read-only query lane. A pure
confirmation ("yes, create them") keeps the previous lane without a model call.

### Adding a tool

- **Read tool.** Define it with `@tool` in its module, add it to the module's
  `READ_TOOLS`, add it to every lane that could need it (including
  `query_agent.TOOLS`), and name it in those lanes' prompts.
- **Write tool.** Define it, add it to `WRITE_TOOLS`, add its name to
  `gate.WRITE_TOOL_NAMES`, and add it to the lanes and prompts. Add a case in
  `pmagent/cli/approval.py::_describe_write`. Then run `uv run pytest -q`: the
  tests fail on any step you missed.

Keep docstring prose **above** `Args:`. Strands drops what comes after it
(`tests/test_tool_specs.py`).

## Differences from the LangGraph original (deliberate)

1. **The gate fails closed.** A tool the agent has is gated unless it is a declared
   read (module `READ_TOOLS`, or a reviewed MCP tool in `APPROVED_READ_TOOLS`).
   The original gated only known write names, so an unreviewed MCP tool would have
   run ungated. A tool name the agent does *not* have is not gated, because it
   can't run; Strands returns "Unknown tool".
2. **Rejecting cancels the whole batch**, including a read bundled with the write.
3. **The diagram lane is reachable.** It always has the local brief tool, and Lucid
   tools are added when `LUCID_MCP_ENABLED=true`. In the original, the lane only
   existed in an async graph that nothing called.
4. **Confluence images ride inside the tool result.** Strands' OpenAI provider moves
   them to a user message itself, so the original's injected-message workaround is
   gone.
5. **Three tool docstrings were re-ordered** so their prose sits above `Args:`. The
   content is unchanged.
6. **An abandoned approval is closed explicitly**: a new message, a crash mid-turn,
   `/new` or Ctrl-D. The pending tool call gets a "Not executed" result, and the
   paused lane agent is rebuilt.
7. Not ported: the `snowflake/` track, the original's `docs/` course,
   `TUTORIAL.md`, LangSmith and LangGraph Studio.

History is unbounded, as in the original (`NullConversationManager`). A very long
session eventually hits the model's context limit; the CLI then says to `/new`.
