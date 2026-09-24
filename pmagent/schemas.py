"""
Pydantic data contract
 agents should exchange *structured data*, not giant text blobs. Each of these models is either a tool's input
contract (`TicketDraft`) or the `structured_output_model` of a Strands agent call
(`RouteDecision`, `PRD`, `ReviewResult`, `DiagramBrief`)

"""

from typing import Literal
from pydantic import BaseModel, Field

class TicketDraft(BaseModel):
    """One Jira issue proposed by the Ticket Agent.

    This is the *contract* between the agent and Jira, not a display object:
    `create_jira_issues` validates incoming drafts against this model and maps
    it onto the Jira create payload in `pmagent/tools/jira/fields.py`. The agent
    shows the draft to the user for approval *before* anything is created.

    Every field here must actually reach Jira — a field that exists on the model
    but is dropped on the way out is worse than no field at all, because the
    agent will faithfully draft content that silently disappears.
    """
    summary: str = Field(description = "Concise issue title, written as an action")
    issue_type: Literal["Epic","Story","Bug","Task","Spike","Sub-task"] = Field(default="Task",description="Jira issue type")
    description: str = Field(description="Body in the team's house style: business context, scope, "
        "and any technical notes. Markdown is rendered to Jira's ADF format.")
    acceptance_criteria: list[str] = Field(
        default_factory=list,
        description="Testable, unambiguous acceptance criteria. Rendered into "
        "the Jira description under an 'Acceptance Criteria' heading.",
    )
    story_points: float | None = Field(
        default=None, description="Fibonacci effort estimate, or null."
    )
    labels: list[str] = Field(default_factory=list)
    components: list[str] = Field(default_factory=list)
    priority: str | None = Field(
        default=None, description="Jira priority name, e.g. 'High'. Null uses the project default."
    )
    parent_key: str | None = Field(
        default=None,
        description="Parent issue key. The Epic for a Story, or the parent "
        "Story for a Sub-task, e.g. 'CSCI-120'.",
    )
    assignee: str | None = Field(
        default=None,
        description="Display name or email of the assignee, e.g. 'Han Li'. "
        "Resolved to an account id at creation time.",
    )
    assignee_account_id: str | None = Field(
        default=None,
        description="Jira account id of the assignee. Only set this when you "
        "already have the id; otherwise use `assignee` and let it be resolved.",
    )
    source_issue: str | None = Field(
        default=None,
        description="The existing issue this ticket was derived from, e.g. "
        "'CSCI-1379'. Set it whenever a draft follows from a specific card — a "
        "batch generated per-issue is the main case. It is shown in the approval "
        "prompt and rendered into the Jira description as a Related line.",
    )
    sprint: int | str | None = Field(
        default=None,
        description="The sprint as a person names it — the visible number (31) "
        "or the full name ('Supply Chain Sprint 31'). Resolved to an internal "
        "sprint id at creation time. Use this, not sprint_id.",
    )
    sprint_id: int | None = Field(
        default=None,
        description="Jira INTERNAL sprint id, not the visible sprint number. "
        "Only set this when you already have the internal id; otherwise use "
        "`sprint` and let it be resolved.",
    )
    project_key: str | None = Field(
        default=None, description="Overrides JIRA_PROJECT_KEY for this one issue."
    )


# Requirements Agent schemas (Writer produces a PRD; Reviewer critiques it)
# These mirror the sections of the Atlassian "Product requirements" template.
# The Writer fills them in (LLM); a deterministic renderer turns them into the
# template markdown (no LLM). The Reviewer reasons over this *structure* to
# check that every source requirement was captured.

#REQUIRE BUSINESS PO REVIEW
class SuccessMetric(BaseModel):
    """One row of the PRD 'Success metrics' table."""
    goal: str = Field(description="The business goal, e.g. 'Reduce reporting errors'.")
    metric: str = Field(description="How it's measured, e.g. 'FX variance < 0.1% MoM'.")

class PRDRequirement(BaseModel):
    """One row of the PRD 'Requirements' table."""
    user_story: str = Field(
        description="Written as 'As a <role>, I want <capability>, so that <benefit>'."
    )
    importance: Literal["High", "Medium", "Low"] = "Medium"
    notes: str = ""
    jira_issue: str = Field(default="", description="Optional Jira key/link if one exists.")

class OpenQuestion(BaseModel):
    """One row of the Atlassian PRD 'Open questions' table."""
    question: str
    answer: str = ""

class PRD(BaseModel):
    """A structured Product Requirements Document (Atlassian template shape).

    The Writer agent produces this; `render_prd_markdown` formats it into the
    template. Keeping it typed is what lets the Reviewer do precise coverage
    checks instead of eyeballing prose.
    """
    title: str = Field(description="Document title / feature name.")
    objective: str = Field(description="A few sentences on what we're doing and why.")
    target_release: str = ""
    owner: str = ""
    stakeholders: list[str] = Field(default_factory=list)
    background: str = ""
    success_metrics: list[SuccessMetric] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    requirements: list[PRDRequirement] = Field(default_factory=list)
    user_interaction_design: str = Field(
        default="", description="Notes or links to UX/design, if any."
    )
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    out_of_scope: list[str] = Field(
        default_factory=list, description="Explicitly what we are NOT doing."
    )


class ReviewResult(BaseModel):
    """The Reviewer agent's verdict on a PRD draft.

    `missing_requirements` is the heart of "ensure all requirements are
    captured": the Reviewer lists any requirement implied by the source notes
    that the draft failed to reflect. While that list is non-empty (and we have
    iterations left), the draft goes back to the Writer.
    """
    approved: bool = Field(description="True only if the PRD captures everything and is well-formed.")
    missing_requirements: list[str] = Field(
        default_factory=list,
        description="Requirements present in the source notes but absent/garbled in the PRD.",
    )
    issues: list[str] = Field(
        default_factory=list,
        description="Quality problems: ambiguity, untestable metrics, vague stories.",
    )
    revision_notes: str = Field(
        default="", description="Concrete, actionable guidance for the Writer's next pass."
    )


class DiagramNode(BaseModel):
    """One node in a `DiagramBrief`."""
    id: str
    label: str


class DiagramEdge(BaseModel):
    """One edge in a `DiagramBrief`."""
    from_: str = Field(alias="from")
    to: str
    label: str = ""

    model_config = {"populate_by_name": True}


class DiagramBrief(BaseModel):
    """Structured output of the Diagram Agent's `draft_diagram_brief` tool.

    Mirrors `snowflake/procs/draft_diagram_brief.py`'s JSON shape exactly, so
    the two tracks stay interchangeable even though this one gets its
    structure via Strands structured output (`llm.structured`) instead of
    prompted JSON + manual parsing (Cortex there has no structured-output
    binding; Strands here does, so we use it — see CLAUDE.md's "LLM does judgment, plain Python
    does arithmetic/formatting" principle: `render_diagram_brief` is the
    deterministic formatting step, this model is just the judgment).
    """
    diagram_type: Literal["flowchart", "entity_relationship", "architecture", "sequence"]
    title: str
    description: str = Field(
        description="Plain-English description of every node and how they "
        "connect, precise enough to hand to a diagramming tool with no other "
        "context."
    )
    nodes: list[DiagramNode] = Field(default_factory=list)
    edges: list[DiagramEdge] = Field(default_factory=list)



class RouteDecision(BaseModel):
    """The Orchestrator's intent-classification output.

    We use the model's *structured output* feature to force the routing decision
    into one of three known buckets, instead of parsing free text. This is the
    deterministic 'router' the blueprint recommends keeping separate from the
    work itself.
    """
    route: Literal[
        "ticket", "sprint", "query", "requirements", "spreadsheet", "diagram", "finance"
    ] = Field(
        description=(
            "ticket = user wants to create/draft a Jira issue, OR is continuing a "
            "ticket workflow already in progress — approving/confirming drafts, "
            "asking for a draft to be changed, or asking to edit, assign, or move "
            "an existing issue between sprints; "
            "sprint = user asks about sprint health/progress/blockers/standup; "
            "requirements = user provides requirements/meeting notes and wants a "
            "PRD / requirements document written; "
            "spreadsheet = user wants to propose or apply an approved update to a "
            "project-control spreadsheet; "
            "diagram = user wants a diagram drawn or a Lucid document created; "
            "finance = user wants the FY budget Snowflake-ingest CSV built, checked "
            "or explained from Finance's fiscal-year budget workbooks — anything "
            "mentioning FY budget files, fy_start / fiscal year start, the daily "
            "budget feed, or the audit metadata for a generated budget CSV; "
            "query = any other read-only question (search issues, look something up, chit-chat)."
        )
    )
