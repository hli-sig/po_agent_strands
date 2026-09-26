# 12. Logging: a run log you can trust

> **Part 3: Extend.** Lessons 12–17 are **guided builds**. The design is in
> `docs/PLAN_HARNESS_MEMORY.md`, but no solution is in the repo. You build it in
> your rebuild (`$MY`, from the Rebuild guide) or in a copy of this repository.
> Each lesson gives the Strands mechanism, a runnable example, the files to create
> (marked `➕`), the tests that define "done", and the traps.
>
> Recommended order: **12 → 13 → 16 → 15 → 17 → 14**. That is the plan's
> H1 → M1 → A → H2 → J1 → M2. Each lesson says what it needs from earlier ones.

## The concept

Today PO Agent records nothing. When a user asks "why did it create that
ticket?", or you want to know how often people reject a draft, there is no
answer. A **run log** is an append-only record of what happened in each turn: the
route, the tools requested, what they returned, the token usage, and above all
**what the human saw when they pressed y or n**.

A run log is not tracing. OpenTelemetry spans (Strands has them in
`strands.telemetry`) are for latency and debugging, and they need a collector. The
run log answers one question spans can't: *what approval text did the human
approve?* It is plain JSON lines you can `grep`.

## The Strands mechanism: hooks observe everything

Lesson 5's hooks are the right place: the loop fires events you can subscribe to,
without touching agent code. Three events cover a run log:

| Event | What you log | Why this event |
|-------|--------------|----------------|
| `BeforeToolsEvent` | each requested `toolUse` (name, args) | it fires **before** the approval gate pauses, so a write the user later rejects is still logged |
| `AfterToolCallEvent` | the tool's status and a clipped result | the result exists only after the call |
| `MessageAddedEvent` | assistant text | every message appended to the history |

Run the toy version:

```bash
uv run docs/learning/examples/12_runlog_hook.py
```

It prints three kinds of JSON lines: `tool_call`, `tool_result` and `assistant`.
The first `assistant` line has empty text, because the model's first message was
only a tool call. The rule the example shows is the most important one:
**`record()` never raises.** A full disk or an odd value must never break a turn.

## What you build

Build it in your rebuild. The design is `docs/PLAN_HARNESS_MEMORY.md` §2
("Phase H1").

1. `➕ pmagent/runlog.py`:
   - `record(event, **fields)` appends one JSON line to
     `<repo>/data/runlog/<YYYY-MM-DD>.jsonl`. Anchor the path to the repository
     root (`Path(__file__).resolve().parent.parent`), never the current
     directory.
   - Create the file with mode `0600` (`os.open(path, os.O_WRONLY | os.O_CREAT |
     os.O_APPEND, 0o600)`), because it holds user text.
   - Use `json.dumps(..., default=str)`, and hold a `threading.Lock` around the
     append, because web turns run on worker threads.
   - Wrap the whole body in `try/except Exception: pass`.
   - `PMAGENT_RUNLOG=0` turns it off. Read the variable **at each call**, not at
     import, so tests can change it.
   - Get the directory from one helper, `➕ data_dir()`: `PMAGENT_DATA_DIR` if
     set, otherwise `<repo>/data`. Resolve it **at each call** too, so a test's
     `monkeypatch.setenv` takes effect. Lessons 13 and 14 reuse it.
   - Every record carries `ts`, `event`, `schema=1`, `conversation_id`,
     `turn_id` and `frontend` (`cli`|`web`|`eval`).
2. `➕ RunLogHook(HookProvider)` in the same module. Grow the example's hook: add
   `AfterToolCallEvent` → `tool_result`, clipped to 500 characters. **For finance
   and spreadsheet tools, log the length and a SHA-256 hash, not the text**,
   because budgets are sensitive. Wrap every callback body in `try/except`.
   The hook needs the ids, so give it a reference to its assistant (or to a
   small shared `ids` object) when `PMAssistant` builds it:
   `RunLogHook(self)`. Each callback then reads `assistant.conversation_id` and
   `assistant.turn_id` at the moment it fires.
3. **Ids in `PMAssistant`** (`pmagent/assistant.py`):
   - `conversation_id`: mint one with `uuid4().hex` in `__init__`, or accept it as
     a parameter, since the web session already has an id. Mint a new one in
     `reset()`.
   - `turn_id`: mint it per `send()`, and **keep it through `resume()`**, because
     the approved write runs during the resume.
   - **Web:** `pmagent/web/sessions.py` builds the assistant with
     `assistant_factory(self.recorder)`. Right after that line, set
     `self.assistant.conversation_id = self.id` and
     `self.assistant.frontend = "web"`. Don't change the factory signature: the
     tests and the demo pass factories that take only `recorder`.
4. **What `PMAssistant` records.** Don't put these in the hook; the assistant is
   the one place that knows them.
   - `turn`, when `send()` starts.
   - `route`, with the reason.
   - (`tool_call` and `tool_result` come from the `RunLogHook`, because only the
     loop sees the requested `toolUse` blocks and the results.)
   - `usage`, at the end of each `send`/`resume`.
   - `approval`, with a decision of `approved`, `rejected` or `abandoned`. Log
     `abandoned` on **every** path that abandons: `discard_pending()`, and
     `send()`'s own auto-discard.
   - Add the `RunLogHook` to every lane: in `__init__`, append one to the
     hooks `PMAssistant` hands `make_lane_agent`, so both frontends and the demo
     get it without changes.
   - Accept a `frontend="cli"` parameter and put it on every record. `web`
     passes `"web"`; the evals in lesson 15 pass `"eval"`.
5. **Frontends record one thing: `approval.shown`.** This is the rendered
   `describe_write` text and the scope warning, keyed by `toolUseId`. Only the
   frontend knows exactly what the human saw.
   - The CLI records it in `main.py`'s `resolve_approvals`. Read the ids with
     `getattr(assistant, "conversation_id", None)`: `tests/test_cli.py` drives
     it with a `StubAssistant` that has no ids, and 3 of its tests would fail.
   - The web records it where it builds the approval, in
     `pmagent/web/sessions.py`.
6. `➕ tests/conftest.py`: turn the log off **for the whole session**, at import
   time, not per test:

   ```python
   import os, tempfile
   os.environ["PMAGENT_RUNLOG"] = "0"
   os.environ["PMAGENT_DATA_DIR"] = tempfile.mkdtemp(prefix="pmagent-test-data-")
   ```

   A per-test `monkeypatch.setenv` is **not enough**. Web tests start worker
   threads that can outlive their test, and once monkeypatch undoes the setting,
   a late `abandoned` record lands in your real `data/runlog/`. (The course
   audit reproduced this.) Individual run-log tests turn it back on with
   `monkeypatch.setenv("PMAGENT_RUNLOG", "1")`, and point
   `PMAGENT_DATA_DIR` at `tmp_path`.

## Definition of done: the tests to write

Create `➕ tests/test_runlog.py`, offline, with `tests/fakes.py::ScriptedModel`:

- [ ] **A logger failure never breaks a turn:** monkeypatch the file open to
  raise, then run `PMAssistant.send`. The turn completes.
- [ ] **A hook-body failure is swallowed:** make the hook's clip function raise,
  and the turn still completes.
- [ ] **A gated write produces `tool_call` before the approval:** script an
  `add_jira_comment` call, `send()`, and read the log. `tool_call` is there and
  no `tool_result` is yet. After `resume(True)`, there is still exactly **one**
  `tool_call` for that `toolUseId` (see Traps).
- [ ] **`approval` is logged on all paths:** approve, reject, `discard_pending`,
  and a new `send()` while pending (auto-discard → `abandoned`).
- [ ] **CLI `approval.shown`:** its `rendered` equals
  `describe_write(...)` for the same call (drive `resolve_approvals` with a
  monkeypatched `input`, as `tests/test_cli.py` does).
- [ ] **Finance results are hashed:** a `read_fy_budget_run` result appears as
  `{"len": …, "sha256": …}`, and its text does not appear anywhere in the file.
- [ ] **Golden keys:** each event type has a fixed key set. Assert it, so a
  refactor can't silently drop `turn_id`.
- [ ] `uv run pytest -q` stays green, and `data/runlog/` is still empty after it
  (conftest works).

## Traps

- **Logging from the hook alone** misses the requirements lane: it runs two
  `structured()` calls with no lane agent and no hooks (lesson 7). That is why
  `turn`/`route`/`usage` come from `PMAssistant`.
- **One global callback slot.** `on_route_decided` and `on_prd_step` already
  belong to the web `TraceRecorder`. Don't reuse them for logging; record
  directly in `PMAssistant`.
- **Logging the approval decision in the frontend** misses abandonment:
  `discard_pending()` can be called by `send()` itself. Record the *decision* in
  the assistant and only the *shown text* in the frontend.
- **`BeforeToolsEvent` fires again on `resume()`**, with the same assistant
  message, so the example's hook would log the gated call twice. Deduplicate on
  `toolUseId`: keep a set of ids already logged.
- **A rejected call gets no `tool_result`.** The gate cancels it, so
  `AfterToolCallEvent` never fires. Its outcome is the `approval` record with
  `rejected`. Don't write a test that expects a result for every call.
- **Commit nothing under `data/`.** Don't add all of `data/` to `.gitignore`: the
  copied `.gitignore` has `!data/fy_budget/input/`-style exceptions that a plain
  `data/` line would override. Add `data/runlog/` now, and later
  `data/conversations.sqlite3*` (lesson 13) and `data/memory/` (lesson 14).

## Exercises

1. **(code)** Add `PMAGENT_RUNLOG=0` handling, run the suite, and prove
   `data/runlog/` stays empty.
2. **(run)** Run `uv run scripts/demo_web.py` in your build, post the comment,
   press Reject, then `grep '"approval' data/runlog/*.jsonl` (no closing quote,
   so it matches `approval.shown` too). You should see
   `approval.shown` and then `rejected`. (The demo writes nothing to Jira; the
   run log is local.)
3. **(think)** Why is "what the human saw" more valuable than "what the model
   said" when you investigate a bad ticket? Hint: lesson 6's
   `unchecked_scope_warning`.

**How the original did it:** it didn't. The original's harness doc planned a run
log after a persistent checkpointer. This plan does the log first, because it is
cheaper and persistence (lesson 13) reuses its ids.
