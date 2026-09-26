"""Jira field mapping: which fields we read, and how a draft becomes a payload.

Pure — no network, no client, no config beyond `env`. This is the single place a
`TicketDraft`-shaped dict turns into something Jira accepts, so a field cannot
reach Jira through one path and be silently dropped by another.
"""

from __future__ import annotations

from typing import Any

from pmagent import env
from pmagent.tools.adf import render_description


def get_story_points_field() -> str:
    """Return the Jira custom field id used for story points.

    For your Jira instance, "Story point estimate" is customfield_10052.
    You can override it in .env:

        JIRA_STORY_POINTS_FIELD=customfield_10052
    """
    return env.JIRA_STORY_POINTS_FIELD


def get_flagged_field() -> str:
    """Return the Jira custom field id used for the "Flagged" impediment marker."""
    return env.JIRA_FLAGGED_FIELD


def get_sprint_field() -> str:
    """Return the Jira custom field id used to place an issue in a sprint."""
    return env.JIRA_SPRINT_FIELD


def build_issue_fields(draft: dict) -> dict:
    """Map a `TicketDraft`-shaped dict onto a Jira create-issue `fields` object.

    One mapping, used by both single and bulk creation, so a field can never
    reach Jira through one path and be dropped by the other. Optional fields are
    omitted entirely rather than sent as null — Jira rejects nulls for several
    of them.
    """
    fields: dict[str, Any] = {
        "project": {"key": draft.get("project_key") or env.JIRA_PROJECT_KEY},
        "summary": draft["summary"],
        "issuetype": {"name": draft.get("issue_type", "Task")},
        "description": render_description(
            draft.get("description", ""),
            draft.get("acceptance_criteria") or [],
            draft.get("source_issue"),
        ),
    }

    if draft.get("labels"):
        fields["labels"] = draft["labels"]

    if draft.get("components"):
        fields["components"] = [{"name": c} for c in draft["components"]]

    if draft.get("story_points") is not None:
        fields[get_story_points_field()] = draft["story_points"]

    if draft.get("priority"):
        fields["priority"] = {"name": draft["priority"]}

    if draft.get("parent_key"):
        fields["parent"] = {"key": draft["parent_key"]}

    if draft.get("assignee_account_id"):
        fields["assignee"] = {"id": draft["assignee_account_id"]}

    if draft.get("sprint_id") is not None:
        # Sprint is a custom field on create, and takes the internal id.
        fields[get_sprint_field()] = draft["sprint_id"]

    return fields


def build_issue_update_fields(changes: dict) -> dict:
    """Map a partial ticket-shaped dict onto a Jira *edit* `fields` object.

    The create-side twin of `build_issue_fields`, with two differences that
    matter:

    - Only keys the caller actually supplied are emitted. An edit must never
      blank a field nobody mentioned, so absence means "leave alone" — which is
      why `None` is skipped rather than sent.
    - Sprint is deliberately not settable here. On a scrum board Jira refuses
      the sprint custom field on the edit screen; moving an *existing* issue
      between sprints only works through the Agile endpoint, which is what
      `JiraClient.move_issues_to_sprint` uses.
    """
    fields: dict[str, Any] = {}

    if changes.get("summary") is not None:
        fields["summary"] = changes["summary"]

    if changes.get("description") is not None:
        # Acceptance criteria are rendered *into* the description, so they can
        # only be updated alongside it — sending criteria alone would drop the
        # existing body.
        fields["description"] = render_description(
            changes["description"],
            changes.get("acceptance_criteria") or [],
            changes.get("source_issue"),
        )
    elif changes.get("acceptance_criteria"):
        raise ValueError(
            "acceptance_criteria can only be updated together with description — "
            "they are rendered into the same Jira field."
        )

    if changes.get("issue_type") is not None:
        fields["issuetype"] = {"name": changes["issue_type"]}

    if changes.get("story_points") is not None:
        fields[get_story_points_field()] = changes["story_points"]

    if changes.get("labels") is not None:
        fields["labels"] = changes["labels"]

    if changes.get("components") is not None:
        fields["components"] = [{"name": c} for c in changes["components"]]

    if changes.get("priority") is not None:
        fields["priority"] = {"name": changes["priority"]}

    if changes.get("parent_key") is not None:
        fields["parent"] = {"key": changes["parent_key"]}

    if changes.get("sprint_id") is not None:
        raise ValueError(
            "Sprint cannot be changed through an issue edit. Use "
            "move_jira_issues_to_sprint, which goes through the Agile API."
        )

    return fields


def jira_read_fields() -> list[str]:
    """Fields we ask Jira to return for read/search/sprint calls.

    Note `status` is enough to get `statusCategory` — Jira nests the category
    inside the status object, so we never request it separately. The category
    (`new` / `indeterminate` / `done`) is what all "is this complete?" logic
    keys off; status *names* are workflow-specific and unreliable.
    """
    return [
        "summary",
        "status",
        "issuetype",
        "labels",
        "components",
        get_story_points_field(),
        get_flagged_field(),
        "assignee",
        "priority",
        "parent",
        "created",
        "updated",
        "resolutiondate",
    ]
