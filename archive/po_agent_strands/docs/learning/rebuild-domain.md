# Rebuild the domain layer (D0–D7)

The domain layer is everything the agents *call*: the Jira client and its tools,
Confluence, the Markdown↔ADF converter, the FY budget converter, the spreadsheet
approval queue and the company-knowledge seam. It is most of the code (about half
of the project), and none of it is about Strands. That is the point: **an agent
framework should be a thin layer over plain code**. This chapter teaches you that
plain code, and you build all of it, with its tests, from an empty `pmagent/`
folder.

This is step R1 of [Rebuild it yourself](rebuild.md). Do R0 first, then D0–D7
here, then carry on at R2.

## How this part works

- **Nothing is copied.** You write every file: the code, its tests, the prompts,
  the skills, the fixtures. This page is the whole specification.
- **Each step has a table of cases.** Write a test for every row, then the code
  that makes them pass (or the other way round, as you prefer). The table is the
  spec: it lists the behaviour that matters, including the traps. Where a later
  step depends on a name, a signature or an exact string, the step says so.
- **A checkpoint is "your tests pass, and they cover every row".** Then break the
  code on purpose once (the step suggests how) and watch a test fail. A test that
  has never failed proves nothing.
- **Use the module names given.** Later steps import them.
- **Offline throughout.** No step needs a Jira, a Confluence, a Microsoft account
  or any budget data. Every client is tested through a fake you inject in its
  place (rule 7).

### The rules every domain module follows

Each rule is here because breaking it once caused a real failure.

1. **The model does judgment; Python does arithmetic and formatting.** Sprint
   metrics, the PRD layout and every FY budget figure are computed by code. A tool
   returns text that Python rendered, and the model narrates it.
2. **Framework-free.** Nothing under `pmagent/tools/` imports Strands except
   `from strands import tool` (and `ToolContext` in the one tool that needs it).
   The exception is R2's `mcp_tools.py`, which imports Strands' `MCPClient`
   inside a function, because connecting to MCP is its whole job.
3. **Clients are built lazily.** A client that checks credentials in its
   constructor is only ever built by a `get_client()` function on first use,
   never at import. Importing a module must work with no `.env`.
4. **Every tool module declares `READ_TOOLS` and `WRITE_TOOLS`,** even when one is
   empty. The approval gate (R3) uses them, and a tool in neither list is gated as
   a write.
5. **A tool's docstring is its prompt, and prose goes above `Args:`.** Strands
   turns the docstring into the tool description the model reads and drops any
   text after the `Args:` block, so "this WRITES to Jira" must sit above it.
6. **Never truncate silently, never guess.** Every cap on something the agent
   reads as an answer is rendered with a warning. (D7's context cap is the one
   plain cut: that text is background for a prompt, not an answer.) A name that
   matches two people, two transitions or two headings is refused, and the
   candidates are listed.
7. **Inject the boundary; don't add a fake mode.** A test replaces the HTTP
   session (`client._session = FakeSession(...)`, on a client made with
   `Client.__new__(Client)` so the credential check never runs) or the module's
   `get_client` (with pytest's `monkeypatch`). Production code has no `if TESTING:`
   branch.

A fake session for rule 7 is a dozen lines: it holds a queue of JSON payloads,
records every `get`/`post`/`put` as `(method, url, json_or_params)`, and returns
the next payload wrapped in a response object with `status_code`, `json()`, `text`
and a no-op `raise_for_status()`. Give its methods `**kwargs`, so your client can
pass `timeout=` (it should).

## D0 — Configuration, contracts and content (3–4 h)

```bash
cd $MY
mkdir -p pmagent/tools pmagent/prompts pmagent/skills/prd pmagent/skills/ticket pmagent/skills/fy_budget
```

### Code

1. `➕ pmagent/__init__.py`, `➕ pmagent/tools/__init__.py` and
   `➕ pmagent/prompts/__init__.py`: empty (a docstring is fine). Later steps add
   `pmagent/agents/`, `pmagent/cli/` and `pmagent/web/` the same way.
2. `➕ pmagent/env.py`: the only module in `pmagent/` that reads the
   environment. It calls `load_dotenv()` once, then exposes module constants.
   Anything else that needs configuration imports `env`. (The launchers are
   separate: `main.py` reads `PMAGENT_LOG_LEVEL`, and R10's entrypoint reads a
   few container settings.) Each constant has the same name as its variable.
   `LUCID_MCP_ENABLED` is a bool (true for `1`, `true` or `yes`), and the numbers
   are ints. With `JIRA_BASE_URL` unset, `CONFLUENCE_BASE_URL` is just `/wiki`.

   | Setting | Default |
   |---|---|
   | `LLM_PROVIDER` | `anthropic` |
   | `LLM_MODEL` | `claude-sonnet-5` for anthropic, `gpt-5.6-luna` for openai |
   | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | none |
   | `LLM_REASONING_EFFORT` / `LLM_REASONING_SUMMARY` | `none` / empty |
   | `LLM_MAX_TOKENS` | 16000 |
   | `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` | none |
   | `JIRA_PROJECT_KEY` | `CSCI` |
   | `JIRA_STORY_POINTS_FIELD` / `JIRA_FLAGGED_FIELD` / `JIRA_SPRINT_FIELD` | `customfield_10052` / `customfield_10021` / `customfield_10020` |
   | `JIRA_STUCK_THRESHOLD_DAYS` | 10 |
   | `JIRA_SPRINT_NAME_TEMPLATE` | `Supply Chain Sprint {n}` (turns "31" into the sprint's name) |
   | `CONFLUENCE_BASE_URL` | the Jira site plus `/wiki` |
   | `CONFLUENCE_SPACE_KEY` | `EDP` |
   | `LUCID_MCP_ENABLED` / `LUCID_MCP_URL` / `LUCID_MCP_AUTH_TOKEN` | false / `https://mcp.lucid.app/mcp` / none |
   | `SPREADSHEET_TENANT_ID`, `SPREADSHEET_CLIENT_ID`, `PMAGENT_API_TOKEN` | none |
   | `FY_BUDGET_INPUT_DIR` / `FY_BUDGET_OUTPUT_DIR` | `data/fy_budget/input` / `data/fy_budget/output` |

   `validate()` raises `ValueError` when the chosen provider's key is missing, and
   `validate_jira()` raises when any of the three Jira settings is missing, saying
   there is no offline mode. **Neither runs at import.**
3. `➕ pmagent/schemas.py`: the Pydantic contracts. Every field description is
   an instruction the model reads (a structured-output schema *is* a prompt), so
   write them for the model.
   - `TicketDraft`: `summary` and `description` are **required**. The rest are
     optional: `issue_type` (a `Literal` of Epic, Story, Bug, Task, Spike,
     Sub-task; default Task), `acceptance_criteria` (list), `story_points`
     (`float | None`), `labels`, `components`, `priority`, `parent_key`,
     `assignee` (a name or email, resolved later), `assignee_account_id`,
     `source_issue` (the card this draft came from), `sprint` (`int | str | None`:
     the visible number *or* the name; Pydantic won't turn `31` into a string for
     you), `sprint_id` (the internal id) and `project_key`.
   - `SuccessMetric(goal, metric)`, `PRDRequirement(user_story, importance
     High/Medium/Low, notes, jira_issue)`, `OpenQuestion(question, answer)`, and
     `PRD` (title, objective, target_release, owner, stakeholders, background,
     success_metrics, assumptions, requirements, user_interaction_design,
     open_questions, out_of_scope).
   - `ReviewResult(approved, missing_requirements, issues, revision_notes)`.
   - Required fields: `TicketDraft.summary` and `.description`, `PRD.title` and
     `.objective`, `SuccessMetric.goal` and `.metric`, `PRDRequirement.user_story`,
     `OpenQuestion.question`, `ReviewResult.approved`, `DiagramNode.id` and
     `.label`, `DiagramEdge.from_` and `.to`, `DiagramBrief.diagram_type`,
     `.title` and `.description`, and `RouteDecision.route`. Everything else has a
     default: strings `""`, lists empty, `importance` `"Medium"`, and `TicketDraft`'s
     other optional fields `None`.
   - `DiagramNode(id, label)`; `DiagramEdge` with `from_` aliased to `"from"`
     (`populate_by_name=True`), `to`, `label=""`; `DiagramBrief(diagram_type` a
     literal of flowchart / entity_relationship / architecture / sequence,
     `title`, `description`, `nodes`, `edges)`.
   - `RouteDecision.route`: a `Literal` of ticket, sprint, query, requirements,
     spreadsheet, diagram, finance. Its field description is the router's whole
     definition of each lane (lesson 7): one clause per route. Say that approving
     or changing drafts continues the `ticket` route, and that FY budget files,
     `fy_start` and the budget CSV belong to `finance`.
4. `➕ pmagent/prompts/prompts.py`: `_load(name)` reads a `.md` file beside the
   module as UTF-8. Module attributes load each prompt at import:
   `orchestrator_system_prompt`, `ticket_agent_system_prompt`,
   `sprint_agent_system_prompt`, `requirements_writer_system_prompt`,
   `requirements_reviewer_system_prompt`, `diagram_agent_system_prompt`,
   `spreadsheet_agent_system_prompt`, `finance_agent_system_prompt`.
   `inject_skill(template, skill)` puts a skill into a prompt's `{skill}`
   placeholder with `str.replace`. **Never `str.format`**: prompts are Markdown
   full of braces (JSON, JQL), and `.format` raises `KeyError` on them.
5. `➕ pmagent/skills/__init__.py`: `load_skill(name, file="SKILL.md")` returns
   `pmagent/skills/<name>/<file>` as UTF-8 text.

### Content: the prompts and skills you write

They are Markdown, so they can be read and changed without touching Python.
Write them in the second person, short and concrete. A prompt that names a tool
must name one the lane actually has (R4 binds each lane's tools; R6's tests check
the match both ways). The table says what each must contain; the wording is yours.

| File | Must contain |
|---|---|
| `➕ prompts/orchestrator.md` | the read-only query lane: answer questions from Jira, Confluence and the FY budget tools; never write; name its tools (`query_jira_issues`, `read_jira_issue_details`, `read_jira_issues_by_key`, `list_jira_transitions`, `read_confluence_page`, `search_confluence`, `inspect_fy_budget_inputs`, `read_fy_budget_run`); say that `sprint = ` in JQL takes the *internal* sprint id |
| `➕ prompts/ticket_agent.md` | its tools (R4's ticket lane): `query_jira_issues`, `read_jira_issue_details`, `read_jira_issues_by_key`, `find_jira_user`, `list_jira_transitions`, `read_confluence_page`, `search_confluence`, `validate_ticket_drafts`, `create_jira_issues`, `assign_jira_issue`, `update_jira_issue`, `add_jira_comment`, `transition_jira_issues`, `move_jira_issues_to_sprint`, `remove_jira_issues_from_sprint`; scope is exactly the keys the user named, and `scope` is passed to `create_jira_issues`; the workflow (look for duplicates → decide how many tickets → draft in the reply, not a tool → `validate_ticket_drafts` → ask "Shall I create these?" → create only after a yes); assigning, editing, commenting, transitioning and sprint moves on existing tickets, each only when asked; the draft shape; ends with `{skill}` for the ticket standard |
| `➕ prompts/sprint_agent.md` | its tools (R4's sprint lane): `get_sprint_status_by_number`, `get_sprint_status`, `query_jira_issues`, `read_jira_issue_details`, `read_jira_issues_by_key`, `list_jira_transitions`, `add_jira_comment`, `transition_jira_issues`, `move_jira_issues_to_sprint`, `remove_jira_issues_from_sprint`; "sprint 31" means the *visible* number, so use `get_sprint_status_by_number`; report completion by story points; say when results were truncated; investigate blockers with `read_jira_issue_details`; a standup mode; scope changes, comments and transitions only when asked |
| `➕ prompts/requirements_writer.md` / `requirements_reviewer.md` | the writer captures **every** requirement in the notes as a user story, marks unclear points as open questions, invents nothing; the reviewer re-reads the notes, lists each requirement the PRD missed in `missing_requirements`, and approves only when none is missing. Both end with `{skill}` |
| `➕ prompts/diagram_agent.md` | its tool `draft_diagram_brief` (plus Lucid's, discovered at run time): draft a brief with `draft_diagram_brief`, ask for confirmation, and create the diagram with Lucid's tools only after a yes |
| `➕ prompts/spreadsheet_agent.md` | its tools `prepare_spreadsheet_approval_queue`, `propose_spreadsheet_cell_update`, `apply_approved_spreadsheet_updates`; human approval is mandatory and happens in the workbook: propose a change, the person approves the row in Excel, then apply; never claim a change is applied before it is |
| `➕ prompts/finance_agent.md` | its tools `inspect_fy_budget_inputs`, `create_fy_budget_csv`, `read_fy_budget_run`; the one rule: every figure comes from the converter and is never invented or restated; `fy_start` comes from the user (it must match the first month in the source), never from the model; inspect before creating; a run is successful only if validation passed; ends with `{skill}` |
| `➕ skills/ticket/SKILL.md` | the ticket standard: issue type hierarchy; a summary that is an action, at most 80 characters; a description with context, scope, out of scope and technical notes; testable acceptance criteria (required for a Story or Bug); Fibonacci estimates (1, 2, 3, 5, 8, 13, and split a 13); a parent for every Sub-task; the INVEST check; how to split a requirement into several tickets |
| `➕ skills/prd/SKILL.md` and `template.md` | the Atlassian PRD sections (overview, objective, background, success metrics, assumptions, requirements as user stories, user interaction and design, open questions, out of scope); the writing principles (capture every requirement, measurable metrics, no invention); the reviewer's checklist (coverage, testability, clarity, no invention, scope) |
| `➕ skills/fy_budget/SKILL.md` | the partition rules and the output contract from D4 (the five cuts, the 24 columns, the derived keys), the two input formats, and "validation must pass before a file is used". Write a stub now and complete it after D4 |

**Your tests (`➕ tests/test_config.py`) must cover:**

| Case | Expect |
|---|---|
| each default in the settings table | the constant has that value when the variable is unset: `monkeypatch.delenv` it, patch `dotenv.load_dotenv` to a no-op (the reload would re-read a real `.env`), then `importlib.reload(env)` |
| `validate()` with the provider's key missing | `ValueError` naming the key |
| `validate_jira()` with one Jira setting missing | `ValueError` naming it; importing `env` alone never raises |
| `TicketDraft(summary=, description=, sprint=31)` | valid, and `sprint == 31` stays an int |
| `TicketDraft` without `description`, or with `issue_type="Chore"` | validation error |
| `PRDRequirement(user_story="…")` | `importance == "Medium"` |
| `DiagramEdge(**{"from": "a", "to": "b"})` and `DiagramEdge(from_="a", to="b")` | both work |
| `inject_skill` on a template containing `{"json": 1}` and `{skill}` | only `{skill}` is replaced, no `KeyError` |
| every prompt attribute and every `load_skill` name | loads, non-empty; the ticket, finance and requirements prompts contain `{skill}` |

**Checkpoint D0:** `uv run pytest tests/test_config.py -q` passes. Break it once:
change `JIRA_PROJECT_KEY`'s default and see the test fail.

## D1 — Markdown ↔ ADF (2–3 h)

Jira and Confluence store rich text as ADF (Atlassian Document Format), a JSON
tree: `{"type": "doc", "version": 1, "content": [nodes]}`, where a node is
`{"type": "paragraph" | "heading" | "bulletList" | "orderedList" | "listItem" |
"codeBlock" | "text" | …, "content": [...], "attrs": {...}}` and a text node is
`{"type": "text", "text": "…", "marks": [{"type": "strong" | "code"}]}`. Jira's
API refuses a plain string description, and a single-paragraph dump turns every
heading and bullet into one blob. So `➕ pmagent/tools/adf.py` converts both ways,
over the Markdown subset the ticket standard uses.

- `to_adf(markdown)`: headings (`attrs.level`), bullet and ordered lists, fenced
  code blocks (`attrs.language`, raw newlines kept, nothing inside parsed) and
  paragraphs, plus inline `**bold**` and `` `code` `` through
  `inline_nodes(text)`. **ADF rejects an empty text node** (drop empty
  fragments), and a `doc` must have non-empty `content`, so empty input still
  yields one empty paragraph.
- `to_text(node)` goes the other way, so an agent reading a description or
  comment sees text, not a node tree. It is lossy on purpose: structure that
  carries meaning (headings as `#`, bullets as `- `, code fences, quotes, table
  rows one per line) survives as light Markdown, and styling doesn't. **Unknown
  node types recurse into their children** rather than vanishing, and a mention
  (`{"type": "mention", "attrs": {"text": "@Name"}}`) renders as the name, with an
  `@` added if missing. `None` or junk renders as `""`.
- `render_description(description, acceptance_criteria=None, source_issue=None)`
  builds a ticket body: the description, then an "Acceptance Criteria" heading
  with a bullet per criterion (no heading when there are none), then a "Related"
  line for `source_issue`. That line keeps provenance ("drafted from CSCI-1379")
  in the ticket after the approval prompt is gone.

**Your tests (`➕ tests/test_adf.py`) must cover:**

| Case | Expect |
|---|---|
| `""` | a valid doc with one empty paragraph |
| a blank line between two lines / two consecutive lines | two paragraphs / one paragraph |
| `# H`, `### H` | headings with levels 1 and 3 |
| three `- ` lines; then `1.` lines; a switch from `-` to `1.` | one bullet list; an ordered list; a new list at the switch |
| a paragraph followed by a list | two separate nodes |
| a fenced block with a language, containing `**x**` and newlines | a codeBlock with that language, raw text, no marks |
| an unterminated fence | still a valid document |
| `**b**` and `` `c` `` in a line | `strong` and `code` marks; no empty text nodes anywhere |
| `render_description` with and without criteria and `source_issue` | heading plus bullets only when there are criteria; the Related line only when there is a source |
| `to_text(to_adf(md))` for a multi-section description | the text survives |
| `to_text` on a mention, an unknown node type, a nested list, a code block, a table, `None` | the name; the children's text; the shape; a fence with language; one row per line; `""` |

**Checkpoint D1:** your ADF tests pass. Break it: emit an empty text node for
`"**b**"` and watch the "no empty text nodes" test fail.

## D2 — Jira (1½–2 days)

The biggest module, split into a package. The dependency arrows point one way:
the pure modules at the top need no network, so they import and test with no
credentials.

```
fields.py  jql.py  matching.py  metrics.py  render.py  validate.py    pure
        ↓
client.py        JiraClient, get_client(), resolve_assignee — all the I/O
        ↓
tools_read.py    read @tools  (READ_TOOLS)
tools_write.py   write @tools (WRITE_TOOLS) — gated in R3
__init__.py      re-exports the public surface; __all__ lists exactly it
```

```bash
mkdir -p pmagent/tools/jira
```

`__init__.py` re-exports every tool, `READ_TOOLS`, `WRITE_TOOLS`, `JiraClient`,
`get_client`, `resolve_assignee`, `JqlBuilder`, the metrics and render functions,
the matching and validate functions, and the field helpers; later steps import
them from the `pmagent/tools/jira/` package. Two traps: `__init__.py` must not use `from
__future__ import annotations` (that adds a public name that isn't in `__all__`),
and a paging loop must not assume every Agile response has `isLast`.

**The pure modules**

- `fields.py`: `get_story_points_field()`, `get_flagged_field()` and
  `get_sprint_field()` read `env`. `jira_read_fields()` is the field list for every
  search: summary, status (it carries `statusCategory`), issuetype, labels,
  components, the story-points and flagged fields, assignee, priority, parent,
  created, updated, resolutiondate. Two mappings:
  - `build_issue_fields(draft)` turns a draft dict into a create payload:
    `project.key` (the draft's `project_key`, else `JIRA_PROJECT_KEY`), `summary`,
    `issuetype.name`, `description` (ADF, via `render_description`), then only the
    optional fields that are set: labels, components (`[{"name": …}]`), story
    points (including `0`), priority, parent, assignee (`{"id": …}`), sprint (the
    configured sprint field, internal id). It **omits** an unset field instead of
    sending null.
  - `build_issue_update_fields(changes)` is the edit twin. It emits only the keys
    supplied, refuses acceptance criteria without a description (they are
    rendered into the same field), and refuses a sprint, because Jira won't take
    the sprint field on an edit.
- `jql.py`: `JqlBuilder`, the only place JQL is quoted. It has chainable
  `project`, `sprint` (an int, the *internal* id), `status_category(negate=)`,
  `issue_type`, `assignee`, `text`, `keys` (ignores blanks), `raw`, `order_by` and
  `build`. `build()` joins clauses with ` AND `, appends `ORDER BY created DESC`
  by default, and raises if there are no clauses. Quoting escapes backslashes and
  quotes.
- `matching.py`: exact match or refuse, never fuzzy. A wrong guess reassigns
  someone's work or fires workflow post-functions.
  - `pick_assignee(candidates, query)` ignores inactive users, prefers a unique
    email match, then a unique exact display-name match, then a single candidate.
    Otherwise it returns `(None, alternatives)`.
  - `pick_transition(transitions, target)` matches the destination status first
    and the transition name second, ignoring case and padding. Several matches
    come back as ambiguous; none as `(None, [])`.
- `metrics.py`: sprint arithmetic, with no model involved.
  - `is_done` trusts `statusCategory == "done"` (stable across custom workflows,
    so "Won't Do" counts) and falls back to status names only without a category.
  - `days_in_current_status(raw)` reads the latest status change in the
    changelog, falls back to `created`, returns 0 with no changelog, and never
    goes negative.
  - `is_blocked` counts blocked or flagged work, or *started-then-stalled* work:
    an `indeterminate` issue that hasn't moved for the stuck threshold. Never plain
    backlog age, and never a done issue.
  - `bucket_of` puts each issue in exactly one of done / blocked / in_progress /
    todo, so the point totals always sum.
  - `compute_sprint_metrics(sprint_payload)` is the single source of sprint
    numbers: points per bucket, completion by points, blocked issues, the sprint's
    identity, whether the issue list was truncated, and the risk level: `LOW` when
    completion is at least 0.7 with at most one blocked issue, `MODERATE` at 0.4
    or more, otherwise `HIGH`. An empty sprint doesn't divide by zero.
- `render.py`: every string a Jira tool returns. **The renderer is the
  interface:** a field it omits is, to the agent, a field never fetched.
  `format_issue_line` always shows the owner, and writes "unassigned" when there
  isn't one. `render_sprint_report(metrics, issues)` carries each blocker's owner
  and the truncation warning. `render_issue_detail` shows the description and the
  most recent comments, saying how many were withheld; `_clip` says when it
  truncated. `_render_batch_result(results, verb)` takes the client's per-key
  results, `[{"key": …, "error": None | "reason"}]`, and reports a batch write
  honestly: "Moved 3 of 4 issue(s)", then exactly which keys were NOT moved and
  why. `format_transitions` and `format_candidates` list options for a human.
- `validate.py`: the checkable half of the ticket skill.
  - `parse_drafts(drafts)` returns `(parsed, errors)`, naming each bad draft by
    position without losing the good ones.
  - `check_draft_standard(draft)` returns a list of readable problems (empty
    means ready) against the Definition of Ready: a summary of at most 80
    characters, a parent for a Sub-task, acceptance criteria for a Story or Bug (a
    Task may skip them), Fibonacci points (1, 2, 3, 5, 8, 13) or none, and 13
    reported as worth splitting.
  - `reconcile_scope(drafts, scope)` returns a list of readable differences
    (empty means the sets agree). It compares the drafts' `source_issue` keys with
    the keys the user named, case- and space-insensitively, and reports **both**
    directions: drafted but never asked for, and asked for but not drafted. Two
    drafts from one source, and a draft with no source, are reported too; a
    deliberate split is allowed, so the message says to re-run without scope.

**The client** (`JiraClient`)

- Attributes: `_session` (a `requests.Session` with basic auth and JSON
  headers), `_base` (the site URL without a trailing slash), `_sprint_cache` and
  `_createmeta_cache` (both dicts). Your tests build it with `JiraClient.__new__`
  and set all four (rule 7).
- `search_issues_page(jql, max_results=50, fields=None, include_changelog=False)`
  posts to `/rest/api/3/search/jql` and pages with `nextPageToken`, at most 100 per
  page. It adds `ORDER BY created DESC` when the query has none, and returns
  `(issues, truncated)`, the issues already normalised. **This endpoint has no total count**, so being truncated
  is the only honest thing it can say about a cut result. `expand` is the
  *string* `"changelog"`, not a list.
- `_normalise_issue(raw)` flattens a raw issue to: key, summary, type, status,
  status_category, story_points, assignee, priority, parent, created, updated,
  resolved, blocked, flagged, days_in_status, labels, components. Missing optional
  fields don't crash it.
- Sprints: `resolve_sprint(project_key, ref)` returns the internal id (an int). It turns a visible number into a name
  through `JIRA_SPRINT_NAME_TEMPLATE` (a full name is used as is), and the name
  into an internal id through a board walk (`/rest/agile/1.0/board`, then each
  board's sprints) that is cached per project. Prefer an active sprint, then a
  future one, then a closed one. An unknown name lists what is visible. **A bare
  number is always the visible number.** `get_sprint_issues(sprint_id)` reads
  the sprint's metadata from the Agile API and its issues through JQL
  (`sprint = <id>`, with the changelog), so they share the search path, and
  returns `{"sprint": meta, "issues": [...], "truncated": bool}`: the payload
  `compute_sprint_metrics` takes.
- Writes, each returning one result per input, in input order:
  - `create_issues(drafts)` posts to `/rest/api/3/issue/bulk` in chunks of 50. Map
    `errors[].failedElementNumber` back to each draft, read the error array even
    on a 400 (Jira answers 400 when *every* issue failed), and report a draft Jira
    returned nothing for. With no error array at all, raise with the detail.
  - `move_issues_to_sprint(sprint_id, keys)` and `move_issues_to_backlog(keys)`
    post `{"issues": [...]}` in chunks of 50; a rejected chunk fails every key in
    it, and doesn't hide a chunk that succeeded.
  - `add_comments(keys, comment)`: one request per key (Jira has no bulk
    endpoint), body in ADF; one failure doesn't condemn the others.
  - `list_transitions(key)` and `transition_issues(keys, status)`: look up
    transitions per issue and post the matched id. When nothing matches, explain
    whether the issue is already in that status (read its current status with
    one more GET, only on this failure path) or has no route to it, listing what
    is available.
  - `assign_issue(key, account_id)` (`None` unassigns),
    `search_assignable_users(project, query)` (project-scoped; drops users with no
    account id), `update_issue(key, changes)` (an error when there is nothing to
    change), `get_create_metadata(project)` (cached).
- `get_client()` builds the client on first use. `resolve_assignee(project,
  name)` does the live user search, then delegates the choice to `pick_assignee`.

**The tools.** Their parameter names are a contract: the model uses them, the
approval prompt (R6) reads them, and R7's web tests call them.

| Tool (read) | Parameters |
|---|---|
| `query_jira_issues` | `jql`, `max_results=50` |
| `read_jira_issues_by_key` | `keys`: exact keys; a missing one is named, not dropped |
| `search_jira_issues` | `jql`: `query_jira_issues` with the default cap |
| `get_sprint_status` | `sprint_id` (internal) |
| `get_sprint_status_by_number` | `sprint_number: int` (visible) |
| `find_jira_user` | `name_or_email`, `project_key=None` |
| `read_jira_issue_details` | `issue_keys`, `max_comments=5` |
| `list_jira_transitions` | `issue_key`; returns `"<KEY> can move to:"` and one line per transition |
| `validate_ticket_drafts` | `drafts`: the standard, plus Jira's createmeta: the issue type exists in the project, and every field the project marks required is present |

| Tool (write) | Parameters |
|---|---|
| `create_jira_issues` | `drafts`, `scope=None`: parse, reconcile scope (abort the whole batch on a mismatch), resolve assignee and sprint (an unresolvable sprint creates nothing), bulk create |
| `create_jira_issue` | flat fields: `summary`, `description`, `issue_type="Task"`, `story_points`, `labels`, `components`, `acceptance_criteria` |
| `assign_jira_issue` | `issue_key`, `assignee` (a name, email, account id or `"unassigned"`) |
| `update_jira_issue` | `issue_key`, then optional `summary`, `description`, `acceptance_criteria`, `story_points`, `labels`, `components`, `priority`, `parent_key`, `issue_type` |
| `add_jira_comment` | `issue_keys`, `comment` |
| `transition_jira_issues` | `issue_keys`, `status` |
| `move_jira_issues_to_sprint` | `issue_keys`, `sprint` |
| `remove_jira_issues_from_sprint` | `issue_keys` |

Each tool calls `get_client()`, catches the client's errors, and returns rendered
text. Every write's docstring says, above `Args:`, that it WRITES and is only
called when the user asked.

**Your tests (`➕ tests/test_jira_tools.py`) must cover:**

| Area | Cases |
|---|---|
| package | `__all__` equals the package's public names that aren't submodules (import `ModuleType` to tell them apart); each tool's parameters equal the tables above (read them from `tool.tool_spec["inputSchema"]["json"]["properties"]`) |
| JqlBuilder | clauses and order by; status-category negation; quotes escaped; empty query refused; sprint takes an int; `keys` matches an exact set and skips blanks |
| completion | a custom done status and "Won't Do" count as done; name fallback without a category; a done issue is never blocked; a flagged one is |
| buckets and metrics | buckets are exclusive and points sum to the total; completion and LOW risk; HIGH when little is done; empty sprint; sprint identity and truncation carried; the truncation warning reaches the report |
| stuck work | days from the latest status change; non-status changes ignored; never transitioned → created; no changelog → 0; bad or future timestamps don't crash or go negative; in-flight work past the threshold is stuck, inside it isn't; to-do work never is; the threshold is overridable; an explicit flag wins |
| normalise | status category, flagged field, missing optional fields |
| search | pages until the end; reports truncation at the cap; never asks for more than 100; adds a default order by, keeps the caller's; the sprint search asks for the changelog, a plain search doesn't |
| sprints | visible number and full name resolve; unknown sprint lists what exists; active wins across boards; the board walk is cached; sprint issues use JQL with the internal id |
| create fields | criteria reach the payload; description is ADF; unset fields omitted; fields mapped to Jira shapes; project key default and override; zero points sent; `source_issue` reaches the description only when set; the sprint field is the configured one |
| drafts | bad drafts reported by position, good ones kept; unknown issue type rejected; each Definition of Ready rule; a missing estimate is allowed |
| bulk create | one result per draft; partial failure mapped to the right draft; chunks of 50; a missing result reported; all rejected still names the field; no error array → raise |
| people | single candidate; exact name beats partial; exact email; same name twice, ambiguous partial, only inactive, none → refuse; the candidate list is readable; assign puts the id, unassign sends null; the search is project-scoped; a named assignee maps to the field, an unresolved name alone isn't sent |
| edits | only supplied fields; same mapping as create; criteria with a description, refused without; sprint refused; nothing to change → error |
| moves, comments | agile endpoints; chunks of 50; a rejected chunk reported per key; field errors surfaced; backlog endpoint; ADF comment per issue; one failed comment doesn't condemn the rest |
| rendering | owner in a search line, "unassigned" when none; blockers carry their owner; detail shows description and recent comments, says how many were withheld, truncates out loud; a partly failed batch never reads as done; the verb drives headline and failure note |
| transitions | destination first, then name; case and padding ignored; no closest guess; several routes reported; the looked-up id is posted; a lookup per issue; unavailable lists the options; "already in"; one failure doesn't stop the rest |
| scope | matching sets reconcile; out-of-scope draft and undrafted key both reported; case and spaces ignored; duplicate sources flagged; no source reported; no scope → no check; a mismatch aborts the create, a match proceeds; a draft's sprint number resolves to the internal id, an explicit id is left alone |

**Checkpoint D2:** your Jira tests pass. Break it: make `pick_assignee` take the
first candidate, and watch the ambiguity tests fail.

## D3 — Confluence (½–1 day)

Same site and token as Jira, and pages are the same ADF, so `adf.to_text` reads
them. Three facts about real pages shaped `➕ pmagent/tools/confluence_tools.py`:
pages are **too big to read whole** (one data-model page renders to about 40,000
characters), their structure is **headings**, and their real content is often
**screenshots** (the column list of a table is an image).

- `page_id_from_url(value)` accepts a bare id, a `/pages/<id>/Title` URL and the
  legacy `viewpage.action?pageId=` form. It refuses a `/x/` tiny link by name
  instead of guessing, and says what a good reference looks like.
  `section_from_url` takes the `#anchor` as the section to read.
- `split_sections(doc)` cuts the ADF at headings into `{level, title, nodes}`.
  Content before the first heading becomes an untitled section.
  `find_section(sections, name)` matches a heading ignoring case and punctuation,
  as an anchor does, but otherwise refuses a near miss; duplicates come back as
  ambiguous. `section_and_children` includes the deeper headings under a section,
  up to the next peer. `media_ids(nodes)` finds image ids at any depth, in order,
  without duplicates.
- `read_confluence_page(page, section="")`: without a section, a page of up to
  6,000 characters (of `to_text` of the whole page) is returned whole (with its
  images) and a larger one as its **outline** (each heading with the images in its
  own section), asking which section to read.
  A read returns a Strands `ToolResult` dict, `{"status": "success", "content":
  [...]}`, mixing `{"text": …}` and `{"image": {"format": "png", "source":
  {"bytes": …}}}` blocks: the text (clipped, loudly, at 12,000 characters), then
  the images, at most 4 and each under 4,000,000 bytes, each preceded by a text
  label. That is how a tool returns images in Strands (lesson 2). Every skipped
  image becomes a note: over the cap, too big, not an image, an SVG, or missing
  from the attachments. The text says whether the detail is in the images. A
  failure (an unknown page, an ambiguous or missing section, an HTTP error) is a
  `success` result whose text says what went wrong and what to ask, because the
  lane needs an answer to relay, not a traceback.
- `ConfluenceClient` (built by `get_client()`, attributes `_base` =
  `CONFLUENCE_BASE_URL` and `_session`) talks to: `GET /api/v2/pages/{id}` with
  `body-format=atlas_doc_format` (the ADF is a JSON *string* in
  `body.atlas_doc_format.value`); `GET /api/v2/pages/{id}/attachments`, following
  `_links.next`; each attachment's `_links.download` (or `downloadLink`); and
  `GET /rest/api/search` with `cql` and `limit` (a hit's `content.id`, `title` and
  `url`). An ADF media node's `attrs.id` is the attachment's `fileId`, and an
  image's `format` comes from its `mediaType`. It fetches pages with
  `body-format=atlas_doc_format`, follows the attachments
  cursor to the end, downloads attachments, and searches with CQL, escaping the
  query (`space=""` searches every space). An HTTP error carries its status and
  body. A legacy-editor page has no ADF, and it says so rather than reading as
  empty.
- `search_confluence(query, space="", limit=10)` defaults the space to
  `CONFLUENCE_SPACE_KEY`, treats `"*"` as every space, and renders one line per
  hit with the link Confluence returned and the page id to read next; no hits
  says so. Both tools are reads.

**Your tests (`➕ tests/test_confluence_tools.py`) must cover:** every bullet
above as at least one case: each URL form and the refused tiny link; anchors;
splitting, the untitled first section, children and peers; heading matching,
near misses and duplicates; media ids at depth, deduplicated; the text-only and
images notes; labelled image blocks; the unsupported-format note; the outline;
clipping announced and short text untouched; ADF parsed and the legacy page; HTTP
errors; the attachments cursor; search links, empty results, space scoping and
escaping; the image cap, an oversized image, a non-image attachment, a missing
one; text-then-images order; a missing section listing the real ones; a failure as
an answer; and that calling the decorated tool returns the dict unchanged.

**Checkpoint D3:** your Confluence tests pass. Break it: drop the image cap and
watch its test fail.

## D4 — The FY budget converter (1–1½ days)

Finance's converter turns fiscal-year budget workbooks into the daily CSV that a
Snowflake view ingests: one row per entity per calendar day. It is pandas and
openpyxl with no agent anywhere, and it is the purest example of rule 1.
**Every figure is computed and then validated against its source** before a file
is called good.

```bash
mkdir -p pmagent/tools/fy_budget
```

### The two input layouts

Real budget files are never shared, so you build synthetic ones with openpyxl in
a test helper, `➕ tests/fy_synthetic.py`. Compute every amount in Python from
its position and write the value (not an Excel formula, which openpyxl reads back
as empty): for example `1000 + 97 * (sum(map(ord, code + part)) % 50) + 13 *
month_idx`, GP 30% of that. Then a test can compute what any output must sum to.
Example rows, as Python lists: core `["Private", "Net sales"]` then
`["PBS", v1, …, v12]`; Customer view `[None, "AMC", ns×12, ns_total, gp×12,
gp_total]` and `["Group GRA", "GRA", …]` (name in column A, code in B); Plant_View
`[None, "Plant 1201", 101201, ns×12, gp×12]`; FY27 Plant View
`["Core", "Plant 1201", "NA", 101201, ns×12, gp×12]`; a Cat View
`["FOS", ns×12, gp×12]`, with the `Private Label`, `Exclude Private Label` and
`Total` markers in the same `MG1` column.
Use the codes the converter requires (banners `AMC` and `DDS`; MG1 codes from
`FOS`, `PBS`, `PRV`, `OTC`, `MED`) and made-up customer groups and plants.

- **Legacy (two files, e.g. `fy_start=202502`).**
  - `core.xlsx`, sheet `Total by month`: in the first rows, a row whose columns
    B–M hold twelve month abbreviations. Then, for each section (`Total`,
    `Private`, `Non Private`) and metric (`Net sales`, `GP`): a label row
    `[section, metric]`, followed by one row per MG1 code (`[code, 12 values]`,
    at most six rows), then a blank row.
  - `customer_and_plant.xlsx`, sheet `Customer view`: a header row with 13 `Net
    sales` cells and 13 `GP` cells (the 13th of each is the FY total). Banner rows
    have no name and a code of `AMC` or `DDS`. A row whose code cell is `Group
    code` starts the customer groups (`[name, code, values…]`); groups coded
    `Total` or `NA` are skipped. Sheet `Plant_View`: a header row with 12 `Net
    sales` and 12 `GP` cells; a row starting `Core`, then plant rows whose third
    cell is a numeric cost centre (keyed by its last 4 digits); a row whose third
    cell isn't a number ends the block; then the same for `CW` (some plants only).
  - Optional daily-weight file: a table with a date column (`date`,
    `CalYearMthDay`, `YYYYMMDD`, `Cal Year Mth Day`) and a weight column (`prec`,
    `weight`, `Daily Weight`, `Daily Rate`, `pct`), as `.xlsx`, `.csv` or `.tsv`.
- **FY27 (one workbook, `fy_start=202607`)** with seven tabs, found by name
  ignoring case and extra spaces: `Customer View`, `Plant View`, `Total Cat View`,
  `Core Cat View`, `CW Cat View`, `FY27 Daily Allocation_Core`, `FY27 Daily
  Allocation_CW`. In the first four kinds, row 1 holds real dates (the first of
  each month) above row 2's twelve `Net Sales` and twelve `GP` labels.
  - Customer View: an unlabelled code column holding `AMC` and `DDS` from row 3,
    a blank row, then the customer groups (`Total`, `NA`, `AMC`, `DDS` skipped).
  - Plant View: row 2 has a `CC` header; the component (`Core` / `CW`) sits three
    columns to its left; plant rows carry a numeric CC.
  - The three Cat Views: row 2 has an `MG1` header. A first `Total` row (the
    category total), then a `Private Label` section of MG1 rows ended by `Total`,
    then an `Exclude Private Label` section ended by `Total`. CW has no private
    label amounts, and Total = Core + CW.
  - Daily allocation tabs: a `Date` header and a `Daily Sales %` column, one row
    per day of the fiscal year; each month's weights sum to 1.

### The code

`➕ pmagent/tools/fy_budget/convert_budget.py`, the legacy layout:

- `fy_months(fy_start)` gives the twelve `YYYYMM` values from a start month, and
  `days_of(ym)` gives the days of a month as `YYYYMMDD` (leap years included).
- Readers locate most of their data by **content**: the `Net sales` / `GP`
  column blocks, the `Group code` marker, the `Core` and `CW` markers. The core
  sheet is the exception, with its fixed columns B–M. The legacy converter does
  **not** check `fy_start` against the month abbreviations: that value comes from
  a human (D5), and the tool says so. They return long-format
  frames (`code, month_idx, ns, gp`), so the builders never see a layout. Plant
  amounts are Core + CW per cost centre.
- Five cuts, each stamped with Division 2, Channel 1, Sales Org 1010 and `RQF =
  Budget`:

  | Type | Partition | Source | Stamped |
  |---|---|---|---|
  | RQF | Banner State | the AMC and DDS banner rows | MG1 `FOS`, banner code |
  | RQF | Customer Group State | customer groups | MG1 `FOS`, group code |
  | RQF | Customer Group State | plants (Core + CW) | MG1 `FOS`, group `PLANT`, plant code |
  | RQF INCLD CWH | Customer Group State | core `Private` MG1 rows | private label `Yes` |
  | RQF INCLD CWH | MG1 | core `Non Private` MG1 rows | private label `No` |

- `_expand_to_days(monthly, daily_rate, months)` spreads a monthly amount over
  its days: evenly (`daily_rate=None`), or by the daily-weight table normalised
  per month. A month whose weights sum to zero or less raises `RuntimeError("Daily-rate
  weights sum to zero or less for month <ym>; cannot distribute monthly values.")`,
  and a weight file that misses any day of the fiscal year raises one containing
  "does not cover the fiscal year". FY27 calls the same function through the
  module (`convert_budget._expand_to_days`), so one monkeypatch reaches both.
- `validate` checks that every (entity, month) daily sum equals the monthly source
  within 0.01, **before** rounding; each failure names the cut, entity and month
  (`"Banner AMC 202601: Budget … != …"`). The CSV is rounded to 2 dp afterwards.
  Two schema guards fail loudly on any column drift ("Internal output schema
  mismatch", "Published output schema mismatch").
- The published contract, `OUTPUT_COLS`, in this order: `Type`, `Partition`, `Cal
  Year Mth Day`, `Division Code`, `Distr Channel Code`, `Order Type`, `Plant
  Code`, `MG1 Code`, `Product Hierarchy Level 5`, `Sales Organisation`, `Customer
  Group Code`, `Private Label Flag`, `Banner Group Code`, `State`, `Budget`, `GP`,
  `RQF`, `Comments`, `Division Key`, `Dist Channel Key`, `Div_Dist_Key`,
  `MG1_PL_SO_DC_Key`, `Ban_State_Key`, `Group_State_Key`. Blank Plant, Banner and
  State become the literal `NA`; a blank Customer Group stays blank. State is
  always blank (so `NA`); `Order Type`, `Product Hierarchy Level 5` and
  `Comments` are always blank; Customer Group is blank on banner and MG1 rows.
  The keys are built **after** the `NA` substitution: `Division Key` and `Dist
  Channel Key` repeat their codes; `Div_Dist_Key` is Division + `0` + Channel
  (`201`); `MG1_PL_SO_DC_Key` is MG1 + Private Label + `0` + Channel + Sales Org
  (`FOSNo011010`); `Ban_State_Key` is Banner + State (`AMCNA`, and `NANA` on a row
  with no banner); `Group_State_Key` is Customer Group + State (`GRANA`,
  `PLANTNA`), or blank when there is no group. Every RQF row has Private Label
  `No`.
- `run(core_path, custplant_path, fy_start, out_path, dailyrate_path=None)` writes
  the CSV, then a `<name>.metadata.json` sidecar with `generated_at_utc`,
  `output_csv`, `inputs` (core, custplant, dailyrate or null), `fiscal_year_start`,
  `fiscal_months`, `total_rows`, `rows_by_partition` (`"<Type> | <Partition>"` →
  count), `daily_distribution` (`"applied"` or `"even split (no daily file)"`),
  `output_schema`, `schema_validation`, `internal_validation` (`"passed"`, or
  `"<N> failures"`) and `validation_failures`. A legacy run that fails validation
  still writes both files and **reports** the failures.

`➕ pmagent/tools/fy_budget/fy27_merged.py`: `is_fy27_merged_workbook(path)` and
`run(path, fy_start, out_path)`. Every range is found from its labels.

- **Customer View uses the Core allocation, never a blend.** This is a
  business-confirmed rule; record it in the metadata's `source_layout` section
  (under `customer_daily_allocation`).
- Plant and non-private MG1 amounts are Core and CW components. Each is spread on
  its own allocation, then the two are summed. Private MG1 is Core only.
- Validation, every (entity, month) within 0.01: banners and customer groups
  against the Customer View, plants against Core + CW, private MG1 against the
  Total Cat View's `Private Label` section, non-private MG1 against its `Exclude
  Private Label` section. A failure **raises** ("FY27 internal validation failed:
  …"). So does a `fy_start` that doesn't match the headers ("month headers are
  …"), an allocation tab that misses a day, and a month whose weights don't sum to
  1 within 1e-9 ("daily weights for <ym> sum to …"). CW's private label rows are
  zeros, which is fine. `daily_distribution` is `"embedded Core/CW daily
  allocations"`, and the sidecar's `inputs` has the workbook as both `core` and
  `custplant`, with `dailyrate` null.

`➕ pmagent/tools/fy_budget/detect.py`: `detect_roles_with_warnings(paths)`
identifies each file **by its sheets and columns, never its filename**:
`fy27_merged`, `core` (a `Total by month` sheet), `custplant` (`Customer view` +
`Plant_View`) or `dailyrate` (date and weight columns). The required roles are
either `fy27_merged` alone (it wins if legacy files are also present) or `core` +
`custplant`. A duplicate is a warning ("Multiple files matched role 'core'; using
…"). A missing required role raises ("Could not identify required file(s): […]"),
naming the sheets it saw.
`detect_roles` returns just the roles.

`➕ pmagent/tools/fy_budget/pipeline.py`: `convert_fy_budget(file_paths, fy_start,
out_path)` detects the roles, runs the right converter, and returns `steps`,
`summary`, `warnings`, `validation_failures`, `out_path` and `metadata_path`.
`summary` holds `fiscal_year_start`, `fiscal_months`, `total_rows`,
`rows_by_partition`, `daily_distribution`, `warnings`, `metadata_path` and
`internal_validation`; D5's report reads exactly these. Add an `__init__.py`.

**Your tests (`➕ tests/test_fy_budget.py`, using `tests/fy_synthetic.py`) must cover:**

| Area | Cases |
|---|---|
| calendar | `fy_months` crosses the year end; February 2024 has 29 days |
| detection | roles found by content in any order; FY27 recognised; a duplicate is a warning; a missing role names what was seen; a weight table recognised by its columns |
| legacy run | one row per entity per day (and the partition counts); the date range; validation passes; the sidecar's fields |
| roll-up | for every cut, every month's Budget and GP equal your builder's formula (within 0.2 after rounding) |
| stamps and keys | the table's stamps; Division 2, Sales Org 1010, RQF = Budget; each derived key, including a blank `Group_State_Key` on private rows |
| contract | `OUTPUT_COLS` equals the list above, literally |
| daily | even split (equal days, amount / days); a full weight file applied and still tying back; a short file aborts; zero weights abort |
| validation fails | make `_expand_to_days` drop a day (monkeypatch it on the module): the legacy run reports failures naming entity and month, the sidecar says so; FY27 raises; a Total Cat View cell that disagrees with its parts raises; schema drift raises |
| FY27 | converts with embedded allocations; a Customer View day = amount × Core share; a plant day = Core × Core share + CW × CW share; a private MG1 day uses Core; months roll up to the Total Cat View; the `NA` placeholders; a wrong `fy_start` and bad weights refused |
| pipeline | the exact top-level and `summary` keys |

**Checkpoint D4:** your FY tests pass. Break it: make `validate` return `[]`, and
check that a test fails. If none does, your "validation fails" cases are missing.

## D5 — The finance tools (2–3 h)

`➕ pmagent/tools/finance_tools.py` puts the converter in front of the agent. It
does four things and **no arithmetic**: select input files, call
`convert_fy_budget`, check the result, and render it.

- `inspect_fy_budget_inputs(input_dir="", out_path="")` (read) lists the files in
  the input folder, skipping Excel's `~$` lock files and anything that isn't
  `.xlsx`/`.csv`/`.tsv`/`.txt`. It shows the role each file was matched to,
  whether it is FY27 or the legacy pair, and whether the output exists. It **never
  infers `fy_start`** for a legacy workbook (that value must come from a human,
  matching the first visible month), but states the only valid one for FY27.
  "Whether the output exists" is checked for `out_path` when given, or for FY27's
  default name; for a legacy pair without `out_path` it says the name depends on
  the `fy_start` still to come. A missing or empty folder is an answer, not an
  exception. The tool is free (not
  gated) so the model has no reason to guess.
- `create_fy_budget_csv(fy_start, input_dir="", out_path="", overwrite=False)`
  (write, gated) rejects a `fy_start` that isn't a plausible `YYYYMM` (a year
  from 2000 to 2100 and a month from 1 to 12) before
  touching any file (the message names both conventions: 202502 for a February
  start, 202607 for July), refuses to replace an existing CSV unless
  `overwrite=True`, runs the converter, and renders the result. A converter error
  is reported, not worked around. The default output is `fy_budget_<fy_start>.csv`
  in `FY_BUDGET_OUTPUT_DIR`.
- `render_run_report(result)`: **the first line is the verdict**:
  `FY budget conversion passed validation.` or `FY budget conversion FAILED
  validation — N failure(s).`, followed on a failure by a line saying the CSV must
  not be used, because it exists. Then the paths, `Fiscal year: 202607 (Jul 2026 -
  Jun 2027)`, rows, rows per partition, the steps, warnings (even on a pass), and
  the failures (all of them when few, clipped with a count when many).
- `read_fy_budget_run(csv_path)` (read) renders a CSV's sidecar (or explains the
  CSV ↔ `.metadata.json` pairing when there is none). Metadata with failures is
  never called clean; inputs that weren't supplied are omitted.
- `format_fy_start(202607)` renders `202607 (Jul 2026 - Jun 2027)` and never
  crashes on a bad value. Import the `fy_budget` package **lazily**, inside the
  functions: it pulls in pandas, and a lane that never touches finance shouldn't
  pay for that.
- `READ_TOOLS = [inspect_fy_budget_inputs, read_fy_budget_run]`,
  `WRITE_TOOLS = [create_fy_budget_csv]`.

**Your tests (`➕ tests/test_finance_tools.py`) must cover:** every bullet above as
at least one case, plus two on real runs of your synthetic workbooks: a clean FY27
run renders `passed` with its fiscal start, and a run whose expansion drops a day
renders `FAILED` first.

**Checkpoint D5:** your finance tests pass. Break it: make the report lead with the
paths instead of the verdict.

## D6 — The spreadsheet approval queue (2–3 h)

`➕ pmagent/tools/spreadsheet_tools.py` changes project-control spreadsheets
through Microsoft Graph, with a **second human gate inside the workbook**. The
agent can only *propose* a cell change, as a row on a `PM Agent Approvals` sheet.
A person sets that row to `Approved` in Excel, and only then can it be applied.

- `ApprovalWorkbook` is a `typing.Protocol` with `get_cell`, `set_cell`,
  `append_request`, `requests()` (returning a list of `(index, row)`) and
  `replace_request`.
  The workflow is written against it, and your tests pass a dict-backed fake.
- `propose_cell_update(workbook, sheet, cell, value)` records the target's
  **current** value next to the proposed one, as a row with `QUEUE_HEADERS`:
  RequestId (`scr_<hex>`), RequestedAt, Operation (`set_cell`), Target
  (`Sheet!A1`), ExpectedValue, ProposedValue, Status (`Pending`), ApprovedBy,
  AppliedAt, Result. It returns the request id.
- `apply_approved_requests(workbook)` applies only `set_cell` rows marked
  `Approved`, and returns the applied ids. If the cell changed since the
  proposal, it marks the row `Conflicted` ("Target changed after proposal") and
  writes nothing, so a stale approval never overwrites someone's edit. It splits
  `Sheet!A1` on the **last** `!`.
- `DeviceCodeAuth(tenant_id, client_id)` implements Microsoft's device-code
  sign-in, with a token cache in `~/.pmagent/microsoft-graph-token.json`. It
  reuses a token until two minutes before expiry, then refreshes it (keeping the
  refresh token if the response omits one); with no cache it runs the device-code
  flow.
- `DeviceCodeAuth` posts form data to
  `https://login.microsoftonline.com/<tenant>/oauth2/v2.0/devicecode` and
  `…/oauth2/v2.0/token`, with the scopes `https://graph.microsoft.com/Files.ReadWrite
  User.Read offline_access`.
- `GraphWorkbook(workbook_url)` needs `SPREADSHEET_TENANT_ID` and
  `SPREADSHEET_CLIENT_ID` (an error naming them otherwise). Its calls go to
  `https://graph.microsoft.com/v1.0`: `GET /shares/<token>/driveItem` (the drive
  and item ids); `POST /drives/<d>/items/<i>/workbook/createSession`
  (`{"persistChanges": true}`); then, under `…/workbook`,
  `worksheets/<sheet>/range(address='A1')` (GET a value, PATCH `{"values":
  [[v]]}`), `worksheets/add`, `tables/add` then `PATCH tables/<id>` with
  `{"name": "PMAgentApprovals"}` (adding a table can't name it), `tables/PMAgentApprovals/rows/add`,
  `tables/PMAgentApprovals/rows` and `rows/itemAt(index=<n>)/range`. `get_cell`
  returns a string (`""` for an empty cell), so a number and its text compare
  equal. It resolves a sharing
  URL (`u!` + unpadded base64url of the URL), opens a persistent workbook session,
  and sends every call with that session's `workbook-session-id` header. The
  queue is an Excel **table** (`PMAgentApprovals` on the `PM Agent Approvals`
  sheet), so rows go through the table's `rows/add` endpoint.
- An applied row gets Status `Applied`, AppliedAt, and Result `Applied`.
- The tools `prepare_spreadsheet_approval_queue(workbook_url)`,
  `propose_spreadsheet_cell_update(workbook_url, sheet, cell, value)` and
  `apply_approved_spreadsheet_updates(workbook_url)` are all **writes**, including the one
  that only creates the queue sheet: it changes someone's real workbook.
  `READ_TOOLS = []` is a deliberate statement.

**Your tests (`➕ tests/test_spreadsheet_tools.py`) must cover:** an approved
update applied; a pending one never touching the cell; the proposal's recorded
expected value; a conflict; non-`set_cell` rows ignored; a sheet name containing
`!`; the sharing token (decodes back to the URL, no padding); the missing-ids
error; a cached token reused; an expiring token refreshed with the refresh token
kept (monkeypatch `requests.post`); every Graph call carrying one session id and
the right range path (monkeypatch `requests.request`); all three tools classified
as writes.

**Checkpoint D6:** your spreadsheet tests pass. Break it: drop the conflict check.

## D7 — The company-knowledge seam (15 min)

`➕ pmagent/tools/company_knowledge.py`: `retrieve_company_context(topic,
max_chars=4000)` is a **reserved seam**. It is a plain function, not a `@tool`,
so this module has no `READ_TOOLS`/`WRITE_TOOLS`. The requirements workflow (R4)
calls it to ground a PRD. Today it returns the `.md` and `.txt` files in
`sample_data/company_docs/` at the project root (the folder is a module constant,
`_DOCS_DIR`, so tests can point it elsewhere), sorted by name, each under a
`### <file name with extension>` header, separated by a blank line, cut at
`max_chars`, or `""` when there are none. `topic` is unused today; it is there for
the real retriever. Replacing it
with a real retriever later changes this one function body and nothing else.
That is how you reserve a place for a feature without building it too early.

**Your tests (`➕ tests/test_company_knowledge.py`) must cover:** a missing and an
empty folder → `""`; `.md` and `.txt` files in name order with their headers, other
files ignored, and the cap.

## Checkpoint: the domain layer is rebuilt

```bash
uv run pytest tests -q
```

Every test you wrote passes, and each step's table is covered. Every tool the
agents will call now exists, is tested offline, and imports with no `.env`. Go on
to R2 in [Rebuild it yourself](rebuild.md), where Strands enters: the model layer,
and the tools that need a model.
