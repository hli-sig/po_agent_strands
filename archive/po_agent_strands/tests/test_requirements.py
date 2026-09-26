"""Unit tests for the PRD writer <-> reviewer loop. No LLM, no network."""

from pmagent.agents.requirements import (
    RequirementsState,
    render_prd_markdown,
    route_after_review,
)
from pmagent.schemas import PRD, OpenQuestion, PRDRequirement, ReviewResult, SuccessMetric


def make_prd(**overrides) -> PRD:
    base = {
        "title": "FX Rate Backfill",
        "objective": "Backfill historical FX rates.",
        "requirements": [
            PRDRequirement(user_story="As an analyst, I want X, so that Y.", importance="High"),
        ],
    }
    return PRD(**{**base, **overrides})


# ---------------------------------------------------------------------------
# Loop control — the Reviewer's judgement, not a hardcoded count
# ---------------------------------------------------------------------------
def test_approved_draft_goes_straight_to_render():
    state = RequirementsState(review=ReviewResult(approved=True), iterations=1)
    assert route_after_review(state) == "render"


def test_unapproved_draft_loops_back_to_the_writer():
    state = RequirementsState(
        review=ReviewResult(approved=False, missing_requirements=["the FX bit"]),
        iterations=1,
    )
    assert route_after_review(state) == "writer"


def test_iteration_cap_stops_an_endless_loop():
    state = RequirementsState(
        review=ReviewResult(approved=False, missing_requirements=["still missing"]),
        iterations=3,
        max_iterations=3,
    )
    assert route_after_review(state) == "render"


def test_loop_count_is_not_hardcoded():
    # A stricter cap must be respected — the cap is data, not control flow.
    state = RequirementsState(review=ReviewResult(approved=False), iterations=1, max_iterations=1)
    assert route_after_review(state) == "render"


# ---------------------------------------------------------------------------
# Deterministic rendering
# ---------------------------------------------------------------------------
def test_render_follows_the_atlassian_template_sections():
    markdown = render_prd_markdown(make_prd())
    for heading in (
        "## Objective",
        "## Background",
        "## Success metrics",
        "## Assumptions",
        "## Requirements",
        "## User interaction and design",
        "## Open questions",
        "## Out of scope",
    ):
        assert heading in markdown


def test_requirements_are_numbered_in_order():
    prd = make_prd(requirements=[
        PRDRequirement(user_story="first"),
        PRDRequirement(user_story="second"),
    ])
    markdown = render_prd_markdown(prd)
    assert "| 1 | first |" in markdown
    assert "| 2 | second |" in markdown


def test_pipes_in_content_do_not_break_the_table():
    import re

    prd = make_prd(requirements=[PRDRequirement(user_story="a | b")])
    row = [ln for ln in render_prd_markdown(prd).splitlines() if "a \\| b" in ln]
    assert row, "pipe was not escaped"
    # Only unescaped pipes are cell delimiters: 5 columns => 6 delimiters.
    assert len(re.findall(r"(?<!\\)\|", row[0])) == 6


def test_newlines_in_content_do_not_break_the_table():
    prd = make_prd(requirements=[PRDRequirement(user_story="line one\nline two")])
    markdown = render_prd_markdown(prd)
    assert "| 1 | line one line two |" in markdown


def test_empty_sections_render_placeholders_not_broken_tables():
    markdown = render_prd_markdown(make_prd(requirements=[], success_metrics=[]))
    assert "_None captured._" in markdown
    assert "_None defined._" in markdown


def test_optional_tables_render_when_populated():
    prd = make_prd(
        success_metrics=[SuccessMetric(goal="Fewer errors", metric="variance < 0.1%")],
        open_questions=[OpenQuestion(question="Which source?")],
        out_of_scope=["Real-time rates"],
        assumptions=["RBA publishes monthly"],
    )
    markdown = render_prd_markdown(prd)
    assert "| Fewer errors | variance < 0.1% |" in markdown
    assert "| Which source? | _Open_ |" in markdown
    assert "- Real-time rates" in markdown
    assert "- RBA publishes monthly" in markdown


def test_render_of_nothing_is_empty_not_a_crash():
    assert render_prd_markdown(None) == ""


# ---------------------------------------------------------------------------
# The loop itself, driven by a scripted model (tests/fakes.py)
# ---------------------------------------------------------------------------
from pmagent.agents.requirements import run_requirements  # noqa: E402
from tests.fakes import ScriptedModel, structured  # noqa: E402

PRD_FIELDS = {"title": "FX feed", "objective": "Load daily FX rates."}


def _user_prompt(request: dict) -> str:
    return request["messages"][0]["content"][0]["text"]


def test_an_approved_first_draft_is_rendered_after_one_pass():
    model = ScriptedModel([
        [structured(PRD, **PRD_FIELDS)],
        [structured(ReviewResult, approved=True)],
    ])
    result = run_requirements("We need daily FX rates.", model=model)

    assert result.iterations == 1
    assert result.reply.startswith("# FX feed")
    assert "Note:" not in result.reply
    assert model.remaining == 0


def test_the_reviewer_s_missing_requirements_drive_a_revision_pass():
    model = ScriptedModel([
        [structured(PRD, **PRD_FIELDS)],
        [structured(ReviewResult, approved=False, missing_requirements=["Rates by 7am"],
                    revision_notes="Add the SLA.")],
        [structured(PRD, **PRD_FIELDS)],
        [structured(ReviewResult, approved=True)],
    ])
    result = run_requirements("We need daily FX rates by 7am.", model=model)

    assert result.iterations == 2
    second_writer_prompt = _user_prompt(model.requests[2])
    assert "Your previous draft" in second_writer_prompt
    assert "Requirements you MISSED" in second_writer_prompt
    assert "- Rates by 7am" in second_writer_prompt
    assert "Revision notes: Add the SLA." in second_writer_prompt


def test_the_iteration_cap_ends_the_loop_and_the_reply_says_so():
    never_happy = [structured(ReviewResult, approved=False, missing_requirements=["X"])]
    model = ScriptedModel([[structured(PRD, **PRD_FIELDS)], never_happy] * 3)
    result = run_requirements("notes", model=model)

    assert result.iterations == 3
    assert "reviewer still had open items after 3 passes: X" in result.reply
    assert model.remaining == 0


def test_writer_and_reviewer_get_their_own_system_prompts():
    model = ScriptedModel([
        [structured(PRD, **PRD_FIELDS)],
        [structured(ReviewResult, approved=True)],
    ])
    run_requirements("notes", model=model)
    writer, reviewer = model.requests
    assert writer["system_prompt"] != reviewer["system_prompt"]
    assert writer["tools"] == ["PRD"] and reviewer["tools"] == ["ReviewResult"]
