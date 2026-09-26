# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

PO_Agent (`../PO_Agent`, LangGraph) rewritten on Strands Agents 1.57. The CLI is
`uv run main.py`. It is also the owner's project for learning Strands, so keep the
agent layer idiomatic and explained. Every module in the agent layer has a
docstring that names the Strands concept it teaches and how the LangGraph original
did it. Keep that up when you edit. The course lives in `docs/learning/`.

## Commands

```bash
uv sync
uv run main.py                 # CLI (needs .env: LLM key + Jira creds)
uv run web.py                  # web UI + API on 127.0.0.1:8000
uv run scripts/demo_web.py     # the web UI offline (scripted model, fake Jira) — safe, no keys
uv run scripts/smoke.py [--web]  # CLI or web with every HTTP write blocked — use this for live checks
uv run scripts/build_course.py # regenerate docs/learning/course.html after editing a lesson
uv run pytest -q               # offline suite, no credentials
uv run scripts/verbatim_report.py   # which files still match the original
docker build -t po-agent . && docker run --rm --init -p 127.0.0.1:8000:8000 po-agent   # the image (demo)
```

**Never run `main.py` against the real `.env` to test a write path.** `.env` points
at a production Jira. Use `scripts/smoke.py`, where approving is safe.

## Distribution: the Docker image

The project ships as a Docker image (`Dockerfile`, `compose.yaml`, entrypoint
`scripts/container.py`). Keep it that way:

- **Linux and Python only.** No PowerShell or other Windows-only scripts; port
  anything that arrives as one (see `docs/learning/examples/sprint_review/analyser.py`).
- **`.dockerignore` is an allowlist.** A new top-level file or folder the app needs
  must be added there, and `tests/test_docker.py` lists what must and must not be in
  the image. `.env`, `data/` and `docs/evidence/` never go in.
- **A new way to run the project is a named command** in `scripts/container.py`,
  with a case in `tests/test_docker.py`. A new offline example goes in
  `OFFLINE_EXAMPLES` (a test enforces it).

## Rules that the tests enforce

- **Every tool module declares `READ_TOOLS` and `WRITE_TOOLS`.** Every write name
  is also in `pmagent/gate.py::WRITE_TOOL_NAMES`, and every gated tool has a case in
  `pmagent/cli/approval.py::_describe_write` (`tests/test_agent_lanes.py`). A new
  tool module must be added to `pmagent/gate.py::TOOL_MODULES`, or its reads get
  gated.
- **Docstring prose goes above `Args:`**, because Strands drops text after it
  (`tests/test_tool_specs.py`).
- **Docs cite code as `path::Symbol`**, and `tests/test_learning_docs.py` checks
  every citation.
- **The gate is on every lane agent.** Build lane agents only with
  `pmagent/agents/common.py::make_lane_agent`.

## Design rules (carried over from the original — read its CLAUDE.md for history)

- **The LLM does judgment; plain Python does arithmetic and formatting.** Sprint
  metrics, PRD rendering and the FY budget converter are deterministic.
- **Domain code is framework-free.** `pmagent/tools/**` must not import Strands
  except `from strands import tool`. `JiraClient` and `ConfluenceClient` are built
  lazily (`get_client()`), never at import time.
- **Prompts and skills are Markdown** (`pmagent/prompts/`, `pmagent/skills/`). Inject
  a skill with `prompts.inject_skill`, not `str.format`.
- **Stateless model calls go through `pmagent/llm.py::structured`.** It uses a fresh
  agent, no history, no printing and no hooks. Never run a structured-output call on
  a lane agent: the hidden output tool would land in the shared conversation.
- **Tool results are `role: "user"` messages in Strands.** Use
  `pmagent/messages.py::last_user_text` to find what the human typed.
- **All lanes share `PMAssistant.messages`.** An unanswered interrupt wedges its
  agent, so any path that abandons one must call `PMAssistant.discard_pending`.

## Web layer rules (`pmagent/web/`)

- **The web layer never forks agent logic.** It drives `PMAssistant` exactly like
  `main.py` does. Approval text comes from `cli/approval.py`, and failures call
  `discard_pending()`. Any new behaviour belongs in `PMAssistant`, with both
  frontends picking it up.
- **Never block the event loop.** SSE waits go through `anyio.to_thread.run_sync`
  with `SSE_LIMITER`; `tests/test_web_concurrency.py` (real uvicorn) guards this.
- **Keep the page CSP-clean.** No inline `<script>`, `on*=` handlers or `style=`
  in `static/*.html` (a test greps for them). Libraries are vendored in
  `static/vendor/`, with their hashes recorded in its README. Model output is
  rendered only through DOMPurify with `img` forbidden.
- **Every error is a problem.** Raise `ProblemError("<slug>")`, and add new slugs
  to `problems.CATALOG` (each becomes a `/problems/<slug>` page).
- **New thinking-chain step:** emit it from `TraceRecorder`, render it in
  `app.js::renderChain`, document it in `docs/learning/11-thinking-chain.md`, and
  update the event-order assertions in `tests/test_web_api.py`.
- **Browser tests** (`tests/test_web_ui.py`) skip unless Playwright's Chromium launches. On
  WSL, set `PMAGENT_CHROMIUM_LIBS` to extracted libnss3/libnspr4/libasound (see
  `scripts/ui_screenshots.py`).
- **Editing a lesson** means re-running `scripts/build_course.py`
  (`tests/test_course_build.py` fails otherwise).

## Testing

`tests/fakes.py::ScriptedModel` replays scripted assistant turns through the real
Strands loop. A structured reply is a call to the tool named after the schema
(`fakes.structured(Schema, ...)`). Prefer it over mocking Strands internals.

Domain tests inject the boundary instead of adding a fake mode: a client built
with `__new__` and a fake `_session`, or a monkeypatched `get_client`. Real budget
files are never committed; `tests/fy_synthetic.py` builds synthetic workbooks in
both layouts. The rebuild guide (`docs/learning/rebuild.md`,
`docs/learning/rebuild-domain.md`) is self-contained: the learner copies nothing
from this repository and writes their own tests from each step's case table. When
behaviour changes here, update the matching step's table and spec too.
