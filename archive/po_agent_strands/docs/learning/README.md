# Learning Strands Agents with this repo

This repo is PO_Agent — the LangGraph multi-agent PM assistant — rewritten on the
[Strands Agents SDK](https://strandsagents.com) (v1.57). Everything the app does is
real: it talks to Jira and Confluence, gates every write behind a human, writes
PRDs with a reflection loop. That makes it a good place to learn Strands, because
every concept below is answered twice: in a 30-line example you can run offline,
and in production code that has to survive real use.

**The course has three parts. The end goal is that you can build this project
yourself, and then add logging, persistence and memory to it.**

| Part | What you do | Where |
|------|-------------|-------|
| **1 · Understand** | read the finished app, one Strands concept per lesson | lessons 1–11 |
| **2 · Rebuild** | build all of it again from an empty folder, step by step: the domain layer (every tool the agents call), the agents, both frontends, an MCP server and the Docker image. Each step ends with a table of cases, and the tests you write for it are your checkpoint | [Rebuild it yourself](rebuild.md), [The domain layer](rebuild-domain.md) |
| **3 · Extend** | guided builds of what the project has *designed* but not built: a run log, persistence, long-term memory, evals, a Claude Code harness, and Jev. No solution is in the repo; each lesson gives the design, the files, the tests that define "done", and the traps | lessons 12–17 |

## Before you start

- **The code:** this page is generated from the `po_agent_strands` repository
  (`docs/learning/`). Get a copy from whoever shared the course with you; it is
  not published elsewhere. Run every command from the repository's root.
- **Prerequisites:** [uv](https://docs.astral.sh/uv/) and Python 3.12 or newer
  (uv fetches Python if needed). Then run `uv sync` once.
- **Offline is the default.** Every lesson's example, and the offline web demo
  (`uv run scripts/demo_web.py`, for lessons 10–11), need **no API key and no
  Jira**. They use a scripted model (lesson 9) and fake data.
- **Docker runs the examples and the demo without installing anything.**
  `docker build -t po-agent .` then `docker run --rm --init po-agent examples`
  runs every offline example except 15, and
  `docker run --rm --init -p 127.0.0.1:8000:8000 po-agent` is the offline web demo.
  The code in the image is read-only, so the exercises need a checkout (or a
  rebuild after each edit). The README's "Run with Docker" section lists every
  command.
- **Live runs are optional.** They need `cp .env.sample .env` and an LLM key, plus
  Jira credentials for the app itself. **If your `.env` points at a real Jira, use
  `uv run scripts/smoke.py` (CLI) or `uv run scripts/smoke.py --web` (web UI)**
  instead of `main.py` / `web.py`. Both launchers block every write below the app,
  so approving is safe. The course marks each live step.

## How to use this course

1. Read a lesson (10–15 minutes each).
2. Run its example. Example files are numbered by lesson (`06_interrupt_gate.py`
   is lesson 6); run it with `uv run docs/learning/examples/06_interrupt_gate.py`.
   Add `--live` to use the real model from `.env` (every example except 17,
   which has no model in it). The examples' tools are toys, so
   nothing live is ever written.
3. Open the repo code the lesson cites and read it with the lesson beside you.
4. Do the exercises. There are three kinds:
   - **code:** change the repo, then check with `uv run pytest -q`, and revert;
   - **run:** offline unless marked **live**;
   - **think:** design questions with no single command to check them.
5. In Parts 2 and 3 you work in **your own folder** (the Rebuild guide calls it
   `$MY`), and you write everything there yourself, including the scripted model
   your tests use (R2).

## Words this course uses

| Term | Meaning |
|------|---------|
| **PO Agent** | this project. The assistant it builds calls itself **PM Agent** in its UI. `PO_Agent` (with an underscore) is the original LangGraph repository it was rewritten from |
| **lane** | one specialist of the assistant: a Strands `Agent` with its own system prompt and tools (ticket, sprint, query, finance, spreadsheet, diagram), plus the requirements workflow. Lesson 7 |
| **route** | the label the router gives a message (`ticket`, `sprint`, …), which picks its lane. The CLI prints it as `[route: ticket]`; the web UI shows it as a badge |
| **gate / approval** | the check that pauses before any write until a human approves (lesson 6) |
| **Bedrock Converse shape** | the message format Strands uses everywhere, named after AWS Bedrock's Converse API: `{"role": …, "content": [blocks]}` (lesson 4) |
| **scripted model** | `tests/fakes.py::ScriptedModel`: a fake LLM that replays canned replies, so examples and tests run offline (lesson 9) |
| **LangGraph terms** | `StateGraph` (a graph of nodes), `ToolNode` (the node that runs tools) and `interrupt_before` (pause before a node). These appear only in the "how the original did it" sections |

Citations look like `pmagent/gate.py::ApprovalGate.check_batch` — a file and a
symbol, not a line number, so they survive edits. `tests/test_learning_docs.py`
fails if a cited file or symbol stops existing.

A path marked **➕**, like `➕ pmagent/runlog.py`, is a file **you create in your
own folder**, from the guide alone: the rebuild never asks you to look at, copy
or compare with this repository. Every path without ➕ exists in this repository,
for Part 1's explanations.

## Lessons

| # | Lesson | Strands concepts | Example |
|---|--------|------------------|---------|
| 1 | [The agent loop and models](01-agent-loop-and-models.md) | `Agent`, `Model`, providers, `AgentResult`, `callback_handler` | `01_hello_agent.py` |
| 2 | [Tools](02-tools.md) | `@tool`, docstring → spec, `ToolResult`, images | `02_tools.py` |
| 3 | [Structured output](03-structured-output.md) | `structured_output_model`, the hidden output tool | `03_structured_output.py` |
| 4 | [Conversation state](04-conversation-state.md) | `agent.messages`, message shape, conversation managers | `04_shared_history.py` |
| 5 | [Hooks](05-hooks.md) | `HookProvider`, lifecycle events, observing the loop | `05_hooks.py` |
| 6 | [Interrupts: human in the loop](06-interrupts.md) | `event.interrupt`, resume, `cancel`, vended `HumanInTheLoop` | `06_interrupt_gate.py` |
| 7 | [Multi-agent patterns](07-multi-agent.md) | router + lanes, agents-as-tools, Swarm, `GraphBuilder` | `04_shared_history.py`, `07_graph_reflection.py` |
| 8 | [MCP](08-mcp.md) | `MCPClient`, lifecycle, gating tools you don't own; writing a server you own (the sprint review case study) | `08_mcp_local.py`, `08_sprint_review_mcp.py` |
| 9 | [Testing agents offline](09-testing.md) | a scripted `Model`, testing the real loop | `tests/fakes.py` |
| 10 | [Serving an agent over HTTP](10-serving-over-http.md) | one `PMAssistant` behind a REST API: 202 + Location, RFC 9457, idempotency, security | `scripts/demo_web.py` (offline) |
| 11 | [Streaming the thinking chain](11-thinking-chain.md) | callback handler + hooks → events → SSE; what "reasoning" can honestly show | `11_trace_events.py` |

**Part 2 · Rebuild:** [Rebuild it yourself](rebuild.md) starts from `uv init` and an
empty folder, and **nothing is copied**, from the original project or from this
repository: you write the code, the tests, the prompts, the skills and the web page
from the guide. R1 is its own chapter, [The domain layer](rebuild-domain.md) (D0–D7):
configuration and contracts, prompts and skills, Markdown↔ADF, the Jira package,
Confluence, the FY budget converter, the finance tools, the spreadsheet approval
queue and the company-knowledge seam, about 5–6 days. R2–R8 build the agent layer,
the CLI, the web UI and a safe live run (another 3–4 days). R9 writes your own MCP
server (lesson 8's case study), and R10 packages everything as a Docker image. Each
step ends with a table of cases: your own tests must cover them and pass.

**Part 3 · Extend (guided builds).** Do them in your rebuild, in the order
**12 → 13 → 16 → 15 → 17 → 14**. The design they follow, reviewed three times,
is `docs/PLAN_HARNESS_MEMORY.md`.

| # | Lesson | Strands concepts | Example | You build |
|---|--------|------------------|---------|-----------|
| 12 | [Logging: a run log you can trust](12-run-log.md) | hooks as observers: `BeforeToolsEvent`, `AfterToolCallEvent`, `MessageAddedEvent` | `12_runlog_hook.py` | `➕ pmagent/runlog.py` |
| 13 | [Persistence: surviving a restart](13-persistence.md) | `take_snapshot` / `load_snapshot`, session managers, and why the shared list needs its own store | `13_snapshots.py` | `➕ pmagent/persistence.py` |
| 14 | [Long-term memory](14-long-term-memory.md) | `MemoryManager`, a custom `MemoryStore`, injection vs history | `14_memory_injection.py` | `➕ pmagent/memory_store.py`, a memory lane |
| 15 | [Evals: testing the agent's judgment](15-evals.md) | `strands-agents-evals`: `Case`, `Experiment`, custom `Evaluator`, `TaskOutput` | `15_eval_experiment.py` (needs `--with`, see the lesson) | `➕ evals/` |
| 16 | [The harness around you: Claude Code config](16-claude-code-harness.md) | (Claude Code, not Strands) hooks, permissions, skills, subagents | none | `➕ .claude/` |
| 17 | [A decision model beside your LLM (Jev)](17-jev.md) | a confidence-gated cascade, abstention, decision functions vs `Model` | `17_jev_cascade.py` | `➕ pmagent/jev.py` |

Also: [LangGraph → Strands concept map](LANGGRAPH_TO_STRANDS.md) — read it first if you
know the original codebase.

**The whole course as one web page:** `docs/learning/course.html`, generated from
these Markdown files by `scripts/build_course.py` (a test checks it is up to date).

## The app in one picture

```
main.py (CLI)            web.py → pmagent/web/ (FastAPI + SSE + the page)
   │  send(text) / resume(approved)      — the same two calls, from either frontend
   ▼
pmagent/assistant.py  PMAssistant ── one shared message list ──────────────┐
   │ 1. router.classify()  ── structured output → RouteDecision            │
   │ 2. pick lane                                                           │
   ├─► ticket / sprint / query / finance / spreadsheet / diagram lane       │
   │      = strands.Agent(system prompt, tools, hooks=[ApprovalGate, Echo]) │
   │        writes? → BeforeToolsEvent → interrupt → back to the CLI ──────┘
   └─► requirements = writer ↔ reviewer loop (two structured-output calls)

pmagent/tools/**   framework-free domain code (Jira, Confluence, FY budget, ...);
                   the rebuild's domain chapter (D0–D7) builds all of it
```

## What was deliberately *not* changed

The domain layer — Jira client, JQL builder, sprint metrics, ADF rendering, the FY
budget converter, all prompts and skills — is byte-for-byte the original
(`docs/evidence/verbatim_report.txt`). That is the first lesson of the port: **an
agent framework should be a thin layer.** If swapping LangGraph for Strands had
meant rewriting the Jira client, the design would have been wrong.

The port kept that code; the rebuild doesn't. In Part 2 you write the domain layer
yourself (D0–D7), before any agent exists, with its tests as the spec. When those
tests pass with no Strands agent anywhere, you have shown the same thing from the
other side: the tools stand on their own.
