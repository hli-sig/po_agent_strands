# Plan (part 3): harness, memory and Jev (v3.1: review PASS)

**Status: plan only. Nothing here is built.**

- **v3**, 2026-09-24: v2 revised after review round 1 (10 major, 8 minor) and
  extended with **Jev** at the owner's request (§J); v3 revised after review
  round 2 (5 major, 8 minor). **Round 3: PASS**; its 8 minor notes are folded in
  as v3.1. See §12.
- It is written against the repo recorded in `docs/EVIDENCE.md` (664 tests).

## 0. What the words mean here (disambiguate first)

"Harness" follows the original project's map
(`../PO_Agent/docs/harness-handover.md` §1–§2):

| | Harness | Payload | State today |
|-|---------|---------|-------------|
| **A** | Claude Code's config for this repo (`.claude/`) | Claude, working on the repo | **none** |
| **B** | PO Agent itself (loop, lanes, gate) | the LLM | built; **nothing is recorded**, and a restart loses everything |
| **C** | `tests/` | the deterministic code | strong (664 tests) |
| **D** | `evals/` | the agent's *judgment* | **does not exist** |

"Memory" means two things:
- **M1**, conversation persistence: survives a restart.
- **M2**, long-term memory: carried across conversations.

**Jev** (TypeSafe's calibrated decision model) is threaded through the phases
where it is the right tool (§J).

## 1. Research findings (verified against installed code and the docs, 2026-09-24)

**Strands 1.57: memory.**
- **Plugin.** `strands.memory.MemoryManager` is a `Plugin`, passed as
  `Agent(memory_manager=)`. Its tools (`search_memory`, on by default;
  `add_memory(entries: list[str])`, off by default) are registered in
  `agent.tool_registry` (`plugins/registry.py::_register_tools`). So the
  fail-closed gate **does** gate `add_memory`, and `search_memory` needs a
  read-allowlist entry.
- **Injection.** It rebuilds the per-call context only
  (`injection/_message_injection.py`) and **never touches persisted history**.
  It folds retrieved memory into the latest *user* message for that call; this
  matters for §4.4.
- **Extraction does not fit this app as shipped.** Four reasons:
  - The sync `Agent.__call__` awaits `memory_manager.flush()` after **every**
    invocation (`agent/agent.py::_invoke_async_and_flush`). `flush` extracts all
    unsaved messages regardless of the trigger, so every `send()` and `resume()`
    would block on one extractor call.
  - The extractor defaults to `agent.model`, and its usage bypasses our usage
    sink (`model_extractor.py`).
  - `MemoryManager.init_agent` rebinds a single coordinator per agent
    (`memory_manager.py`), so it cannot serve six lane agents sharing one list.
  - `FileMemoryStore.add` serialises with an `asyncio.Lock`, which is unsafe
    across our per-conversation threads and event loops.
  → **This plan uses the MemoryManager for injection and search only, and runs
  extraction once per completed turn from `PMAssistant`** (§4.2).
- **`FileMemoryStore` stores topic files, not facts.**
  - `add` ignores `metadata`, merges entries into files keyed by the slug of
    their heading, and `search` returns `{path, score}`.
  - There is no per-fact id, provenance or date, so per-fact delete and
    provenance are impossible.
  - It namespaces its keys under `memory/<name>/`.
  - `LocalFileStorage(base_dir)` needs no sandbox argument.

  → **A small custom `MemoryStore`** is needed (§4.2).
- `DEFAULT_MEMORY_MESSAGE_FILTER` strips `toolUse`/`toolResult` blocks but
  **keeps assistant text**. An assistant summary of a malicious Jira comment would
  therefore reach a default extractor (§4.4).

**Strands 1.57: sessions.**
- **Per agent, and fragmentary here.** `FileSessionManager` persists an agent's
  messages, state and interrupt state per agent
  (`repository_session_manager.py::append_message`). With our shared list, each
  lane would store only **its own fragments**, and requirements-lane messages
  would never be stored.
- **Snapshots.** Strands also has `Agent.take_snapshot()`/`load_snapshot()`
  (messages and interrupt state), and `SnapshotSessionManager` ("recommended path
  for new agents").

  → Keep our own store (§3). It stores a **Strands `Snapshot`-shaped payload**
  (a `schema_version`, and `app_data` holding route and ids), encoded with
  `strands.types.session.encode_bytes_values`. Lesson 13 teaches
  `take_snapshot`/`load_snapshot` on a single agent, and explains
  why the shared list can't use a session manager directly.

**Telemetry.** `strands.telemetry` provides OpenTelemetry spans. It is useful for
latency and debugging, but it is not the approval record (§2).

**Evals: `strands-agents-evals==1.4.0`** (official; inspected in an isolated env).
- It resolves with our 1.57, and brings in `boto3` and `strands-agents-tools`, so
  it is a dev dependency only.
- The API is `Case`, then `Experiment.run_evaluations(task)`, then
  `EvaluationReport`.
- **The task must return a `TaskOutput`-shaped dict**, `{"output", "trajectory",
  "environment_state", …}`. Other keys are silently dropped (`_run_task_async`).
- Custom `Evaluator.evaluate(EvaluationData) -> list[EvaluationOutput]` is
  enough.
- Judges take `model=`.
- ⚠ **`strands-evals`** (0.0.1, "automated data synchronization and backend
  management") is not the official package and looks like a typosquat. Pin the
  exact name.

**Jev: `typesafe-sdk==0.7.1`, `jev-1.13.0`.** See §J.0–§J.1, including two
live probes on synthetic text.

**Prior design to reuse** (`../PO_Agent/docs/`):
- the loop doc's run log, with the *rendered* approval text;
- two eval tiers, never mixed;
- the harness doc's `.claude/` items (§7) and its "what can a harness do that tests
  can't" rule (§6);
- the §9.7 outstanding eval, "does the agent pass `scope`";
- the §8.5 abstention channel.

## 2. Phase H1: run log (harness B), ~½ day

### 2.1 Design

- `pmagent/runlog.py::record(event, **fields)` appends JSON lines to
  `<repo>/data/runlog/<YYYY-MM-DD>.jsonl`, with the file mode 0600 and the path
  anchored to the repo root, not the CWD.
- It **never raises**: `record` and every hook callback body are wrapped. It
  uses `json.dumps(default=str)`, skips bytes, and takes a `threading.Lock`
  around appends.
- `PMAGENT_RUNLOG=0` disables it. So does pytest, via `conftest` (§8).
- **Where each event comes from.** This fixes the review's single-slot finding,
  because `TraceRecorder` already owns the `on_route_decided` and `on_prd_step`
  callbacks.
  - **`PMAssistant` records** `turn` (with `conversation_id` and `turn_id`),
    `route` (with reason and confidence), `usage` and `approval`
    decisions (`approved`/`rejected`/`abandoned`, on **every** path, including
    `send()`'s auto-discard and `discard_pending`).
  - `PMAssistant` gains `conversation_id` (minted, or passed in by the web
    session) and a per-send `turn_id`.
  - A `RunLogHook` (added by `PMAssistant` to every lane) records `tool_call`
    from the *requested* `toolUse` blocks on `BeforeToolsEvent`, including gated
    calls that never execute, deduplicated on `toolUseId` (the event fires again
    on resume). It records `tool_result` on `AfterToolCallEvent`, clipped to 500
    chars. A rejected call has no `tool_result`; its outcome is the `approval`
    record.
  - **For sensitive tools** (finance, spreadsheet), the log keeps the length and a
    SHA-256 hash of the result, not its text.
  - **Frontends record only `approval.shown`**: the rendered `describe_write`
    text and the scope warning, keyed by `toolUseId`. That is the one fact only
    they have. The CLI records it in `resolve_approvals`, the web in
    `sessions._finish`.
- Each record carries `ts`, `event`, `schema=1`, `conversation_id`, `turn_id` and
  `frontend` (`cli`|`web`|`eval`).
- Retention: `scripts/prune_data.py --runlog-days 30` (see the §7 inventory).

### 2.2 Why not OpenTelemetry

The run log answers "what did the human see when they pressed y/n", which spans
don't carry. It also needs to be greppable with no collector. OTel stays an
optional extra (`PMAGENT_OTEL=console|otlp`, off by default), mentioned in
lesson 12.

### 2.3 Tests

- A logger failure never breaks a turn.
- Hook-body failures are swallowed.
- CLI and web each produce `approval.shown` with `rendered == describe_write`.
- A gated call produces `tool_call` before approval.
- Finance results are hashed, not stored.
- The golden key set per event is fixed.

### 2.4 Ordering note

The original harness doc §11 put the persistent checkpointer *before* the run
log. This plan reverses that. H1 is cheaper, and M1 reuses H1's ids. The cost is
that until M1 lands, a crash still loses the conversation the log describes. The
log itself survives, and that is enough for mining rejections.

## 3. Phase M1: conversation persistence (runtime), ~1 day

### 3.1 What survives a restart

- **Kept:** messages, route, and (web) turns with their event logs and approvals.
- **A *pending* approval does not survive.** It becomes `abandoned (restart)`,
  with the message "Not executed — the approval was abandoned before the user
  answered."
- **An approval that was *approved* and was executing when the process died
  becomes `interrupted`.** Order matters here:
  1. The store records `approved/executing` **before** the write runs. This is
     done by an `on_before_resume` persistence callback inside
     `PMAssistant.resume()`, so the CLI and web get the same guarantee.
  2. On restart, that call is closed with **"Outcome unknown — the process stopped
     during this write. Verify in Jira before retrying."**
  3. The web approval shows `interrupted`, and the run log records the same.

  This prevents the duplicate create that the old "nothing was written" message
  would invite (review finding 6).

### 3.2 Store

- `pmagent/persistence.py::ConversationStore` uses stdlib `sqlite3`, with WAL and
  one connection per thread.
- The file is `<repo>/data/conversations.sqlite3`, mode 0600. `data/**` is
  gitignored, which covers the `-wal`/`-shm` files.
- Tables: `conversations(id, created_at, updated_at, snapshot_json)`, where the
  snapshot is Strands-snapshot-shaped (`schema_version`, `messages` via
  `encode_bytes_values`, `app_data={route, …}`); `turns(…, events_json)`; and
  `approvals(…, status)`.
- `PMAssistant.snapshot()` / `restore()` return and accept plain data.
- The web `SessionStore` writes through on every state change. The CLI gains
  `/sessions`, `/resume <id>`, `/delete <id>` and `/new`.
- The web API gains `DELETE /api/v1/conversations/{id}` (which already exists)
  and now **deletes the stored row too**.

### 3.3 Tests

- A snapshot round-trips `toolUse`, `toolResult` and image blocks.
- A pending approval becomes `abandoned` after a restart, and the lane accepts
  input again.
- **Approved and executing, then a simulated crash, then a restart:** the call
  is closed as `interrupted` with the "outcome unknown" text, and the approval
  status is `interrupted`.
- The web API reloads conversations across two `create_app` instances sharing one
  db.
- Two threads writing concurrently cause no corruption.
- Delete removes the row.

## 4. Phase M2: long-term memory, ~1½ days

### 4.1 What it is for

It holds preferences, team facts and decisions *the user stated*. It is **never**
Jira state: Jira is the source of truth, and memory must never answer "what is
the status of X".

### 4.2 Design, revised after review

- **Store, in two layers:**
  - `pmagent/tools/memory/facts.py::FactFiles`: **framework-free** (no Strands
    import, keeping CLAUDE.md's tools-layer rule), with **synchronous**
    `add`/`list`/`delete`/`confirm`. It is used by the `remember_fact` tool, the
    extractor, the CLI and the API.
  - `pmagent/memory_store.py::FactStore`: the Strands `MemoryStore` **adapter**
    over it, outside `pmagent/tools/`. Its async `search`/`add` delegate to
    `FactFiles` via `asyncio.to_thread`.

  `FactFiles` keeps **one Markdown file per fact** under `data_dir()/memory/` (default `<repo>/data/memory/`; `PMAGENT_DATA_DIR` moves it),
  with front-matter:
  `id`, `text`, `status` (`suggested`|`confirmed`), `conversation_id`, `turn_id`,
  `created_at` and `source` (`user`|`explicit`).
  - Search is keyword-based over confirmed facts.
  - It adds `list()` and `delete(id)`.
  - Writes use a `threading.Lock` and an atomic write: a temporary file, then
    `os.replace`.
  - Duplicates are dropped on normalised text.
- **One `MemoryManager` instance, shared by the six lane agents.** With
  extraction off, `init_agent` only adds per-agent injection middleware, and
  `flush()` is a no-op (verified, round 2). It provides:
  - **injection** (`max_entries=5`);
  - **`search_memory`**, in `gate.READ_TOOL_NAMES` via `APPROVED_MEMORY_READS`.

  The store is `extraction=False`, and `add_tool_config=False`: the vended
  `add_memory` is not used (see "Explicit memory" below).
- **"Confirmed only" is enforced inside `FactStore.search`.** Injection and
  `search_memory` both go through `store.search`, and `MemoryInjectionConfig` has
  no filter of its own.
- `FactStore` implements the protocol: `name`, `description`,
  `max_search_results`, `writable`, `extraction`, `async search(query,
  options=None) -> list[MemoryEntry]`, and `async add(content, metadata=None)`.
- **Extraction is done by `PMAssistant`, exactly once per turn**, when the turn
  *completes*, whether that happens in `send()` or after `resume()`. It never
  runs on an interrupt. It runs **off the reply path**, on a background thread
  after the reply is returned, so a web turn isn't held.
  - It uses the model named in `LLM_MEMORY_MODEL`, set in `env.py` and
    `.env.sample`; the default is the provider's cheap tier.
  - Because it finishes after the turn has returned, its tokens are reported as
    their **own `memory.usage` event** (run log and trace), keyed by that turn's
    `turn_id`. They are never added to `_turn_usage`, which has already been
    returned or reset.
  1. It runs `llm.structured(MemoryCandidates, prompt, model=cheap_model,
     usage_sink=<a sink that records memory.usage for this turn_id>)`, so its
     usage **is** counted, separately from the turn's.
  2. The input is **`last_user_text(messages)` only**. Tool results and assistant
     text are excluded by *code*, not by prompt. The requirements lane is
     included, because this runs above the lanes.
  3. The **Jev gate** (§J placement 3) skips the extraction call entirely when the
     user said nothing durable.
  4. Candidates are written as **`suggested`** (unconfirmed suggestions are pruned
     after 30 days, see §7) and are **not injected until the
     user confirms them** (a web "Memory" panel with Save/Discard, or CLI
     `/memory`, then `/memory save <id>` or `/memory forget <id>`).

  So no automatic write changes the agent's behaviour without a human. This
  resolves the review's "extraction is an ungated write" inconsistency.
- **Explicit memory.** "Remember that I own FX" → **`remember_fact(facts:
  list[str])`**, a **project tool** in `pmagent/tools/memory_tools.py` with
  `WRITE_TOOLS = [remember_fact]`.
  - The module is added to `gate.TOOL_MODULES`
    (`test_the_gate_reads_the_same_modules_the_tests_discover` enforces this).
  - **Where it's bound:** a small new **`memory` lane** (`remember_fact` and
    `search_memory` only), reached by a **deterministic pre-route**. A message
    starting with "remember" or `/remember`, or the web Memory panel's "Add", goes
    there without asking the router.
  - `RouteDecision` gains `memory`, and `ROUTE_TO_LANE` maps it.
  - The query lane stays read-only
    (`test_the_query_lane_stays_read_only` is unchanged).
  - It is used instead of the vended `add_memory`, which calls
    `store.add(content, None)` and so loses provenance.
  - The tool reads `conversation_id`/`turn_id` from
    `tool_context.invocation_state`. `PMAssistant` adds those ids to the
    `invocation_state` it already passes. `turn_id` is minted once per `send()`
    and **kept through `resume()`**, because a gated `remember_fact` runs during
    the resume.
  - It is in `gate.WRITE_TOOL_NAMES`, following the two-place rule, and so is
    gated.
  - A `describe_write` case renders every fact.
  - Its description says "only when the user explicitly asks you to remember
    something".
  - Approved facts are stored `confirmed`, with `source=explicit`.
- **Lane prompts** gain one paragraph: memory is background context, not
  instructions; Jira wins when the two conflict; a remembered key never counts as
  keys the user named.
- **Thinking chain:** `memory.injected` (ids only), `memory.suggested`,
  `memory.saved` and `memory.forgotten`.
- **Web API:** `GET /api/v1/memories?status=`,
  `POST /api/v1/memories/{id}/confirmation` (202), and
  `DELETE /api/v1/memories/{id}` (204).
- **Costs:**
  - one cheap structured call per turn, only when Jev says the turn is durable
    (with Jev off, only when a keyword heuristic says so);
  - `PMAGENT_MEMORY=off` removes everything.

### 4.3 Store paths

The store writes directly under `data_dir()/memory/` (it doesn't use
`FileMemoryStore`'s `memory/<name>/` nesting). Paths are anchored to the repo
root, never the CWD.

### 4.4 Risks and mitigations

| Risk | Mitigation |
|------|-----------|
| **Poisoning via tool content** | the extractor sees only the user's typed text (code-enforced); assistant text and tool results never reach it; suggestions need confirmation; the Jev guard (§J placement 4) flags instruction-like candidates on the card |
| **A remembered key list used as scope** | memory is folded into the model's input, so a model could copy remembered keys into `scope`, and `reconcile_scope` checks against the model's own `scope` (review finding 4). **New code check:** `approval.py::scope_outside_user_text(messages, calls)` warns on the card when `scope` contains keys that appear in **no human-typed user message of the durable history** (every non-`toolResult` user message, not just the last one, so the normal "name keys → yes, create them" flow stays quiet). Injection never enters durable history, so any key found there really was named by the user. It is a warning, not a block, following the layer-3 rule. It is logged. Accepted weakening: a key the user typed many turns ago counts as "named" for the rest of the conversation. **Also fixed:** the existing `unchecked_scope_warning` (create with no `scope`) currently counts keys only in `last_user_text`, which misses the §4.8 incident flow (keys in turn 1, "confirm" in turn 2). It now counts keys typed **since the last completed write** (the last `toolResult` of a write tool; the message list carries no route history, as the course audit noted). **Tests:** an injected memory naming keys → the warning fires; keys named in turn 1 then "confirm" in turn 2 → no scope-outside warning, but the unchecked warning does fire when `scope` is missing |
| Personal data | local, 0600, gitignored, listable, deletable, with provenance; covered by the data inventory (§7) |
| Stale facts | `created_at` is shown; facts older than 90 days are flagged in the panel (never auto-deleted) |
| Echo re-extraction and duplicates | the extractor ignores assistant text; the store drops duplicates |

### 4.5 Tests

All offline, using a temp dir and a separate `ScriptedModel` for the extractor:
- the extractor input is only the last user text;
- it runs once per completed turn, and not on an interrupt;
- its usage appears as a `memory.usage` event for the right `turn_id`, and the
  turn's own usage is unaffected;
- suggestions are not injected until confirmed;
- confirmed facts are injected, and history is unchanged;
- `remember_fact` is gated, and its `describe_write` renders all facts;
- `remember …` / `/remember` pre-routes to the memory lane;
- `search_memory` is not gated;
- a walk of each lane's `tool_registry` finds every registered tool either
  classified or gated;
- the scope-outside-user-text warning fires for memory-only keys, and stays
  silent across a multi-turn confirm;
- both cards (CLI and web) get the same warnings from one
  `approval.py::approval_warnings(messages, calls)`, which combines
  `unchecked_scope_warning` and `scope_outside_user_text`;
- the store under two threads loses nothing;
- `PMAGENT_MEMORY=off` → no plugin, no extractor.

## 5. Phase H2: the eval harness (harness D), ~2 days

### 5.1 The eval sandbox (review finding 8)

`evals/sandbox.py::install()` is shared by tier 1 and tier 2. It:
1. **Blanks `JIRA_*`/`CONFLUENCE_*` in `os.environ` before the first `pmagent`
   import** (`env.py` runs `load_dotenv()` at import, and dotenv doesn't override
   variables already set), and also patches the `pmagent.env.*` attributes.
2. Sets `pmagent.tools.jira.client._client` and `confluence_tools._client` (the
   only module-global clients; verified in round 2) to **recording fakes**. Every
   write method appends to `fake.writes`.
3. Installs a deny-all HTTP guard. `pmagent/httpguard.py::install(allow=frozenset())`
   is factored out of `scripts/smoke.py`, which then uses it too, so importing it
   has no side effects. The guard covers `requests`. **It does not cover httpx2**,
   which Jev's SDK uses; the live Jev judge is the one deliberate exception, and
   it is stated in the report.
4. Forces `LUCID_MCP_ENABLED=false` (and builds assistants with `mcp=None`),
   `PMAGENT_MEMORY=off`, `PMAGENT_RUNLOG` with `frontend=eval`, and a fixture
   `FY_BUDGET_INPUT_DIR`.

In tier 1 the sandbox is a **pytest fixture that restores**
`requests.Session.request` and both `_client` globals afterwards, so it can't
leak into `test_jira_tools`/`test_confluence_tools`.

`NoWriteWithoutApproval` asserts `fake.writes == []`. That is a real check now,
not the tautology the review found.

### 5.2 Tier 1: pytest, no model

- Location: `tests/evals/`, which sits inside the normal `testpaths`.
- It replays scripted turns through the real loop with `ScriptedModel` and the
  sandbox.
- Seed cases:
  - TUTORIAL §4.8, scope abort;
  - §4.7, sprint number vs id;
  - §4.6, the `conifrm creation` continuation;
  - the new scope-outside-user-text warning.

### 5.3 Tier 2: live model, on demand

- Command: `uv run evals/run.py`. It is never part of `pytest -q`.
- **Task.** It builds `PMAssistant(model=llm.build_model())` inside the sandbox,
  and seeds `prefix_messages` and `previous_route` from the case input (needed
  for the continuation case).
- It calls `send()` once and **stops at the first approval (never `resume`)**.
- It returns a **`TaskOutput` dict**: `{"output": {"reply", "route",
  "route_reason", "pending", "usage"}, "trajectory": [requested tool names from
  toolUse blocks, including gated calls], "environment_state": [{"name":
  "writes", "state": fake.writes}]}`.
- **Evaluators:**
  - custom deterministic evaluators:
    - `RouteIs`;
    - `ScopeDeclared`: with ≥3 user-named keys, the pending create's `scope`
      equals them;
    - `NoWriteWithoutApproval`;
  - `ToolCalled`;
  - `StateEquals`;
  - a sparing `OutputEvaluator` with `model=llm.build_model()`;
  - the **Jev rubric judge** (§J placement 6).
- **Recorded biases:** the same model family grades its own output, and judge
  tokens are counted separately, because `EvaluationReport` lacks them.
- **Cases** live in `evals/cases/*.jsonl`: the three incidents, about 25 routing
  cases (including abstention cases, §J placement 2), and later cases from the run
  log (§5.4).
- `--repeat N` reports flakiness. Reports go to `evals/reports/` (gitignored),
  with a trend compared against the previous report.
- **Router comparison mode** (for J1): runs a **held-out set of ≥100 routing
  cases** (all 7 routes, including write routes, continuations and ambiguous
  cases) under LLM-only, Jev-only and cascade. It reports:
  - **per-route recall with 95% confidence intervals**;
  - latency and cost.

  τ is chosen on a separate tuning split, never on the held-out set or the 14
  probe messages.

### 5.4 From run log to case

`scripts/runlog_to_case.py`:
- redacts names and keys to placeholders;
- writes a draft case for a human to review;
- never auto-commits.

### 5.5 Tests of the harness

- The sandbox blocks HTTP.
- The fakes record writes.
- One offline `Experiment` run with a `ScriptedModel` task.
- The report and trend writer.

## 6. Phase A: Claude Code harness (`.claude/`), ~2–3 hours

Configured with the `update-config` skill. The exact hook schema is checked
then: matchers match tool names, so path and command filtering happens *inside*
the hook script from `tool_input`.

| Item | Kind |
|------|------|
| `PostToolUse` `Edit\|Write` → the script checks the path is under `pmagent/(tools\|agents\|web)/`, then runs `pytest tests/test_agent_lanes.py tests/test_tool_specs.py -q` | hook (timing) |
| `PreToolUse` `Bash` → the script checks for `git commit`, then runs the **measured** full suite (it includes uvicorn/Playwright; the time is recorded in the doc), blocking on red | hook (timing) |
| `PostToolUse` `Edit\|Write` of `docs/learning/*.md` → `scripts/build_course.py` | hook |
| `/audit-jira-config`: **user-invoked only** (`disable-model-invocation`); read-only field and createmeta audit through the smoke guard | skill (needs live Jira) |
| `plan-reviewer` subagent: the reviewer brief used throughout this project; **codifies the plan → review → implement → evidence workflow** | subagent |
| `course-tester` subagent: the learner-tester brief (offline only, revert experiments, report only) | subagent |
| `gate-auditor` subagent: is each write's approval text *sufficient*? Includes harness doc §8.3's **graduation rule**: a finding seen three times becomes a test | subagent |

**Permissions (review finding 9).**
- **Allow** only named safe commands: `uv run pytest*`,
  `uv run scripts/build_course.py*`, `uv run scripts/verbatim_report.py*`,
  `uv run scripts/demo_web.py*`, `uv run scripts/check_claude_config.py*` and
  `uv run docs/learning/examples/*`.
- **Ask** for `scripts/smoke.py`, `scripts/web_drive.py` and
  `scripts/ui_screenshots.py`: they read production Jira and spend money.
- **Deny** `uv run main.py*` and `uv run web.py*`, as defence in depth only.
- **An accident guard in code, not a security boundary.**
  `pmagent/liveguard.py::refuse_under_claude()` is called **only from the
  `if __name__ == "__main__":` blocks** of `main.py` and `web.py`, never from
  `main()`, so importing or calling `main()` in tests is unaffected.
  - It exits with a **distinct code (3)** and message when `CLAUDECODE=1`, unless
    `PMAGENT_ALLOW_LIVE=1`.
  - `smoke.py` needs nothing: it calls `main.main()`/`web.main()` in-process,
    which never reach the `__main__` guard.
  - The guard runs **before** `build_assistant`.
  - The refusal test runs the script with `runpy.run_path(..., run_name="__main__")`
    or a subprocess, since calling `main()` never reaches the guard.
  - `tests/conftest.py` removes `CLAUDECODE` for all tests. Claude Code runs
    pytest with it set, which would otherwise make tests pass vacuously. Only the
    refusal test sets it, and asserts on exit code 3 and the message.
  - Permissions also **deny** Bash commands containing `PMAGENT_ALLOW_LIVE` or
    `env -u CLAUDECODE`.
  - Honest limit: this is easy to bypass on purpose, e.g.
    `uv run python -c "import main; main.main()"`. The aim is to stop an
    *accidental* live run from a Claude session, not a deliberate one.

`scripts/check_claude_config.py` validates the JSON and checks that referenced
scripts exist, with a test.

## 7. Data inventory, retention and deletion (review finding 10)

| Store | Contains | Location (repo-anchored, 0600, gitignored `data/**`) | Retention | Delete |
|-------|----------|-------------------------------------------------------|-----------|--------|
| Run log | turns, routes, tool calls (clipped; finance/spreadsheet hashed), approvals as shown | `data/runlog/*.jsonl` | 30 days (`prune_data.py`) | prune |
| Conversations | full messages incl. tool results and images | `data/conversations.sqlite3` (+wal/shm) | 90 days after last update (`prune_data.py`) | `/delete`, `DELETE /api/v1/conversations/{id}` |
| Memory | user-stated facts with provenance | `data/memory/*.md` | confirmed: until deleted, flagged after 90 days; **suggested (unconfirmed): 30 days** | `/memory forget`, `DELETE /api/v1/memories/{id}` |
| Eval reports | case outputs (sandboxed fake data) | `evals/reports/` | keep last 20 | prune |
| Jev calls | locally: only the run log's `jev.decided` (answers and confidences, not the state text). **At TypeSafe:** request content under their standard retention (zero data retention is enterprise-only; see their legal page), and the data classes sent are listed per placement in §J.0 | TypeSafe | per TypeSafe's terms | not deletable by us: this is why each placement needs approval |

## 8. Test isolation (review finding 15)

- `tests/conftest.py` sets `PMAGENT_RUNLOG=0`, `PMAGENT_MEMORY=off` and
  `JEV_ENABLED=false`, **removes `CLAUDECODE`**, and points `PMAGENT_DATA_DIR` at
  a temp dir, **at import time for the whole session**. A per-test monkeypatch
  isn't enough, because web worker threads can outlive their test (found by the
  course audit). Every existing test then stays hermetic.
- The extractor gets its own `ScriptedModel` in memory tests, so it never
  consumes lane script entries.

## J. Jev: where it fits

### J.0 What it is, and isn't

- **What it does:** `jev-1.13.0` via `typesafe-sdk==0.7.1`. Send a *state* and
  typed questions, and get calibrated answers:
  - `Noul`, a yes/no probability (criteria keys `true`/`false`);
  - `Choice`, a label with a probability per option and `confidence`;
  - `Score`, a rubric level with `confidence`.
- **Price and limits:** $0.042 per million *input* tokens (output free); 1,200
  requests per minute; 64k context (32k for state plus the longest question);
  text only.
- **Pin the version:** the `jev-latest` alias moves.
- **Documented weaknesses:**
  - literal reading;
  - no maths, counting or date comparison;
  - large irrelevant state hurts;
  - **adversarial content can steer it**;
  - **no generation**.

**Rules for this repo**

- **Not a Strands `Model`.** Jev plugs in as a *decision function* at four seams:
  the router, a hook/approval path, a custom evaluator, and the memory pipeline.
- **Always a deterministic bypass.** `JEV_ENABLED=false`, or TypeSafe being down,
  means today's behaviour.
- **Never authorises a write.**
- **Data governance, per placement.** Every placement sends text to a second
  processor (TypeSafe; zero data retention is enterprise-only). There is no
  generated "summary": Jev can't generate one, and an LLM summary would cost
  the call the cascade saves. So each placement's state is **defined in code**,
  and each needs its own approval:

  | Placement | State sent to TypeSafe (exact) | Data class | Owner approval | Default |
  |-----------|--------------------------------|------------|----------------|---------|
  | 1–2 router and abstention | `{user_text, previous_route, previous_user_text}`: only what the human typed, **no assistant or tool text** | user-typed text (may contain ticket keys and names the user typed) | required | off until approved and eval-passed |
  | 3–4 memory gate and flag | `last_user_text`, and the candidate fact (derived from it) | user-typed text | required | off until approved |
  | 5 memory relevance filter | the user's text plus the stored facts retrieved | user-stated facts | required | off |
  | 6 eval judge | eval case output from the **sandbox (fake data)** | synthetic | not needed (no production data) | on in tier 2 |
  | 7 approval advisories | the rendered pending write (**Jira-derived** drafts or comments) | Jira content | **separate approval** | off |
  | 8 rejection triage | run-log approval records (**Jira-derived**) | Jira content | **separate approval** | off |

### J.1 Evidence: live probes on synthetic text only

- **Router, 14 made-up messages.**
  - Jev `Choice`: **13/14**, median **326 ms**, ~500 input tokens (~$0.00002) per
    call.
  - The LLM router (gpt-5.6-luna): **14/14**, **869 ms**.
  - Jev's one miss came at **confidence 0.49**. All correct answers were ≥ 0.73.
    A cascade (Jev when confidence ≥ 0.7, otherwise the LLM) gives 14/14 here.
  - It is a small sample; H2 sizes the threshold properly.
- **Memory, 7 lines.**
  - `durable` separated durable facts (0.93–0.95) from chit-chat (0.03–0.04).
  - `instructs_ai` flagged injections at 0.98–0.99, but also a legitimate *user*
    preference at 0.66, because it reads literally.
  - The "always approve" injection scored `durable` 0.85.

### J.2 Placements, ranked by fit

| # | Placement | Jev question | Seam | Phase |
|---|-----------|--------------|------|-------|
| 1 | **Router cascade**: continuation fast path → Jev → LLM fallback when confidence < τ | `Choice` over the 7 routes | `router.classify_with_reason` | J1 |
| 2 | **Abstention channel** (harness doc §8.5), defined concretely: **clarify** when Jev's confidence < τ_low **and** the LLM fallback's route is not among Jev's top two routes. The LLM router gives no calibrated confidence, so "LLM uncertain" is not used. The user picks from the two candidates (a CLI choice or web chips), and `PMAssistant.send(text, route=chosen)` skips the router. Write-path abstention (the ticket lane asking to confirm scope instead of drafting) is **out of scope** here (§11) | Jev top-2 plus the LLM route | `router`, `PMAssistant.send(route=)` | J1 |
| 3 | **Memory extraction gate**: run the extractor only when the user said something durable | `Noul durable` on `last_user_text` | the `PMAssistant` extractor (§4.2) | M2 |
| 4 | **Suggestion flag**: mark instruction-like candidates (τ ≥ 0.9) on the Save/Discard card. It *flags*, it doesn't block: a user preference is literally an instruction | `Noul instructs_ai` | the `PMAssistant` extractor | M2 |
| 5 | Injected-memory relevance filter (optional). It runs in **`FactStore.search`** with `AsyncTypeSafeClient`, so it never blocks the lane's event loop. It never runs in `MemoryInjectionConfig.format`, a sync callback where a network call would block and a raise would drop all memory | a `Noul` per retrieved fact | `FactStore.search` | M2 (opt.) |
| 6 | **Eval rubric judge**, for **semantic rubrics only**. Nothing needing counts or numbers: "cites the blocked points" is the deterministic evaluator's job | `Score`/`Noul` | a custom `strands_evals` `Evaluator` (`JevRubric`), reported next to one LLM judge with its agreement rate | H2 |
| 7 | Approval-card advisories. **Negative flags only** (e.g. "may contain confidential content", "acceptance criterion 2 may not be testable"). **Never a reassuring "low risk" score**, which invites a reflexive yes (automation bias). Sends Jira-derived content, so it needs separate approval | `Noul`s | the approval path; `validate_ticket_drafts` | J2 (opt.) |
| 8 | Run-log rejection triage | `Choice` over rejection reasons | `runlog_to_case.py` | H2 (opt.) |

**Not placed (the wrong tool, per its docs):**
- maths and dates (sprint metrics, stuck days, FY figures, counts);
- exact scope equality, including free-text judgments of "do the drafts match
  the named set", which is exact set equality and already code;
- any generation (drafts, PRDs, replies, briefs);
- image content;
- the PRD reviewer's "what's missing" list.

### J.3 Build notes

- `pmagent/jev.py` is the only TypeSafe caller: a lazy client, and
  `decide() -> JevResult | None` that returns None on any error.
  - **The fallback must be fast.** The SDK defaults (10 s timeout, 2 retries with
    backoff up to 5 s, honouring `Retry-After`) would add ~30 s per turn while
    TypeSafe is down.
  - So the client is built with
    `TypeSafeClient(timeout=1.5, retry=RetryPolicy(max_retries=0), model=env.JEV_MODEL)`.
    `model` is passed explicitly, because the SDK otherwise falls back to
    `TYPESAFE_DEFAULT_MODEL`/`jev-latest`.
  - A **total deadline** of 2 s wraps each call (`future.result(timeout=2)` on a
    small executor), because the SDK's `timeout=1.5` applies per phase (connect,
    read, write, pool). The worst case is therefore bounded at about 2 s.
  - `decide()` catches `Exception`, not only `TypeSafeError`.
  - A **circuit breaker** skips Jev for 60 s after 3 consecutive failures. Its
    state is guarded by a `threading.Lock`, since web worker threads share it.
    `used_fallback`/`breaker_open` are recorded.
  - Tests use a fake transport (`transport=`):
    - assert the request's `extensions["timeout"]` is 1.5 s per phase;
    - "a hanging transport → LLM route within the 2 s deadline";
    - "the breaker opens and closes".
- New env vars: `JEV_ENABLED` (default false), `TYPESAFE_API_KEY` (restored to
  `env.py`), `JEV_MODEL=jev-1.13.0` and `JEV_ROUTE_THRESHOLD=0.7`.
- Dependency: **`typesafe-sdk==0.7.1`, pinned exactly** (the API moves fast). It
  brings `httpx2`, which round 2 verified is pydantic's fork, not a typosquat.
- Events: `jev.decided` (question, answer, confidence, model version, latency,
  `used_fallback`) in the thinking chain and the run log.
  `TurnResult.route_reason` gains `jev`, `jev->llm` and `clarify`.
- **Eval-gated default:** J1 ships with the H2 router comparison. `JEV_ENABLED`
  defaults on only if the cascade is at least as accurate as LLM-only.
- **Tests (offline, fake Jev client):**
  - confident → no LLM call;
  - low confidence → LLM;
  - error → LLM;
  - both uncertain → clarify;
  - disabled → identical to today;
  - the memory gate skips the extractor when not durable;
  - the flag appears on suggestions.

## 9. Course and documents

**Done ahead of the build, as guided lessons** (2026-09-24). The course now has
three parts: Understand (1–11), Rebuild (`docs/learning/rebuild.md`), and Extend.
Extend's lessons follow this plan, so the owner can build each phase by hand.

| Lesson | Phase | Example (offline) |
|--------|-------|-------------------|
| 12 Logging: the run log | H1 | `12_runlog_hook.py` |
| 13 Persistence | M1 | `13_snapshots.py` |
| 14 Long-term memory | M2 | `14_memory_injection.py` |
| 15 Evals | H2 | `15_eval_experiment.py` (with `--with strands-agents-evals==1.4.0`) |
| 16 The Claude Code harness | A | none |
| 17 Jev | J1 (+ the M2/H2 placements) | `17_jev_cascade.py` (fake client) |

When a phase is actually built in this repo, its lesson gains citations to the
real code, and the `➕` markers go. Still to do at build time: README,
CLAUDE.md, `.env.sample` (`PMAGENT_RUNLOG`, `PMAGENT_MEMORY`, `PMAGENT_OTEL`,
`PMAGENT_ALLOW_LIVE`, `JEV_*`, `TYPESAFE_API_KEY`), the LangGraph→Strands map,
and an EVIDENCE part per phase.

## 10. Order and sizing

| Phase | Effort | Useful alone | Depends on |
|-------|--------|--------------|-----------|
| **H1** run log | ½ day | yes | — |
| **M1** persistence | 1 day | yes | H1 ids |
| **A** `.claude/` | 2–3 h | yes | — |
| **H2** evals (with the Jev judge) | 2 days | yes | the sandbox; H1 for real cases |
| **J1** Jev router + abstention | 1 day | yes | H2 (comparison) |
| **M2** memory (with the Jev gate/flag) | 1½ days | yes | M1, H2 |
| J2 approval advisories | ½ day | optional | J1 |
| Course + docs | 1–1½ days | — | each phase |

**Order:** H1 → M1 → A → H2 → J1 → M2 → (J2). Each phase ships with its tests,
lesson section and evidence before the next starts.

**Decisions for the owner:**
1. Scope: all phases, or a subset.
2. Jev data governance, **per placement** (§J.0 table):
   - placements 1–4 send only user-typed text;
   - placements 7–8 send Jira-derived content and need a separate yes.
3. Confirm the defaults:
   - pending approvals are abandoned on restart, and executing ones marked
     *interrupted*;
   - extracted memories need a human Save;
   - `remember_fact` is gated.

## 11. Out of scope

- The scheduled weekly reviewer (loop doc Step 4), until the run log has data.
- A vector memory store, multi-user memory, auth, hosted tracing.
- Resuming paused writes across restarts.
- Write-path abstention (a lane declining to draft until scope is confirmed).
  This is a candidate after J1, once routing abstention is measured.

## 12. Review log

**Round 1: NEEDS CHANGES (10 major, 8 minor), all accepted.**

| # | Finding | Where fixed |
|---|---------|-------------|
| 1 | extraction runs every invocation, blocks, is uncounted | §1, §4.2 (a PMAssistant-level extractor with a cheap model and a usage sink) |
| 2 | six agents vs a per-agent extraction coordinator; store lock not thread-safe | §4.2 (no lane extraction; `FactStore` with a `threading.Lock`) |
| 3 | assistant text leaks into extraction; ungated extraction write | §4.2 (last user text only, in code; suggestions need Save), §4.4 |
| 4 | the scope mitigation was false | §4.4 (`scope_outside_user_text` warning) |
| 5 | `FileMemoryStore` can't do provenance or per-fact delete | §4.2 (custom `FactStore`) |
| 6 | crash after approval → false "nothing written" | §3.1 (`interrupted`, "outcome unknown") |
| 7 | the eval task return shape loses fields | §5.3 (`TaskOutput` dict) |
| 8 | evals could reach real clients | §5.1 (sandbox + recording fakes + deny-all HTTP) |
| 9 | the allowlist undermines the deny rule | §6 (named allows, ask, `CLAUDECODE` guard in code) |
| 10 | no data inventory or retention | §7 |
| 11 | run-log event sources | §2.1 |
| 12 | ordering note; abstention omitted | §2.4; §J placement 2 |
| 13 | M1 research (fragments; snapshots) | §1, §3.2 |
| 14 | plugin tools vs gate coverage; `add_memory` rendering and description | §4.2, §4.5 |
| 15 | test isolation; tier-1 location; continuation case needs prefix | §8, §5.2, §5.3 |
| 16 | hook matcher semantics; suite time; auditor graduation; skill invocation | §6 |
| 17 | judge bias, `build_model`, judge cost | §5.3 |
| 18 | store path nesting | §4.3 |

Also added in v2: **Jev** (§J), at the owner's request.

**Round 2: NEEDS CHANGES (5 major, 8 minor), all accepted.** The round-1 fixes
were confirmed against the SDK:
- `extraction=False` makes `flush` a no-op;
- the `FactStore` protocol;
- "confirmed only" is enforceable in `search`;
- the `TaskOutput` shape;
- the sandbox's client coverage.

| # | Finding | Where fixed |
|---|---------|-------------|
| 1 | scope warning fired on normal confirm flow | §4.4 (all human-typed messages) + multi-turn test |
| 2 | Jev governance contradicted by placements; "summary" undefined | §J.0 per-placement table; router state defined; §7 TypeSafe retention; §10 per-placement decision |
| 3 | SDK defaults make fallback slow | §J.3 (1.5 s timeout, no retries, circuit breaker, fake-transport tests) |
| 4 | "both uncertain" undefined | §J.2 #2 (concrete top-2 rule; `send(route=)`); write-path abstention → §11 |
| 5 | CLAUDECODE guard could make tests vacuous; overclaimed | §6 (guard only in `__main__`, exit 3, conftest removes it, reworded as an accident guard) |
| 6 | `add_memory` contradictory; loses provenance | §4.2 (`remember_fact` project tool via `invocation_state`) |
| 7 | Jev fit: judge numbers, risk-score bias, sync format callback | §J.2 #5–7, not-placed list |
| 8 | J1 acceptance underpowered | §5.3 (≥100 held-out, per-route CI, separate τ split) |
| 9 | sandbox: dotenv at import, smoke import side effects, fixture restore, `mcp=None` | §5.1 (`httpguard.py`, env before import, restoring fixture) |
| 10 | CLI persistence of executing state | §3.1 (`on_before_resume` in `PMAssistant`) |
| 11 | warnings wiring could diverge | §4.5 (`approval_warnings()` for both cards) |
| 12 | extractor model, once-per-turn, off reply path, suggestion retention | §4.2, §7 |
| 13 | httpx2 not covered by the guard; pin exactly | §5.1, §J.3 |

**Round 3: PASS (0 blocker, 0 major; 8 minor, all folded in as v3.1).** The
reviewer verified offline:
- the Jev client settings, using `httpx2.MockTransport`;
- that `remember_fact` can read ids from `invocation_state`, with the pattern
  already used in `diagram_tools.py`;
- that the scope warning covers multi-turn confirms and still catches keys that
  came only from memory;
- that the `__main__`-only guard keeps the existing tests meaningful.

| # | Minor | Where |
|---|-------|-------|
| 1 | background extraction usage would be lost or misattributed | §4.2 (`memory.usage` event per `turn_id`) |
| 2 | no lane could bind `remember_fact` (the query lane is read-only) | §4.2 (a `memory` lane plus a deterministic pre-route; `gate.TOOL_MODULES`) |
| 3 | stale `add_memory` references | §4.5, §10 |
| 4 | `unchecked_scope_warning` misses the incident flow | §4.4 (count keys since the last completed write) |
| 5 | per-phase timeout overstated; breaker not thread-safe | §J.3 (2 s total deadline, catch `Exception`, locked breaker, assert timeout extensions) |
| 6 | the sync client in an async search; placement 5 missing from governance | §J.2 #5 (`AsyncTypeSafeClient`), §J.0 row |
| 7 | the tools layer must stay framework-free | §4.2 (`FactFiles` in tools/, `FactStore` adapter outside) |
| 8 | guard leftovers: smoke needs no flag; name the easy bypass | §6 |
