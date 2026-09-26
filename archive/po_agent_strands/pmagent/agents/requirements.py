"""
Requirements Agent — a Writer ↔ Reviewer reflection loop.

Takes raw input (a requirement or meeting notes) and produces a finished PRD in
the Atlassian template.

        writer → reviewer ──approved?──► render
          ▲                  │ no (and iterations left)
          └──────────────────┘

Why this is genuinely an *agent*, not a script: the Reviewer's judgement at
runtime decides whether the Writer runs again. The number of passes, and what
the Writer fixes each time, depend on the content — none of it is hard-coded.

STRANDS CONCEPT — the "workflow" pattern. Writer and Reviewer are each a
stateless structured-output call (`llm.structured`: a fresh Strands Agent that
must return a `PRD` / `ReviewResult`). The loop between them is ordinary Python.
When control flow is fixed and simple, plain code is the clearest orchestrator;
reach for Strands' multi-agent `GraphBuilder` when you want the framework to own
the flow (docs/learning/07-multi-agent.md shows this same loop as a Graph).

LangGraph original: a compiled `StateGraph` subgraph with writer/reviewer/render
nodes and a conditional edge (`route_after_review`). The node functions and the
routing rule survive unchanged in spirit; only the driver changed.

Split of responsibilities (the recurring principle):
  - Writer / Reviewer  → LLM (writing and judging are language tasks).
  - render_prd_markdown → plain Python (formatting must be exact, never guessed).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel
from strands.models import Model

from pmagent.llm import structured
from pmagent.prompts import prompts
from pmagent.schemas import PRD, ReviewResult
from pmagent.skills import load_skill
from pmagent.tools.company_knowledge import retrieve_company_context


# The PRD skill text is injected into both prompts so the writing principles and
# the review checklist come from one source. Loaded once at import.
_PRD_SKILL = load_skill("prd")
_WRITER_PROMPT = prompts.inject_skill(prompts.requirements_writer_system_prompt, _PRD_SKILL)
_REVIEWER_PROMPT = prompts.inject_skill(prompts.requirements_reviewer_system_prompt, _PRD_SKILL)


class RequirementsState(BaseModel):
    """State threaded through the writer ↔ reviewer loop.

    Private to this workflow: nothing outside needs the iteration counter or the
    reviewer's working notes. Only the finished PRD and markdown leave it.
    """

    source_notes: str = ""
    company_context: str = ""
    prd: PRD | None = None
    review: ReviewResult | None = None
    iterations: int = 0
    max_iterations: int = 3
    markdown: str = ""


def writer_prompt(state: RequirementsState) -> str:
    """The Writer's input: the notes, house style, and — on a revision pass — the
    previous draft plus exactly what the Reviewer said was wrong with it."""
    parts = [f"Source notes:\n{state.source_notes}"]

    if state.company_context:
        parts.append(f"Company context (match this house style):\n{state.company_context}")

    # Revision pass: hand back the previous draft plus what was wrong with it.
    if state.prd and state.review:
        parts.append(f"Your previous draft:\n{state.prd.model_dump_json(indent=2)}")
        feedback = [f"Revision notes: {state.review.revision_notes}"]
        if state.review.missing_requirements:
            feedback.append(
                "Requirements you MISSED (each must appear in this pass):\n"
                + "\n".join(f"- {m}" for m in state.review.missing_requirements)
            )
        if state.review.issues:
            feedback.append(
                "Quality problems to fix:\n" + "\n".join(f"- {i}" for i in state.review.issues)
            )
        parts.append("\n\n".join(feedback))

    return "\n\n---\n\n".join(parts)


# `on_step(kind, data)` reports progress to whoever is watching — the web UI's
# thinking chain. Optional everywhere; the CLI passes nothing.
StepCallback = Callable[[str, dict], None]


def write(state: RequirementsState, model: Model | None = None, usage_sink=None) -> RequirementsState:
    """Draft the PRD, or revise it against the Reviewer's feedback."""
    prd = structured(PRD, writer_prompt(state), system_prompt=_WRITER_PROMPT, model=model,
                     usage_sink=usage_sink)
    return state.model_copy(update={"prd": prd, "iterations": state.iterations + 1})


def review(state: RequirementsState, model: Model | None = None, usage_sink=None) -> RequirementsState:
    """Critique the draft. Its verdict is what decides whether the loop runs again."""
    verdict = structured(
        ReviewResult,
        f"Source notes:\n{state.source_notes}\n\n---\n\n"
        f"PRD draft to review:\n{state.prd.model_dump_json(indent=2)}",
        system_prompt=_REVIEWER_PROMPT,
        model=model,
        usage_sink=usage_sink,
    )
    return state.model_copy(update={"review": verdict})


def route_after_review(state: RequirementsState) -> str:
    """Approved, or out of iterations? Render. Otherwise write another pass.

    The iteration cap is a safety net, not the control flow — the Reviewer's
    judgement is what normally ends the loop.
    """
    if state.review and state.review.approved:
        return "render"
    if state.iterations >= state.max_iterations:
        return "render"
    return "writer"


def run_requirements_loop(
    state: RequirementsState,
    model: Model | None = None,
    on_step: StepCallback | None = None,
    usage_sink=None,
) -> RequirementsState:
    """writer → reviewer → (render | writer again). The whole workflow, in plain Python."""
    step = on_step or (lambda kind, data: None)
    while True:
        step("writer.started", {"pass": state.iterations + 1})
        state = write(state, model, usage_sink)
        step("writer.finished", {
            "pass": state.iterations,
            "title": state.prd.title,
            "requirements": len(state.prd.requirements),
        })
        state = review(state, model, usage_sink)
        step("reviewer.finished", {
            "pass": state.iterations,
            "approved": state.review.approved,
            "missing_requirements": state.review.missing_requirements,
            "issues": state.review.issues,
        })
        if route_after_review(state) == "render":
            step("render", {"passes": state.iterations, "approved": state.review.approved})
            return state.model_copy(update={"markdown": render_prd_markdown(state.prd)})


# ---------------------------------------------------------------------------
# Deterministic rendering — no LLM. Mirrors skills/prd/template.md exactly.
# ---------------------------------------------------------------------------
def _escape_cell(value: str) -> str:
    """Make a value safe inside a Markdown table cell."""
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


def render_prd_markdown(prd: PRD | None) -> str:
    """Render a `PRD` into the Atlassian template.

    Plain Python on purpose: the Writer decides *what* the PRD says, this
    decides how it is laid out. Formatting must be exact, never guessed.
    """
    if prd is None:
        return ""

    stakeholders = ", ".join(prd.stakeholders) if prd.stakeholders else "—"

    lines = [
        f"# {prd.title}",
        "",
        "| | |",
        "|---|---|",
        f"| **Target release** | {_escape_cell(prd.target_release) or '—'} |",
        f"| **Document owner** | {_escape_cell(prd.owner) or '—'} |",
        f"| **Stakeholders** | {_escape_cell(stakeholders)} |",
        "",
        "## Objective",
        "",
        prd.objective or "—",
        "",
        "## Background",
        "",
        prd.background or "—",
        "",
        "## Success metrics",
        "",
    ]

    if prd.success_metrics:
        lines += ["| Goal | Metric |", "|------|--------|"]
        lines += [
            f"| {_escape_cell(m.goal)} | {_escape_cell(m.metric)} |" for m in prd.success_metrics
        ]
    else:
        lines.append("_None defined._")

    lines += ["", "## Assumptions", ""]
    lines += [f"- {a}" for a in prd.assumptions] or ["_None._"]

    lines += ["", "## Requirements", ""]
    if prd.requirements:
        lines += [
            "| # | User story | Importance | Jira | Notes |",
            "|---|------------|------------|------|-------|",
        ]
        lines += [
            f"| {n} | {_escape_cell(r.user_story)} | {_escape_cell(r.importance)} "
            f"| {_escape_cell(r.jira_issue) or '—'} | {_escape_cell(r.notes) or '—'} |"
            for n, r in enumerate(prd.requirements, start=1)
        ]
    else:
        lines.append("_None captured._")

    lines += [
        "",
        "## User interaction and design",
        "",
        prd.user_interaction_design or "—",
        "",
        "## Open questions",
        "",
    ]
    if prd.open_questions:
        lines += ["| Question | Answer |", "|----------|--------|"]
        lines += [
            f"| {_escape_cell(q.question)} | {_escape_cell(q.answer) or '_Open_'} |"
            for q in prd.open_questions
        ]
    else:
        lines.append("_None._")

    lines += ["", "## Out of scope", ""]
    lines += [f"- {item}" for item in prd.out_of_scope] or ["_Not specified._"]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point used by PMAssistant for the "requirements" route
# ---------------------------------------------------------------------------
@dataclass
class RequirementsResult:
    reply: str          # rendered markdown, plus a note if the reviewer wasn't satisfied
    prd: PRD | None
    review: ReviewResult | None
    iterations: int


def run_requirements(
    source_notes: str,
    model: Model | None = None,
    on_step: StepCallback | None = None,
    usage_sink=None,
) -> RequirementsResult:
    """Turn the user's notes into a rendered PRD.

    Returns the structured PRD alongside the markdown — the "structured data
    between agents" rule — so a caller can use the fields, not parse the text.
    """
    state = run_requirements_loop(
        RequirementsState(
            source_notes=source_notes,
            company_context=retrieve_company_context(source_notes[:200]),
        ),
        model,
        on_step,
        usage_sink,
    )

    note = ""
    if state.review and not state.review.approved:
        note = (
            f"\n\n> Note: the reviewer still had open items after "
            f"{state.iterations} passes: "
            + "; ".join(state.review.missing_requirements or state.review.issues or ["quality concerns"])
        )

    return RequirementsResult(
        reply=state.markdown + note,
        prd=state.prd,
        review=state.review,
        iterations=state.iterations,
    )
