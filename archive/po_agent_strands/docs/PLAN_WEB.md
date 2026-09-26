# Plan (part 2): web frontend + HTTP API + thinking chain

Builds on `docs/PLAN.md` (the CLI rewrite, done; see `docs/EVIDENCE.md`).

## 1. The request

1. A web frontend in a TypeSafe AI–*inspired* style that gives the same experience
   as `uv run main.py`: chat, the seven lanes, `/new`, and approval before any write.
2. The page must show **the agent's thinking chain**.
3. The HTTP API behind it must follow API best practice.
4. Everything goes into the course (as a published web page) and into every
   document (README, CLAUDE.md, EVIDENCE, the LangGraph→Strands map).

### Style reference, measured from typesafe.ai's HTML

- **Palette:** ink `#1E1E1E` on paper `#FEFEFE`; greys `#DEDEDE` and `#C4C4C4`;
  accent pink `#F386A1`; secondary magenta `#D45BB6`.
- **Type:**
  - monospace-first: their LisaTerminal display font (commercial), JetBrains Mono
    and Fragment Mono;
  - "Die Grotesk" (commercial) for sans.
  - Substitutes: **JetBrains Mono / Fragment Mono** (Google Fonts) and **Space
    Grotesk** for sans.
- **Shape:** sharp corners (0–4px), 1px ink borders, no soft shadows.
- **Chrome:** content lives in retro desktop "windows" with a title bar and version
  tag ("Clock Tool 1.1").
- **Branding:** we copy the *aesthetic* only. Our own name ("PM Agent"), no
  TypeSafe name, logo or copy.

### What "thinking chain" means here, and what is honest to show

Everything the agent actually did during a turn, as a live timeline:

| Step | Source |
|------|--------|
| route decision + *why* (continuation fast-path vs classifier) | router |
| model reasoning text, **when the provider returns it** | callback handler `reasoningText` deltas |
| assistant text per model step (streamed) | callback handler `data` deltas |
| tool call started (name, args) / finished (status, duration, output preview, image count) | `BeforeToolCallEvent` / `AfterToolCallEvent` |
| approval required / decided | ApprovalGate interrupt / resume |
| PRD loop: writer pass N, reviewer verdict (approved, missing requirements) | new `on_step` callback in the workflow |
| token usage for the turn | `AgentResult.metrics` |

**Verified live.** `gpt-5.6-luna` via `OpenAIResponsesModel` with
`reasoning={"effort":"medium","summary":"detailed"}` streams reasoning summaries
through Strands' `reasoningText`.

- At `LLM_REASONING_EFFORT=none` (chat completions, the default) **no reasoning is
  returned**. The UI then shows the other steps and a note saying so.
- Change: when effort is not `none`, `llm.get_model` also sends
  `"summary": "auto"` (overridable with `LLM_REASONING_SUMMARY`).
- Anthropic extended thinking would arrive through the same `reasoningText`
  channel. It is not verifiable here (no Anthropic key), so it is documented as
  untested and no extra code is written for it.
- Strands drops `reasoningContent` blocks when it rebuilds a multi-turn Responses
  request (`openai_responses.py`), so the shared history is safe.

## 2. Architecture

```
browser (static HTML/CSS/JS, no build step)
   │  REST (JSON)                      │  SSE (text/event-stream)
   ▼                                   ▼
pmagent/web/app.py  FastAPI  /api/v1/...   ──►  ConversationSession (one per conversation)
                                                  ├─ PMAssistant  (unchanged public API)
                                                  ├─ TraceRecorder (hook + callback handler)
                                                  └─ Turn: event log + status, worker thread
```

- **The web layer reuses `PMAssistant` as-is.** The CLI and the web differ only in
  their I/O adapter, which is the point the original `main.py` docstring made
  ("a second frontend … without duplicating any logic").
- **One worker thread per running turn.** Strands' `agent()` is synchronous. Each
  conversation has a lock, so there is one turn at a time per conversation
  (409 otherwise). Separate conversations run independently.
- **Events are appended to the turn's log**, with a monotonically increasing `id`,
  and a `threading.Condition` wakes waiters. The SSE endpoint replays from
  `Last-Event-ID`, then streams live.
- In-memory storage only, like the CLI and the original `MemorySaver`: a restart
  loses conversations. This is documented.

### Small, additive changes to existing modules

| Module | Change | CLI impact |
|--------|--------|-----------|
| `agents/common.py::make_lane_agent` | new optional `callback_handler=` (default `None`) | none |
| `assistant.py::PMAssistant.__init__` | new optional `callback_handler=`, passed through to lane agents; `on_route` receives `(route, reason)` where reason ∈ `continuation` / `classifier` / `fixed` | `_RoutePrinter` accepts and ignores reason |
| `agents/router.py` | `classify_with_reason()`; `classify()` keeps its return type | none |
| `agents/requirements.py::run_requirements(_loop)` | optional `on_step(kind, data)` | none |
| `llm.py::get_model` | Responses path adds a reasoning summary | CLI unaffected (reasoning isn't printed) |

## 3. HTTP API (`/api/v1`)

Resources: **conversations** → **turns** (each with an **events** stream) and
**approvals**.

| Method & path | Success | Purpose |
|---------------|---------|---------|
| `GET /api/v1/health` | 200 `{"status":"ok"}` | liveness; no auth |
| `GET /api/v1/meta` | 200 | model, provider, Jira project, lanes, reasoning availability; for the header |
| `POST /api/v1/conversations` | **201** + `Location` | create (the CLI's `/new` = create another) |
| `GET /api/v1/conversations/{cid}` | 200 | id, created_at, route, status, transcript (text only), pending approval if any |
| `DELETE /api/v1/conversations/{cid}` | **204** | discard (abandons any pending approval) |
| `POST /api/v1/conversations/{cid}/turns` `{"message": "…"}` | **202** + `Location` → the turn | start a turn in the background |
| `GET /api/v1/conversations/{cid}/turns/{tid}` | 200 | status `running | awaiting_approval | completed | failed`, reply, error, events so far |
| `GET /api/v1/conversations/{cid}/turns/{tid}/events` | 200 `text/event-stream` | the thinking chain, live. Replays from `Last-Event-ID` or `?after=`. Heartbeat comment every 15s. **Closes when the turn reaches a resting state** (`awaiting_approval`, `completed`, `failed`); the client reconnects with `Last-Event-ID` after deciding |
| `GET /api/v1/conversations/{cid}/approvals/{aid}` | 200 | the pending writes: tool, args, human-readable `description` (same `describe_write` as the CLI), scope `warning` |
| `POST /api/v1/conversations/{cid}/approvals/{aid}/decision` `{"decision":"approve"\|"reject"}` | **202** | resume the same turn; events continue on the turn's stream |

### Best-practice rules applied

1. **Nouns, not verbs; versioned prefix** `/api/v1`. The decision is a sub-resource,
   not `/approve`.
2. **Correct status codes.** 201 + `Location` on create; 202 + `Location` for async
   work; 204 on delete; 404 unknown id; **409** for a turn while one is running,
   a turn while an approval is pending, or a decision on an approval that is not
   pending; 422 validation; 401 bad or missing token (when enabled).
3. **One error format: RFC 9457 `application/problem+json`** —
   `{type, title, status, detail, instance}` with stable `type` URIs
   (`/problems/turn-in-progress`, `/problems/approval-pending`,
   `/problems/not-found`, `/problems/validation`, `/problems/unauthorized`).
   FastAPI's default 422 and 404 bodies are replaced.
4. **Typed request/response schemas** (Pydantic) → an accurate OpenAPI 3.1 document
   at `/api/v1/openapi.json`, with interactive docs at `/api/docs`.
5. **Idempotency.** `POST …/turns` honours an `Idempotency-Key` header: a retry
   with the same key returns the *same* turn (202, same `Location`) instead of
   running the message twice. Reusing a key with a different body → 422
   (`/problems/idempotency-key-reuse`). A decision is naturally idempotent: repeating
   the same decision → 202 with the same result; a *different* decision after one
   is recorded → 409.
6. **Consistent shapes.** snake_case fields, ISO-8601 UTC timestamps, opaque
   string ids (`conv_…`, `turn_…`, `apr_…`), explicit `status` enums.
7. **Security.**
   - Binds to `127.0.0.1` by default.
   - Optional bearer auth: if `PMAGENT_API_TOKEN` is set, every `/api/v1` route
     except `health` needs `Authorization: Bearer <token>`. SSE auth works via the
     header, since the UI uses `fetch()` streaming, not `EventSource`.
   - Request size cap: `message` ≤ 20,000 chars (422 over it).
   - No CORS: the UI is same-origin.
   - Agent Markdown is sanitised with DOMPurify before rendering; tool output is
     inserted as text, never HTML.
8. **SSE event contract** (stable, documented). Each event has `id`, `event`
   (type) and `data` (JSON with `ts` and `turn_id`). Types:
   - `turn.started`, `route.decided`, `reasoning.delta`, `text.delta`,
     `message.completed`
   - `tool.started`, `tool.finished`
   - `approval.required`, `approval.decided`
   - `prd.step`, `usage`
   - `turn.completed`, `turn.failed` (the latter carries a problem object)
9. **Not included, and documented as such:** persistence, rate limiting,
   multi-user auth, pagination (a conversation is small). The API is for a
   single local user.

## 4. Frontend (`pmagent/web/static/`)

- Static `index.html` + `app.css` + `app.js`, served by FastAPI at `/`. No build
  step. Vanilla JS modules, `fetch` + a ReadableStream SSE parser. marked +
  DOMPurify come from jsdelivr for Markdown (PRD tables).
- **Layout** (desktop): two windows side by side.
  - Left: `PM Agent 1.0` (chat). Messages, a composer, and `New` / `Help` buttons.
  - Right: `Thinking chain 1.0`, a live timeline for the selected turn. Each step
    has a mono label, time, and a collapsible detail (args JSON, output preview).
  - Reasoning streams into a dimmed block.
  - A route badge shows the lane and why it was chosen.
- On mobile the windows stack.
- **Approval card** in the chat window (pink accent border). It shows the same
  `describe_write` text as the CLI plus any scope warning, with **Approve** /
  **Reject**. Nothing is approved by default, and Enter does not approve.
- A status line shows model, Jira project, and whether reasoning is available.
  `[route: x]` parity with the CLI comes from the route badge.
- Clicking a past turn in the chat shows that turn's chain (events are kept on the
  turn).
- Accessibility: buttons are real `<button>`s, focus is visible, and
  `prefers-reduced-motion` is respected.

## 5. Entry points and tooling

- `uv run web.py` starts uvicorn on 127.0.0.1:8000 (`--host/--port`). It
  validates config like `main.py`.
- `uv run scripts/smoke.py --web` gives the same fail-closed write block, around
  the web server.
- Dependencies: `fastapi` (+ uvicorn/sse-starlette/httpx, already present).
  Dev: `playwright` for a UI smoke and screenshots, only if Chromium can be
  installed; otherwise a curl-driven API smoke.

## 6. Tests (offline)

- `tests/test_web_api.py` uses FastAPI `TestClient` + a `PMAssistant` factory with
  `ScriptedModel` and a stub classifier. It covers:
  - create 201 + `Location`; get; delete 204 then 404;
  - turn 202 + `Location`; stream events end at a resting state, with the expected
    order: `turn.started` → `route.decided` → `tool.started` → `tool.finished`
    → `message.completed` → `turn.completed`;
  - the write flow: `approval.required` → GET approval (description text) →
    decision 202 → reconnect with `Last-Event-ID` → no replayed ids, and the
    `approval.decided` … `turn.completed` events arrive;
  - reject: the tool never runs;
  - 409 for a turn while running, a turn while an approval is pending, and a
    second, different decision;
  - 404, 422 and 401 all as `application/problem+json` with the right `type`;
  - `Idempotency-Key`: same key → same turn, one model run; same key with a
    different body → 422;
  - the requirements lane emits `prd.step` events;
  - reasoning deltas (a ScriptedModel reasoning block) → `reasoning.delta`
    events;
  - the OpenAPI doc lists every route; message-length cap → 422.
- `tests/test_trace.py`: TraceRecorder maps hook and callback events to trace
  events correctly (unit level).
- The existing suite stays green; the CLI is untouched except `_RoutePrinter`'s
  signature.
- `ScriptedModel` gains a `reasoning(text)` block helper, emitted as a
  `reasoningContent` delta.

## 7. Live evidence (safe)

- `scripts/smoke.py --web` against the real model and Jira: drive the API with a
  script (httpx) through read, write-reject and write-approve, where the approve
  is blocked at HTTP. Save the SSE event logs to `docs/evidence/web_*.jsonl`.
- With `LLM_REASONING_EFFORT=medium` for one run, show `reasoning.delta` events
  arriving live.
- If Playwright/Chromium is available: screenshots of the UI mid-turn (thinking
  chain), at an approval card, and on a PRD, saved to `docs/evidence/`.

## 8. Course and documents

- New lessons:
  - **10 — Serving an agent over HTTP.** Resource design, async work with
    202 + Location, RFC 9457, idempotency, and why the agent layer didn't change.
  - **11 — Streaming the thinking chain.** Callback handler vs hooks, SSE,
    `Last-Event-ID`, what "reasoning" can and can't be shown.
- Example `09_trace_events.py`: a hook + callback recorder printing the event
  stream offline.
- **The web course:** one self-contained HTML page (`docs/learning/course.html`)
  rendering all 11 lessons, the concept map and the API reference, in the same
  visual style. It is published as a private Artifact. `tests/test_learning_docs.py`
  keeps covering the Markdown sources.
- Update README (run the web UI, API table), CLAUDE.md (web rules),
  EVIDENCE.md (a new section), LANGGRAPH_TO_STRANDS.md (frontends), and the course
  index.

## 9. Order of work

1. Router reason + requirements `on_step` + callback passthrough + tests.
2. `pmagent/web/trace.py` + `sessions.py` + tests.
3. `pmagent/web/app.py` (API, problems, auth, idempotency) + `test_web_api.py`.
4. Static frontend.
5. `web.py`, `smoke.py --web`, live API smoke, screenshots.
6. Lessons 10–11, example 09, docs updates, `course.html`, publish.
7. Final audit by the independent reviewer; evidence.

## 10. Revisions after review 1 (these supersede anything above)

Review 1: NEEDS CHANGES, with 4 major and 9 minor findings. All are accepted.

**R1. SSE never blocks the event loop.** The SSE generator waits for new events
off-loop: `await anyio.to_thread.run_sync(turn.wait_for_events, after, 15.0)`,
which blocks a threadpool thread, not the loop. Test: a real uvicorn server on an
ephemeral port in a background thread. One SSE stream is held open (the turn is
parked mid-run on a gate event in the scripted model), and meanwhile
`GET /api/v1/health` and another conversation's full turn complete.

**R2. Failure cleanup, the same as the CLI.**
- The worker's `except` calls `assistant.discard_pending()` before emitting
  `turn.failed`.
- `ContextWindowOverflowException` → problem `/problems/context-full` ("start a
  new conversation").
- Test: a scripted model outage during the resume after approve; then a new turn
  is accepted (202).

**R3. DNS rebinding and CSRF.**
- `TrustedHostMiddleware(allowed_hosts=["127.0.0.1", "localhost", <--host>])`.
- Any request whose `Origin` header is present and not the server's own origin →
  403 problem `/problems/forbidden-origin`, for every non-GET request.
- JSON bodies require `Content-Type: application/json` (FastAPI enforces it).
- Tests: a wrong Host → 400; a foreign Origin on decision → 403; a `text/plain`
  body → 422.

**R4. Exfiltration through rendered Markdown.**
- Every response carries a strict CSP:
  `default-src 'self'; img-src 'self' data:; connect-src 'self'; script-src 'self';
  style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com;
  frame-ancestors 'none'; base-uri 'none'; form-action 'none'`.
  It also sets `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`.
- marked and DOMPurify are **vendored** into `static/vendor/` (pinned versions,
  recorded in a README there), so the UI works offline and loads no CDN script.
- DOMPurify runs with `FORBID_TAGS: ['img','style','form','iframe','svg','math']`,
  and links get `target=_blank rel="noopener noreferrer"`.
- Tests: CSP and nosniff on `/` and on the API.
- Auth token in the UI: when `/api/v1/meta` returns 401, the page asks for the
  token in an in-page field and keeps it in `sessionStorage` only.

**R5. Token usage.**
- `llm.structured` gains an optional `usage_sink` callable that receives
  `result.metrics.latest_agent_invocation.usage`.
- `PMAssistant` sums, per turn: the lane invocations (send + each resume, from
  `latest_agent_invocation`), the router call, and the requirements writer and
  reviewer calls. The diagram brief passes a sink through `invocation_state` →
  `tool_context.invocation_state`.
- New field `TurnResult.usage` (default empty; the CLI ignores it).
- UI label: "tokens this turn (all model calls)".
- Documented: `agent_invocations` grows per lane agent for the process lifetime.

**R6. Reasoning summaries are opt-in.**
- New `LLM_REASONING_SUMMARY` (unset by default → no summary sent). It only
  applies when effort ≠ none.
- `meta.reasoning_available` is true only when both are set.
- Docs say it is a condensed summary, not the raw chain of thought. OpenAI may
  require organisation verification for summaries.

**R7. `on_route` is unchanged.**
- New separate optional callback `on_route_decided(route, reason)`.
- Reason is `continuation` / `classifier` from `router.classify_with_reason`, and
  `custom` when a `classify` function is injected.
- `TurnResult.route_reason` is added too. Existing tests are untouched.

**R8. Approval ids are minted by the web layer**: `apr_<random>` per
`approval.required`. Strands' interrupt id (a uuid5 of the name, the same for
every approval) stays internal. The decision's 202 carries `Location` → the turn.
Test: two consecutive approvals in one turn.

**R9. Idempotency** (after draft-ietf-httpapi-idempotency-key-header):
- The key is scoped per conversation and kept in memory for its lifetime.
- The value stored is a SHA-256 of the canonical JSON body (sorted keys).
- The key is recorded only once the 202 is minted.
- A concurrent duplicate while the first is being created → 409
  `/problems/idempotency-key-in-use`.
- A reuse with a different body → 422 `/problems/idempotency-key-reuse`.
- A retry → 202, same turn, same `Location`.
- Problem `type` URIs are absolute paths `/problems/<slug>`, and each is served as
  a small HTML page, so they dereference.

**R10. Lifecycles.**
- The MCP session starts and stops in the FastAPI lifespan.
- `DELETE /conversations/{cid}` calls only `discard_pending()`; it never calls
  `PMAssistant.close()`.
- Anthropic: the web layer builds one model per conversation (`llm.build_model()`,
  uncached), because the cached `AnthropicModel` holds one async client used across
  event loops. OpenAI keeps the shared cached model (a client per request).
  Anthropic concurrency is untested and documented as such.

**R11. Dependencies and ops.**
- `sse-starlette` is declared explicitly.
- SSE responses set `Cache-Control: no-cache`, `X-Accel-Buffering: no` and
  `retry: 3000`.
- `web.py` and `smoke.py --web` run uvicorn **in-process** (`uvicorn.Server`,
  `workers=1`, no reload). `smoke.py --web` installs and self-checks the write
  block in the serving process, and asserts it isn't a reload/worker setup.

**R12. `course.html` is generated.**
- `scripts/build_course.py` renders `docs/learning/*.md` into one styled page
  (Python `markdown`, a dev dependency), rewriting `.md` cross-links to in-page
  anchors.
- `tests/test_course_build.py` asserts the committed `course.html` equals the build
  output.

**R13. CLI parity gaps.**
- `GET …/turns` lists every turn with its events, so a reload rebuilds the chat and
  the chains.
- `turn.failed` is rendered inline and the composer is re-enabled.
- A pending approval is part of `GET conversation`, so the card survives a reload.
- The approval `description` and `warning` come from exactly
  `describe_write(call)` and `unchecked_scope_warning(assistant.messages, calls)`.
  Test: they are string-equal to what `main.py::resolve_approvals` prints for the
  same calls.
- Since the web returns 409 on a new turn while an approval is pending (explicit,
  unlike the CLI's implicit discard), the UI shows *Approve / Reject*, and
  `DELETE` (New conversation) abandons it.

## 11. Review log

- Review 1: NEEDS CHANGES (4 major, 9 minor) → §10 R1–R13.
- Review 2: first attempt lost to a reviewer rate limit; retried → **PASS** (0 blocker,
  0 major). Minors adopted as implementation notes:
  1. SSE waits use `anyio.to_thread.run_sync(..., abandon_on_cancel=True,
     limiter=SSE_LIMITER)` with a dedicated `CapacityLimiter(16)`. `wait_for_events`
     returns at resting states. The concurrency test uses a real `uvicorn.Server`
     on port 0, a per-conversation ScriptedModel parked on a `threading.Event`
     (wait with a timeout, released in `finally`), and asserts `turn.started` was
     received before the concurrent calls.
  2. Origin = `scheme://Host` of the request, compared exactly (`null` is foreign).
     Tests use `base_url="http://127.0.0.1"`. A bad Host returns a problem+json
     `/problems/bad-host` from our own middleware, not Starlette's text/plain.
     IPv6 `[::1]` is not allowed (documented).
  3. The static page has no inline script, handlers or styles, and a test greps for
     them. A relaxed CSP applies on `/api/docs` only; ReDoc is disabled. A test
     checks the strict/relaxed CSP split.
  4. `invocation_state={"usage_sink", "model"}` → `draft_diagram_brief` reads both
     from `tool_context`, and falls back to `get_model()` for the CLI.
  5. The usage sink sums field by field under the turn's lock.
  6. Approvals have a status (`pending/approved/rejected/abandoned`). Discard and
     DELETE mark it abandoned and emit `approval.decided`
     (`decision: "abandoned"`). A later decision → 409 `/problems/approval-not-pending`.
  7. An idempotent retry creates no second turn and appends no events (tested via
     `GET …/turns`).
  8. `build_course.py` uses `markdown` with `tables`, `fenced_code` and `toc`. The
     published course page is self-contained with inline styles (the Artifact
     contract), which is a separate variant from the app's strict CSP.
