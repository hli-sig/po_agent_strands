# Evidence that the rewrite is done

Each claim below lists what was checked, how, and where the proof lives. Re-run
anything marked with a command.

## 1. The process

| Step | Result | Where |
|------|--------|-------|
| Understand the original | read CLAUDE.md, `main.py`, `graph.py`, every lane, the tool layer; ran its suite (357 passed, 1 pre-existing doc-claim failure) | — |
| Plan | written before any code | `docs/PLAN.md` |
| Independent plan review, round 1 | NEEDS CHANGES: 3 major, 10 minor (docstring loss, abandoned interrupts, unsafe live test, …) | `docs/PLAN.md` §9 |
| Round 2 | NEEDS CHANGES: 2 major (the smoke blocker would have broken reads; MCP double-start), 5 minor | §9 |
| Round 3 | **PASS**; 5 minor notes folded in | §9 |
| Implementation, checked against the plan at each step | see §6 below for deviations | — |
| Independent implementation audit | **PASS**: 0 blocker, 0 major, 9 minor, all fixed | §7 |

## 2. Offline test suite

```bash
uv run pytest -q          # 554 passed, 14 skipped (FY fixtures are real data, not in the repo — same as the original)
```

| File | Tests | What it proves |
|------|------:|----------------|
| `tests/test_gate.py` | 11 | real Strands loop + `ApprovalGate`: a write's body does not run before approval; approve runs it once; reject never runs it and the model sees why; read+write batches gate as a whole; unknown MCP-like tools gate; unregistered names don't |
| `tests/test_assistant.py` | 20 | every writing lane pauses; the query lane can't be asked to write; the MCP tool gates and the client is closed; lanes share history; the router sees the previous route; abandoned approvals leave the lane usable, both on the same lane and after moving to another; no duplicate results after a failed resume; the diagram brief runs on the calling agent's model (ToolContext) |
| `tests/test_cli.py` | 11 | approval rendering, only `y`/`yes` approves, Ctrl-D abandons, context overflow (stubbed *and* raised through the real Strands loop), a full CLI turn with a real agent + ConsoleEcho |
| `tests/test_requirements.py` | 15 | PRD renderer (ported) + the loop: missed requirements drive a revision, the cap ends it, separate system prompts |
| `tests/test_agent_lanes.py` | 89 | ported invariants: every `@tool` classified, gate lists agree, every route resolves, continuation rules, classifier context, approval-prompt provenance, scope warning |
| `tests/test_tool_specs.py` | 27 | no docstring prose is lost on the way to the model (caught 2 real regressions during the port) |
| `tests/test_learning_docs.py` | 134 | every `path::Symbol` and path cited in the docs exists |
| `tests/test_jira_tools.py`, `test_adf.py`, `test_confluence_tools.py`, `test_finance_tools.py`, `test_spreadsheet_tools.py`, `test_fy_budget_conversion.py` | 261 | the domain layer, ported from the original (only `.invoke({...})` → direct calls; Confluence image tests rewritten for Strands `ToolResult`s) |

**Mutation check.** The gate was disabled on purpose (`if not gated:` → `if True:`
in `pmagent/gate.py::ApprovalGate.check_batch`): **7 of 11 gate tests failed**.
After restoring it, all 11 pass. The suite catches a broken gate.

## 3. Live runs (real model gpt-5.6-luna, real Jira, writes blocked)

Both runs used `scripts/smoke.py`. It self-checks its block before starting: seven
write URLs are asserted blocked, and those are the `BLOCKED` lines at the top of
each transcript.

`docs/evidence/smoke_transcript.txt` (inputs: `smoke_turns.txt`)

| Turn | Route | Shows |
|------|-------|-------|
| who owns CSCI-1934… | query | `read_jira_issue_details` against real Jira |
| status of sprint 31 | sprint | deterministic metrics (231.5 pts, 60.7%) narrated by the model |
| search confluence… | query | Confluence CQL search |
| add a comment… | ticket | the lane's prompt asks for confirmation *before* calling the tool, as in the original |
| `y` at the gate | ticket | the approval prompt shows the full comment. After approval, the write reached HTTP and was **blocked by the smoke guard** (`BLOCKED POST /rest/api/3/issue/CSCI-1934/comment`). The agent reported that nothing was posted |
| PRD notes | requirements | writer↔reviewer loop → full Atlassian-template PRD |
| FY input folder | finance | `inspect_fy_budget_inputs`, honest "folder does not exist" |

`docs/evidence/smoke_transcript_2.txt` (inputs: `smoke_turns_2.txt`)

| Turn | Shows |
|------|-------|
| post the comment, `n` at the gate | **gate rejection live.** There is no HTTP `BLOCKED` line for CSCI-1934, so the tool body never ran. The rejection text reached the model, and it answered accordingly |
| read DimEmployee on the EDM page | Confluence **image** in a `ToolResult`: the model listed 13 columns that exist *only in the screenshot*, which proves the Strands OpenAI image split end to end |

**Jira was checked afterwards.** CSCI-1934 was read back from real Jira: no
comments. Nothing was written.

**Model configuration was checked live.** gpt-5.6-luna via `OpenAIModel` without
`reasoning_effort="none"` returns HTTP 400 ("Function tools with reasoning_effort
are not supported"). With it, tool calls and structured output both work.
`OpenAIResponsesModel` with `reasoning.effort=low` also works. That is why
`pmagent/llm.py::get_model` is written as it is.

## 4. Domain code is verbatim

```bash
uv run scripts/verbatim_report.py     # → docs/evidence/verbatim_report.txt
```

- **37 files IDENTICAL**: all prompts and skills, the Jira client, fields, JQL,
  matching, metrics and render, ADF, the whole FY budget package, and the finance
  and spreadsheet lane declarations.
- **16 CHANGED**, for three reasons:
  - framework-only changes: the `@tool` import, `.func(` → direct call, and
    `state` → `schemas`;
  - docstrings that named `graph.py`;
  - the agent layer, which was rewritten on purpose.
- For `finance_tools.py`, a sorted-lines diff shows the only changed *line* is the
  import. The two paragraphs moved above `Args:` are otherwise unchanged.
- **9 NEW**: the Strands agent layer. **4 REMOVED**: `graph.py`, `state.py`,
  `orchestrator.py` (split into `router.py` and `query_agent.py`), and the empty
  `jev.py`.

## 5. Learning material

- 9 lessons (at the end of part 1; part 2 added lessons 10–11), a concept map and a
  course index in `docs/learning/`.
- 8 runnable examples. All 8 run offline:

  ```bash
  for f in docs/learning/examples/0*.py; do uv run "$f"; done
  ```

- Examples 03, 05 and 07 were also run `--live`. In 05, a real model went pause →
  approve → run, then pause → reject → refused. In 07, the Graph reflection loop
  completed writer → reviewer → writer → reviewer.

## 6. Deviations from the plan

- The approval-rendering tests stayed in `tests/test_agent_lanes.py` (where the
  original had them) instead of a new `test_approval.py`. They are the same tests,
  in the same place as the original.
- The plan listed 6 examples; there are 8. Shared history and a local MCP server
  were added.
- `main.py` quiets Strands' own log output (`PMAGENT_LOG_LEVEL` to override). The
  first live run showed a Strands WARNING line interleaving with the chat.
- ConsoleEcho shows an image as `[image: png]` rather than `[image: title]`. A
  Strands image block has no title field, and the text label before each image
  already carries it.
- `draft_diagram_brief` became `@tool(context=True)` so it can use the calling
  agent's model (a finding from the audit).
- `.env` was copied from `../PO_Agent` so the app runs. It is gitignored and
  `chmod 600`.

## 7. Independent implementation audit

The same reviewer who approved the plan audited the implementation against it. It
read the agent layer, ran the suite and the offline examples, and checked the
lessons' claims against the SDK source. It did not run `main.py`, `smoke.py` or
`--live`, because the `.env` points at production.

**Verdict: PASS.** There were no blocker or major findings. Every item in plan
§3–§8 and every review-3 note was found implemented. Its 9 minor findings are all
fixed:

| # | Finding | Fix |
|---|---------|-----|
| 1 | lesson 2 overstated how verbatim `tools_write.py` is | reworded to match the verbatim report |
| 2 | lesson 1 listed `"max_tokens"` as a stop reason | removed; noted that `MaxTokensReachedException` is raised instead |
| 3 | lesson 6 put `HumanInTheLoop` in `hooks=` | it's `Agent(interventions=[...])`; fixed in the text and the exercise |
| 4 | lesson 5 said `BeforeInvocationEvent` can't change anything | it can `cancel` and replace `messages`; table fixed |
| 5 | example 05 said "Lesson 5", and approve and reject ended the same way | now "Lesson 6", with distinct closing replies (example 04 and 06 headings fixed too) |
| 6 | the overflow test used only a stub | added `tests/test_cli.py::test_a_real_context_overflow_reaches_the_cli_unwrapped` |
| 7 | no test for abandoning on one lane and continuing on another | added `tests/test_assistant.py::test_abandoning_on_one_lane_and_continuing_on_another` |
| 8 | `draft_diagram_brief` ignored an injected model | `@tool(context=True)` + `tool_context.agent.model`; test added; taught in lesson 2 |
| 9 | this section was pending | this section |

Afterwards: `uv run pytest -q` → **554 passed, 14 skipped**. All 8 examples run
offline. The verbatim report is regenerated (37 identical / 16 changed / 9 new /
4 removed).

---

# Part 2: the web UI, HTTP API and thinking chain

## 8. Process

| Step | Result | Where |
|------|--------|-------|
| Style reference | measured from typesafe.ai's HTML: ink `#1E1E1E` / paper `#FEFEFE` / greys / pink `#F386A1` / magenta `#D45BB6`, monospace-first type, sharp 1px windows. Commercial fonts were replaced by open ones; no TypeSafe branding | `docs/PLAN_WEB.md` §1 |
| Plan | written before any code | `docs/PLAN_WEB.md` |
| Plan review, round 1 | NEEDS CHANGES: 4 major, 9 minor, including an SSE design that would stall the server, DNS-rebinding approval, Markdown exfiltration, and wedged conversations after failures | `docs/PLAN_WEB.md` §10–11 |
| Round 2 | first attempt lost to a reviewer rate limit; the retry → **PASS**, with 8 minor notes adopted | §11 |
| Implementation audit | see §13 | |

## 9. Offline tests

```bash
uv run pytest -q          # 664 passed, 14 skipped (with Chromium for the browser tests)
                          # 661 passed, 17 skipped without it: the 3 browser tests skip cleanly
```

| File | Tests | What it proves |
|------|------:|----------------|
| `tests/test_web_api.py` | 32 | see the list below |
| `tests/test_web_ui.py` | 3 | **the page in real Chromium**, offline (real uvicorn + ScriptedModel):
  - a turn streams into chat and chain, and a reload rebuilds both identically with the composer unlocked;
  - the approval card blocks the write until **Approve** is clicked;
  - model output containing `![](https://evil…)`, `<img onerror>` and `<script>` renders no image and runs nothing.

  **Mutation-checked:** allowing `<img>` in DOMPurify fails it |
| `tests/test_web_entry.py` | 3 | `web.py` binding: loopback by default; a non-loopback bind needs a token; `0.0.0.0` needs `--allowed-host` |
| `tests/test_web_concurrency.py` | 1 | a **real uvicorn** server: while one conversation's SSE stream is held open mid-turn, `/health` and a whole second conversation complete. **Mutation-checked:** with the wait moved onto the event loop, `/health` times out and the test fails |
| `tests/test_course_build.py` | 2 | `course.html` equals the build from the Markdown, and follows the Artifact contract |
| `tests/test_learning_docs.py` | 169 | every `path::Symbol` cited in all docs, including lessons 10–11, exists |

`tests/test_web_api.py` covers:
- 201/202/204 with `Location`;
- the full thinking-chain event order;
- approve and reject over HTTP, with the reject never running the tool;
- approval text byte-equal to the CLI's;
- two approvals in one turn;
- 409s;
- failure cleanup (R2) and abandoned approvals;
- context-full;
- idempotency (retry → same turn, one model run; reuse → 422);
- the PRD loop in the chain;
- 422 / 401 / 400 bad-host / 403 foreign-origin, all as problem+json;
- a strict CSP everywhere, relaxed only on `/api/docs`;
- the OpenAPI route list;
- no inline script, style or handlers in the page;
- vendored-library hashes.

## 10. Live runs (real model, real Jira, writes blocked by `scripts/smoke.py --web`)

`docs/evidence/web_live.jsonl` (123 events, driven over HTTP by `scripts/web_drive.py`):

| Message | Thinking chain shows |
|---------|---------------------|
| who owns CSCI-1934… | route query (classifier) → `read_jira_issue_details` ok 502 ms → reply |
| post a comment…, **reject** | route ticket → `approval.required` (description = CLI text) → `approval.decided: rejected` → **no `tool.started`** → the model acknowledges |
| post it now, **approve** | `approval.decided: approved` → `tool.started add_jira_comment` → `tool.finished error: smoke: write blocked` (blocked at HTTP) → an honest reply |
| PRD notes | route requirements → `prd.step` writer pass 1 → reviewer approved → render → the PRD |

`docs/evidence/web_live_reasoning.jsonl` records reasoning in the chain, live.
- At `LLM_REASONING_EFFORT=high` with `LLM_REASONING_SUMMARY=detailed`, the turn
  streamed **275 `reasoning.delta` events (1,417 chars)**, beginning before the
  tool call.
- Measured along the way:
  - at `medium`, gpt-5.6-luna often returns no summary, particularly with the
    sprint lane's long system prompt (0 of 2 web runs, 1 of 3 direct probes);
  - at `high`, it returned one in 3 of 3 probes.
- The docs, the UI note and `/meta` say this plainly.

**Jira afterwards:** no comment was written to CSCI-1934.

## 11. The UI in a real browser

`scripts/ui_screenshots.py` drives Chromium (Playwright) like a person would:
typing, pressing Enter and clicking Reject. The screenshots are in
`docs/evidence/ui/`:

| File | Shows |
|------|-------|
| `01-start.png` | first load: help with example prompts, the model/project line, the reasoning-off note |
| `02-sprint-chain.png` | a sprint report: route → tool (with duration) → writing → usage (≈15k tokens, 3 model calls) |
| `03-approval-card.png` | the pink approval card with the exact comment text; the chain paused "waiting for you"; the composer locked |
| `04-rejected.png` | after Reject: "nothing was written" |
| `05-prd.png` | the PRD with tables, and the writer/reviewer steps in the chain |
| `06-phone-after-reload.png` | 400px wide, after a reload: stacked windows, chat and chain rebuilt from the API |
| `02-reasoning-streaming.png`, `03-reasoning-done.png` | the reasoning summary streaming into the chain, then the finished turn |

**Browser console: 0 errors, 0 warnings** in both runs (`console.json`,
`console-reasoning.json`), so there were no CSP violations. Playwright's own
`wait_for_function` was blocked by the page's CSP (`'unsafe-eval'` is not
allowed), which is further proof the policy is live.

### Bugs the browser run found, and fixed

1. A chain step created without metadata crashed on the next `text.delta`. The
   client's retry loop then swallowed the exception, so the stream silently stopped
   mid-answer. Fixed: the metadata span always exists, a render error is logged and
   the stream keeps going, and only network errors are retried.
2. At phone width, the two windows shared one viewport height and overlapped.
   Fixed: stacked windows get their own heights and the page scrolls.
3. After a reload, replaying an old approval card left the composer locked.
   Fixed: the final state comes from the server's conversation status.
4. Reasoning summaries arrived as raw Markdown with sections run together. Fixed:
   they render through the sanitised Markdown path, with section breaks.

## 12. Course

- **11 lessons**, including the new **10 (Serving over HTTP)** and **11 (the
  thinking chain)**.
- **9 examples**, including the new `11_trace_events.py`. All 9 run offline.
- **`docs/learning/course.html`**: the whole course as one page. It is generated
  by `scripts/build_course.py` and has an API reference built from the live
  OpenAPI document. It is checked by `tests/test_course_build.py` and published
  as the course web page: https://claude.ai/artifact/4iiYRmxiRbsb6pE8ZmYBdS
  (private to its owner until shared). To update it, rebuild and republish the
  same file.
- **Privacy:** the published page embeds **demo-data** screenshots
  (`scripts/course_figures.py`: the real app, UI, agents and gate, with a scripted
  model and fake Jira). The real-data screenshots in `docs/evidence/ui/` contain
  colleagues' names from the live Jira, so they stay local.

## 13. Independent implementation audit (web)

The same reviewer audited the web implementation against `docs/PLAN_WEB.md`. It
read every file, ran the suite and the examples, and checked the XSS sinks in
`app.js` and the SDK claims in lessons 10–11.

**Verdict: PASS.** There were no blocker or major findings. Its checks included:
- the off-loop SSE wait, and that the concurrency test is meaningful;
- the host/origin middleware and CSP split;
- no unsanitised `innerHTML` sink;
- R2 cleanup, R5 usage, R8 approval ids and R10 lifecycles;
- the live evidence.

All 8 minor findings were fixed:

| # | Finding | Fix | Proof |
|---|---------|-----|-------|
| 1 | race: a decision made the instant `approval.required` fires could have "running" overwritten | the approval is recorded, the event emitted and the status set in one step under the session lock (`Turn.emit(..., status=)`); terminal events are atomic too | `test_a_decision_racing_approval_required_is_not_overwritten`: passes, and **fails when the old ordering is restored** |
| 2 | an exception inside `_finish` left the turn "running" forever | `_finish` is wrapped, so `_fail` runs (abandon + `discard_pending`) | `test_a_crash_while_finishing_fails_the_turn_instead_of_leaving_it_running` |
| 3 | `idempotency-key-in-use` could never be returned | removed; check-and-create is atomic, so a concurrent retry waits and gets the same turn | the idempotency test |
| 4 | client: a stream cut cleanly mid-turn unlocked the composer; `abandoned` set the turn running | `follow()` reconnects from `Last-Event-ID` while the turn runs; only approved/rejected resume | `test_web_ui.py` |
| 5 | `--host 0.0.0.0` put the bind address in the Host allowlist (unusable); plain HTTP carries the token | `--allowed-host` (repeatable); `0.0.0.0` refused without it; a TLS warning | `tests/test_web_entry.py` |
| 6 | the middleware's own 400/403 responses lacked security headers | refusals go through the header-adding `send` | `test_the_middlewares_own_refusals_carry_security_headers` |
| 7 | no automated frontend test | `tests/test_web_ui.py`: Chromium, offline, 3 tests (skips cleanly without Chromium) | 3 passed; XSS test mutation-checked |
| 8 | stale counts, and this section pending | updated | this document |

## 14. Tester review of the published course

A separate tester agent worked through the published course as a learner. It had
no involvement in building it. It read every lesson, ran every offline command and
example, attempted the exercises (and reverted its experiments, checked by file
hashes), and checked the wording against the code and the SDK.

**Verdict: NEEDS FIXES**, with 0 blockers, 7 major and 17 minor issues. It
confirmed that all examples run, all citations resolve, the published page matches
the source byte for byte, and the mutation exercises reproduce.

All 24 issues were fixed:
- **Safety and offline paths** (issues 1–3, 7):
  - a "Before you start" setup section, stating that live exercises go through
    `scripts/smoke.py [--web]`;
  - a new **offline web demo**, `scripts/demo_web.py` (real app, agents and gate;
    scripted model; fake Jira), so lessons 10–11 need no key. It is tested by
    `tests/test_demo_web.py`, and the lesson 11 curl exercise was run against it
    verbatim;
  - the Start page describes the three kinds of exercise (code / run / think),
    and every step that needs a real model or Jira is marked **live**.
- **Wrong claims** (4, 5, 14, 15, 18): the Model interface (four methods), the
  hooks table (order, `resume`, `cancel`), callback handlers, `build_model`,
  `decision_url`'s schema, which test covers which row, and six lane agents
  rather than seven.
- **Misleading examples** (6, 10–12): `06_interrupt_gate.py --ask` reports what
  actually happened; the hello answer, the tools notice (computed from the spec)
  and the shared-history exercise are fixed.
- **Structure** (8, 9, 17, 19, 20, 24):
  - examples are renumbered to their lessons (`04_shared_history`, `05_hooks`,
    `06_interrupt_gate`, `11_trace_events`);
  - forward pointers and the `classify=` stub are added to early exercises;
  - lesson 8's approved-tools step is corrected;
  - every lesson now has a "how the LangGraph original did it" section.
- **API reference** (16): each route has a clean summary and only the error
  codes it can return, plus a note on 400/401/403.
- **Docs and config** (13, 21–23): the provider wording; `.env.sample` gains
  `LLM_REASONING_SUMMARY`, `PMAGENT_LOG_LEVEL`, `JIRA_STUCK_THRESHOLD_DAYS` and
  `PMAGENT_API_TOKEN`; the README counts and allowlist; the note that browser
  tests skip; and a glossary (lane, route, the Bedrock Converse shape, and PO Agent
  vs PM Agent vs PO_Agent).

### Round 2 (re-test of the fixes)

**Verdict: READY.** No blockers or majors remain: 20 issues were resolved and 4
partly resolved. The tester ran the offline demo, and the lesson 11 curl block
worked verbatim.

It reported 7 new minor issues. Those, and the 4 partials, are fixed:

| Item | Fix |
|------|-----|
| Nested lists rendered as literal hyphens on the page (present since v1) | `scripts/build_course.py::loosen_lists` re-indents nested items to 4 spaces, and turns a fenced block inside a list item into an indented code block. Checked in the generated HTML |
| Live steps not marked (1.1, 1.2, 3.1, 3.2, 6.1, 7.1) | marked **live**, with offline alternatives where they exist |
| "The code" pointed at the author's home directory | describes the repository without a machine path |
| `02_tools.py`: 16 vs "16.0" unexplained | the output explains it |
| EVIDENCE counts, and this section's own numbers | corrected (above) |
| The demo's fallback suggested plain `web.py` first | now recommends `smoke.py --web`, with `web.py` only for a non-production `.env` |
| The demo's `/meta` exposed the real `JIRA_BASE_URL` | `demo_web.configure()` sets it to None (verified: `null`) |
| Lesson 10: Swagger UI needs internet | noted, with a curl alternative |
| Port 8000 hard-coded | lessons 10–11 say how to use `--port` |
| The API reference claimed 401 on every route | "every route except `/api/v1/health`" |

# Part 3: the course gains Rebuild and Extend (2026-09-24)

## 15. What was added

- **Part 2 · Rebuild** (`docs/learning/rebuild.md`): steps R0–R8 from an empty
  folder to a safe live run. Each step ends with the reference tests copied in.
- **Part 3 · Extend**: lessons 12 (run log), 13 (persistence), 14 (long-term
  memory), 15 (evals), 16 (Claude Code harness) and 17 (Jev). These are guided
  builds that follow `docs/PLAN_HARNESS_MEMORY.md`, with no solution in the repo.
  Each has a definition of done as tests to write, plus traps and exercises.
  Files the learner creates are marked ➕.
- New offline examples: `12_runlog_hook.py`, `13_snapshots.py`,
  `14_memory_injection.py`, `15_eval_experiment.py` (run with
  `--with strands-agents-evals==1.4.0`) and `17_jev_cascade.py` (fake client).
- `scripts/build_course.py` has Part labels in the nav and a new hero.
  `tests/test_course_build.py` checks lessons 1–17 and the rebuild section.
- The plan's §9 now maps phases to lessons. The audit also fixed three plan
  details:
  - §2.1: event sources;
  - §4.2: `memory.usage` (no longer contradictory);
  - §8: a session-wide conftest.

## 16. Verification

- A dry-run rebuild in a scratch folder: every checkpoint closes, with the
  counts stated in the guide (R1 206+14s, R2 274+14s, R3 11, R4 15, R5 20,
  R6 420+14s, R7 457+17s).
- `uv run pytest -q`: 705 passed, 17 skipped. `build_course.py --check` is up
  to date.

## 17. Independent course audit

The auditor followed the course as a learner, offline only, in a scratch folder.
It ran R0–R1 literally and checked R2–R7 against the reference code, then ran
all the examples. It also checked every Strands 1.57 and typesafe-sdk 0.7.1
claim against the installed packages, and built spikes of lessons 12, 13, 14
and 16.

| Round | Verdict | Findings |
|-------|---------|----------|
| 1 | NEEDS CHANGES | 2 major, 20 minor. The majors: lesson 12's per-test conftest let web worker threads write to the real `data/runlog/`; lesson 16's "allowed path" test could have started the real CLI |
| 2 | **PASS** | all 22 fixed (both majors verified in spikes: `data/` stays empty over 3 full runs; with the guard removed, the refusal test still starts nothing); 5 new minor notes, all fixed afterwards (`data_dir()` for memory and resolved per call, "route change" dropped from the scope-warning rule, exercise 3's pre-route case, two nits) |

## 18. Lesson 8 case study: the sprint review MCP server

The owner's sprint review tool (a Jira CSV → stakeholder deck workflow, first an
MCP server, later handed over as a Codex skill plus a PowerShell analyser) became
lesson 8's "server you own" case study. Only the synthetic `DEMO-*` fixture came
into the repo. The real reference deck, company and people names stay in the
handover package, which `.gitignore` now excludes (`*.zip`).

- `docs/learning/examples/sprint_review/analyser.py::analyse` is a Python port of
  `analyze-sprint.ps1`. It reproduces the handover self-test's figures exactly
  (6 exported, 5 included, 2 Done, 80% of 5 started). Its intended differences
  from the original are listed in its docstring.
- `docs/learning/examples/sprint_review/server.py::create_server` covers the
  handover design review's findings: path trust, no silent overwrite, and
  refusals the model can read (`ToolError`).
- `docs/learning/examples/08_sprint_review_mcp.py` drives the server through the
  real approval gate. Rebuild step R9 builds it.

| Round | Verdict | Findings |
|-------|---------|----------|
| 1 | NEEDS CHANGES | 1 major: R9 from the guide alone reached 27 of 34, because the schema was unspecified. 6 minor: narrow date parsing, a crash on non-UTF-8 input, the file read twice, a save through a `runs` symlink, an overstated stdout trap, the zip not gitignored. 6 nits |
| 2 | **PASS** | all 13 fixed and checked with spikes; R9 from the guide alone reached 51/51 on the first try. 4 minor and 6 nits, all fixed afterwards: the `0.0.0.0` banner URL, seeding the volume, `--env-file` quoting, the image carrying `PROVENANCE.md`, anonymous volumes, CSV/OS errors as refusals, and more |

## 19. Distribution: the Docker image

The owner asked for Linux/Python only and a Docker image. The repo contained no
PowerShell; the sprint review analyser was ported to Python.

- `Dockerfile`: two stages, `uv sync --locked --no-dev`, a non-root user,
  read-only code, a writable `/app/data` (with `HOME` in it), and no `VOLUME` line.
- `.dockerignore` is an allowlist, so `.env`, `data/`, `docs/evidence/`, zips and
  `PROVENANCE.md` never enter the image.
- The entrypoint `scripts/container.py` offers `demo` (the default, offline),
  `web`, `cli`, `smoke`, `smoke-web`, `sprint-review-mcp` and `examples`, plus
  passthrough. `tests/test_docker.py` checks the build context with Docker's
  matching rules and starts `demo` for real.
- Verified on Docker Engine 29.5.3:
  - the image is 566 MB and runs as `app`;
  - the examples command runs all 14 offline examples;
  - the demo answers health checks on a published port;
  - `web` refuses to start without `PMAGENT_API_TOKEN`; with one it returns 401
    without the token and 200 with it, and 400 for an unknown Host;
  - lesson 8's `MCPClient(command="docker", …)` snippet drives the containerised
    server over stdio with `--network none`.

## 20. A fresh rebuild: everything from an empty folder

The owner asked that the rebuild and the course cover everything, including the
domain tools, and start fresh. Part 2 no longer copies anything from the original
LangGraph project.

- `docs/learning/rebuild-domain.md` (D0–D7) builds the domain layer from its
  tests: config and contracts, ADF, Jira, Confluence, the FY budget converter,
  the finance tools, the spreadsheet queue and the knowledge seam.
- `docs/learning/rebuild.md` (R0–R10) builds everything else, then the MCP
  server (R9) and the Docker image (R10).
- The learner copies only tests, Markdown prompts and skills, the web page, the
  reviewed `scripts/smoke.py`, and config files.
- New tests give every module a runnable checkpoint:
  - `tests/fy_synthetic.py` and `tests/test_fy_budget_synthetic.py`: the FY
    converter on made-up workbooks in both real layouts; the real-data suite
    still skips.
  - `tests/test_model_layer.py`, `tests/test_diagram_tools.py`,
    `tests/test_company_knowledge.py`, and more spreadsheet and Jira-surface
    tests.

| Round | Verdict | Findings |
|-------|---------|----------|
| 1 (four auditors, each test-running a slice from the guide alone) | A NEEDS CHANGES, B NEEDS CHANGES, C PASS with changes, D NEEDS CHANGES | 4 major: Jira tool signatures unspecified; env defaults missing; the FY output contract unpinned; FY validation never tested to fail. Plus about 40 minor and nit findings on contracts and wording. Every stated checkpoint count matched |
| 2 (verification) | NEEDS CHANGES | all round-1 findings fixed, and 10 mutations caught. 1 new major: the D4 test imported a D5 module. 2 minor |
| 2, confirmed | **PASS** | a build-order simulation matches every checkpoint: D1 26, D2 141, D3 41, D4 34+14s, D5 41, D6 12, D7 2, R1 297+14s, R2 335+14s, R3 11, R4 15, R5 20, R6 481+14s, R7 518+17s, R9 52, R10 36 |

Left for the owner to decide:

- The production Jira client passes no HTTP timeouts. The guide recommends them
  and the fakes accept them.
- `.env.sample`'s flagged and sprint field ids differ from the code defaults.
- `PROVENANCE.md`, the `CSCI` key and the converter's real business codes are
  still in the repo, though outside the Docker image.

## 21. No reference at all: the self-contained rebuild

The owner clarified that the rebuild must use **no reference**, and chose
"learner writes the tests". `docs/learning/rebuild.md` (R0–R10) and
`docs/learning/rebuild-domain.md` (D0–D7) were rewritten as a complete
specification:

- Every file is written by the learner: the code, the tests, the prompts, the
  skills, the web page (with self-downloaded, hash-recorded libraries), the
  scripted model, the FY fixtures, and the smoke launcher. The sample CSV is part
  of the page.
- Each step has a table of cases the learner's own tests must cover, plus a
  "break it" check. There is no `$REF`, no copying, no "compare with", and no
  citation of repo code.

| Round | Verdict | Findings |
|-------|---------|----------|
| 1 (four reviewers rebuilt their slices from the course text alone, code and tests) | A NEEDS CHANGES, B **PASS**, C NEEDS CHANGES, D NEEDS CHANGES | 4 major: R0's checkpoint needed `git init`; D0's env-default recipe re-read a real `.env`; R2's docstring rule couldn't catch its own "break it"; the course README still called the repo's tests the checkpoints. About 50 minor contract gaps. The scripted-model spec worked against Strands 1.57 exactly as written; D4's layout spec produced a converter and fixtures that agreed first time; D rebuilt R9 and R10 and ran the image live |
| 2 (verification) | **PASS** | all findings fixed; spikes confirmed R0, the `.env`-safe recipe, the docstring rule (fails below `Args:`, passes above) and R5's shared-history behaviour on real Strands. 2 minor and 4 nits, all fixed afterwards: the Graph table rename, the blank-date rule, `TicketDraft` `None` defaults, `requests()`, the diagram-connected condition, a README typo |

One code change came out of it: the sprint review server's `list_exports` no
longer cuts its list at 200 silently.
