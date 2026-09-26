# 11. Streaming the thinking chain

## What "thinking chain" means, and what it can honestly show

The web UI's right-hand window shows everything the agent **did** during a turn,
live:

| Step | Where it comes from | Strands concept |
|------|--------------------|-----------------|
| route → lane, and *why* (continuation fast path vs classifier) | `PMAssistant(on_route_decided=…)` | plain Python (lesson 7) |
| model reasoning (a summary), when the model shares it | callback handler `reasoningText=` deltas | **callback handler** |
| the reply as it's written | callback handler `data=` deltas | **callback handler** |
| each tool call: input, then status, duration, output, image count | `BeforeToolCallEvent`, `AfterToolCallEvent` | **hooks** (lesson 5) |
| each completed model message (text, tools it asked for) | `MessageAddedEvent` | hooks |
| paused before a write / approval decided | the gate's interrupt → the web session | **interrupts** (lesson 6) |
| PRD writer pass N, reviewer verdict, render | `run_requirements(on_step=…)` | workflow (lesson 7) |
| tokens this turn, across **all** model calls | `result.metrics.latest_agent_invocation.usage` via usage sinks | metrics |

One class collects all of it: `pmagent/web/trace.py::TraceRecorder`. It is a
`HookProvider` *and* a callback handler, and `TraceRecorder.assistant_kwargs`
wires it into `PMAssistant`. Callback handler vs hooks: the handler sees
**stream-level** deltas, which make the UI feel live. Hooks see **structured
lifecycle** events, which make the chain precise. You want both.

### About "reasoning"

Be precise with learners and users here.

- What the page shows is a **reasoning summary**, when the provider returns one.
  It is not raw chain-of-thought. OpenAI's Responses API returns summaries on
  request. Anthropic's extended thinking would arrive through the same
  `reasoningText` channel, but that is untested here.
- Measured on gpt-5.6-luna through Strands
  (`docs/evidence/web_live_reasoning.jsonl`):
  - at `LLM_REASONING_EFFORT=none` (the default) there is **no** reasoning;
  - at `medium`, summaries appear only sometimes. The model often skips them,
    especially with a long system prompt;
  - at `high` with `LLM_REASONING_SUMMARY=detailed`, they appear consistently.
    One live turn streamed 275 reasoning deltas.
- Everything else in the chain (route, tools, approvals, PRD passes, usage) is
  **always** there, because it comes from the agent's actual actions, not from
  the model narrating itself. That is the more trustworthy half.

`pmagent/llm.py::reasoning_available` decides what `/api/v1/meta` tells the page,
and the page prints that note under the chain.

## Server-Sent Events, the way this API does them

```
GET /api/v1/conversations/{cid}/turns/{tid}/events
Last-Event-ID: 41                      ← optional: resume after event 41

id: 42
event: tool.started
data: {"ts": "...", "turn_id": "turn_…", "tool_use_id": "…", "name": "get_sprint_status_by_number", "input": {"sprint_number": 31}}
```

- Every event has an `id` (1, 2, 3 … per turn), a type in `event`, and JSON in
  `data`. The log is append-only
  (`pmagent/web/sessions.py::Turn.emit`), so a stream can be **replayed**:
  reconnecting with `Last-Event-ID` continues exactly where it stopped. The same
  log serves `GET …/turns/{tid}`, the polling alternative, and `GET …/turns`,
  which the page uses to rebuild itself after a reload.
- **The stream closes when the turn rests**: `awaiting_approval`, `completed` or
  `failed`. After an approval decision the client reconnects with
  `Last-Event-ID`. This gives reconnection one clean meaning and makes the stream
  easy to test.
- **Never block the event loop.** The endpoint is an async generator, while the
  log is filled by a worker thread. The wait runs off-loop in
  `pmagent/web/app.py::create_app` → `turn_events`:
  `anyio.to_thread.run_sync(turn.wait_for_events, …, abandon_on_cancel=True,
  limiter=SSE_LIMITER)`. A dedicated limiter stops idle tabs from exhausting the
  thread pool.
- The page reads the stream with `fetch()` and a small parser, not
  `EventSource`, so it can send `Authorization` when a token is configured
  (`pmagent/web/static/app.js`).
- `Cache-Control: no-cache`, `X-Accel-Buffering: no` and a `retry:` hint make
  proxies behave; `ping=15` sends keep-alive comments.

## How the LangGraph original did it

The CLI printed new messages by diffing the graph state after each step
(`graph.stream(stream_mode="values")`). There was no event model, no reasoning
display and no browser. This lesson adds all three on top of Strands' callback
handler and hooks.

## Try it offline

- `docs/learning/examples/11_trace_events.py` runs a `TraceRecorder` on a scripted
  agent (a reasoning block, a tool call and a reply) and prints the event stream
  the UI would receive. No server, key or network is needed.
- `uv run scripts/demo_web.py` serves the real UI offline. Ask "Is the FX epic at
  risk?" to see reasoning, a tool call and the reply in the chain.

## Exercise

1. Run `uv run docs/learning/examples/11_trace_events.py`. The first column says
   which mechanism produced each event. For each event type, find the method in
   `pmagent/web/trace.py::TraceRecorder` that emits it, and explain why a
   `text.delta` could never come from a hook.
2. Add a `model.started` event: register a callback for `BeforeModelCallEvent` in
   `TraceRecorder.register_hooks`, emit it, and render it in `renderChain`
   (`app.js`). Then run `uv run pytest tests/test_web_api.py`. Which test must you
   update, and why is that a good thing?
3. **live** (OpenAI only, since `reasoning_available` requires it): run
   `LLM_PROVIDER=openai LLM_REASONING_EFFORT=high LLM_REASONING_SUMMARY=detailed uv run scripts/smoke.py --web`
   and ask something that needs judgment ("is sprint 31 at risk — blockers or
   unstarted work?"). Compare the chain with `LLM_REASONING_EFFORT=none`. Offline,
   `scripts/demo_web.py` shows what reasoning looks like in the chain.
4. With `uv run scripts/demo_web.py` running in another terminal (on a different
   port, e.g. `--port 8124`, change `8000` in the commands):

   ```bash
   B=http://127.0.0.1:8000/api/v1
   CID=$(curl -s -X POST $B/conversations | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')
   EVENTS=$(curl -s -X POST $B/conversations/$CID/turns -H 'Content-Type: application/json' \
            -d '{"message": "Is the FX epic at risk?"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["events_url"])')
   curl -N http://127.0.0.1:8000$EVENTS                          # the whole chain
   curl -N -H "Last-Event-ID: 3" http://127.0.0.1:8000$EVENTS    # only events after id 3
   ```

   Note that each stream closes by itself once the turn rests. What does the
   second command leave out?
