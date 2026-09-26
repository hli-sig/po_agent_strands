"""Read-only Jira `@tool`s. Nothing in this module changes anything in Jira.

The split from `tools_write.py` is structural, not cosmetic: `gate.py` gates
writes by name (`WRITE_TOOL_NAMES`), and a tool that ends up in the wrong list
either bypasses the human approval gate or blocks a harmless read behind it.
`tests/test_agent_lanes.py` asserts this module's `READ_TOOLS` and
`tools_write.WRITE_TOOLS` agree with that set, so the file a tool lives in is
now the thing that decides its gate.

Read tools belong in *every* lane that could be asked for them — a read
withheld from the read-only query lane produces the same "I can't do that" as a
missing tool, with none of the safety justification.
"""

from __future__ import annotations

import json

from strands import tool

from pmagent import env
from pmagent.tools.jira.client import get_client
from pmagent.tools.jira.fields import build_issue_fields
from pmagent.tools.jira.jql import JqlBuilder
from pmagent.tools.jira.metrics import compute_sprint_metrics
from pmagent.tools.jira.render import (
    _DETAIL_MAX_ISSUES,
    format_candidates,
    format_issue_line,
    render_issue_detail,
    render_sprint_report,
)
from pmagent.tools.jira.validate import check_draft_standard, parse_drafts


@tool
def query_jira_issues(jql: str, max_results: int = 50) -> str:
    """Extract Jira issues with a JQL query. The general-purpose read tool.

    Use for any read-only question about issues: finding duplicates, gathering
    context, listing work by status/assignee/type/label, or answering "which
    tickets ...?".

    Useful JQL:
    - `project = CSCI AND statusCategory != Done` — open work
    - `project = CSCI AND sprint = 1234 AND statusCategory = Done` — completed
      in a sprint (note: `sprint` takes Jira's INTERNAL id, not the visible
      number — for a visible number use get_sprint_status_by_number instead)
    - `project = CSCI AND text ~ "DFIO"` — full-text
    - `assignee = currentUser() AND statusCategory != Done` — my open work

    Prefer `statusCategory` (New / "In Progress" / Done) over status names when
    asking whether work is complete — status names vary by workflow.

    Args:
        jql: A Jira Query Language string.
        max_results: Cap on issues returned (default 50). Raise it for
            whole-sprint or whole-backlog questions.
    """
    issues, truncated = get_client().search_issues_page(jql, max_results=max_results)
    if not issues:
        return "No matching issues found."

    lines = [format_issue_line(i) for i in issues]
    lines.append(f"\n{len(issues)} issue(s) returned.")
    if truncated:
        lines.append(
            f"WARNING: more issues matched than the {max_results} returned. "
            "This list is incomplete — re-run with a higher max_results before "
            "quoting any total."
        )
    return "\n".join(lines)


@tool
def read_jira_issues_by_key(keys: list[str]) -> str:
    """Look up an exact list of Jira issues by key. No fuzzy matching.

    Use this whenever the user names issue keys — "for CSCI-1712, CSCI-1714 and
    CSCI-1716 ..." — instead of searching for their summaries. A `text ~` or
    `summary ~` search returns whatever *looks* similar, which quietly pulls in
    neighbouring tickets; this returns the keys you asked for and nothing else.

    Any key that doesn't exist (or isn't visible to you) is reported by name
    rather than dropped, so the set you get back can always be reconciled
    against the set you asked for.

    Args:
        keys: Issue keys, e.g. ["CSCI-1712", "CSCI-1714"].
    """
    wanted = [k.strip().upper() for k in keys if k and k.strip()]
    if not wanted:
        return "No issue keys supplied; nothing to look up."

    issues, _ = get_client().search_issues_page(
        JqlBuilder().keys(*wanted).order_by("key ASC").build(),
        max_results=max(len(wanted), 50),
    )

    lines = [format_issue_line(i) for i in issues]
    lines.append(f"\n{len(issues)} of {len(wanted)} requested issue(s) returned.")

    # A key that came back missing is the whole reason this tool exists: it is
    # the difference between "that ticket isn't in scope" and "I never saw it".
    found = {(i.get("key") or "").upper() for i in issues}
    missing = [k for k in wanted if k not in found]
    if missing:
        lines.append(
            "NOT FOUND (do not treat these as excluded — they may be typos or "
            f"permission-restricted): {', '.join(missing)}"
        )

    return "\n".join(lines)


@tool
def search_jira_issues(jql: str) -> str:
    """Search Jira for existing issues.

    Thin alias for `query_jira_issues` with the default result cap. Prefer
    `query_jira_issues` when the answer depends on getting every match.

    Args:
        jql: A Jira Query Language string, e.g.
             'project = CSCI AND text ~ "DFIO"'.
    """
    return query_jira_issues(jql)


@tool
def get_sprint_status(sprint_id: int) -> str:
    """Get sprint progress by Jira INTERNAL sprint ID.

    Important:
    This takes Jira's hidden internal sprint ID, not the visible sprint number.
    Do NOT use this when the user says "sprint 31" or "Supply Chain Sprint 31" —
    use get_sprint_status_by_number for those.
    """
    payload = get_client().get_sprint_issues(sprint_id)
    metrics = compute_sprint_metrics(payload)
    return json.dumps(metrics, indent=2)


@tool
def get_sprint_status_by_number(sprint_number: int) -> str:
    """Get full status for a sprint by its user-visible number.

    Use this whenever the user names a sprint the way people say it:
    - "sprint 31"
    - "Supply Chain Sprint 31"
    - "what is the complete status for sprint CSCI 31"
    - "progress in sprint 31 by story point"

    Resolves the visible number to the sprint's real name
    (JIRA_SPRINT_NAME_TEMPLATE, e.g. "Supply Chain Sprint 31") and then to
    Jira's internal sprint ID, because visible numbers and internal IDs are
    different things. Returns completion by story points, per-bucket totals,
    blockers, risk level, and the full issue list.
    """
    client = get_client()
    sprint_id = client.resolve_sprint(env.JIRA_PROJECT_KEY, sprint_number)

    payload = client.get_sprint_issues(sprint_id)
    metrics = compute_sprint_metrics(payload)

    return render_sprint_report(metrics, payload.get("issues", []))


@tool
def find_jira_user(name_or_email: str, project_key: str | None = None) -> str:
    """Look up who can be assigned issues, by name or email. Read-only.

    Use this when you need someone's Jira account id, or to check how a name is
    spelled in Jira before assigning. Only returns users who actually have
    permission to be assigned work in the project.

    Args:
        name_or_email: Full or partial name, or an email address.
        project_key: Defaults to JIRA_PROJECT_KEY.
    """
    project = project_key or env.JIRA_PROJECT_KEY
    candidates = get_client().search_assignable_users(project, name_or_email)

    if not candidates:
        return f"No assignable user in {project} matches '{name_or_email}'."

    return f"{len(candidates)} match(es) in {project}:\n" + format_candidates(candidates)


@tool
def read_jira_issue_details(issue_keys: list[str], max_comments: int = 5) -> str:
    """Read the full context of specific issues: owner, description, and recent
    comments. Read-only.

    Use this when a question needs the *content* of tickets rather than a list
    of them — "why is this blocked?", "what's the actual blocker here?", "what
    did they decide on this one?". `query_jira_issues` deliberately returns one
    line per issue and cannot answer those; it has no description or comments.

    Descriptions and comment bodies are Atlassian rich text, rendered to plain
    text for you. Long ones are truncated and say so — never treat a truncated
    body as the whole story.

    Args:
        issue_keys: Issues to read, e.g. ["CSCI-1690", "CSCI-1691"]. Capped at
            25 per call; narrow the list rather than raising it.
        max_comments: Most recent comments per issue (default 5, 0 for none).
    """
    if not issue_keys:
        return "No issue keys supplied; nothing to read."

    requested = len(issue_keys)
    keys = issue_keys[:_DETAIL_MAX_ISSUES]
    client = get_client()

    blocks, failed = [], []
    for key in keys:
        try:
            blocks.append(render_issue_detail(client.get_issue_detail(key, max_comments)))
        except Exception as exc:  # noqa: BLE001 — one bad key must not lose the rest
            failed.append(f"- {key}: {type(exc).__name__}: {exc}")

    out = "\n\n".join(blocks)

    if failed:
        out += "\n\nCOULD NOT READ (" + str(len(failed)) + "):\n" + "\n".join(failed)
    if requested > len(keys):
        out += (
            f"\n\nWARNING: {requested} keys were requested but only the first "
            f"{len(keys)} were read. The rest are missing from this answer — "
            f"ask again for them before drawing conclusions about the full set."
        )
    return out


@tool
def list_jira_transitions(issue_key: str) -> str:
    """Show which workflow statuses an existing issue can move to right now.

    Read-only — changes nothing. Use it when a transition failed, or when you
    are unsure what the project's workflow calls a status ("In Review" vs
    "Review" vs "Peer Review"). What is available depends on the issue's
    *current* status, so answers differ per ticket.

    Args:
        issue_key: The issue to inspect, e.g. "CSCI-1805".
    """
    transitions = get_client().list_transitions(issue_key)
    if not transitions:
        return f"{issue_key} has no available transitions."

    # The transition's own name is only worth printing when it differs from
    # where it leads; in many workflows they're identical and repeating both
    # just pads the model's context.
    def line(t: dict) -> str:
        name, destination = t.get("name", ""), t.get("to_status", "")
        if name and name.strip().lower() != destination.strip().lower():
            return f"- {destination} (transition: {name})"
        return f"- {destination}"

    return f"{issue_key} can move to:\n" + "\n".join(line(t) for t in transitions)


@tool
def validate_ticket_drafts(drafts: list[dict]) -> str:
    """Check ticket drafts against the standard and against Jira, before creating.

    Call this after drafting and BEFORE asking the user to confirm, so problems
    surface while they can still be fixed. Checks each draft's shape, the
    Definition of Ready (summary length, acceptance criteria, parent for
    sub-tasks, Fibonacci estimates), and — against Jira's own createmeta — that
    the issue type exists in the project and that every field the project marks
    required is present.

    Args:
        drafts: List of ticket draft objects. Same shape as create_jira_issues.
    """
    parsed, errors = parse_drafts(drafts)

    report: list[str] = list(errors)

    for index, draft in enumerate(parsed, start=1):
        problems = check_draft_standard(draft)
        if problems:
            report.append(f"Draft {index} ({draft.summary}): " + "; ".join(problems))

    # Jira-side checks. Failure here shouldn't hide the local findings above.
    try:
        client = get_client()
        for index, draft in enumerate(parsed, start=1):
            project_key = draft.project_key or env.JIRA_PROJECT_KEY
            meta = client.get_create_metadata(project_key)

            if draft.issue_type not in meta:
                available = ", ".join(sorted(meta)) or "none visible"
                report.append(
                    f"Draft {index} ({draft.summary}): issue type "
                    f"'{draft.issue_type}' does not exist in {project_key}. "
                    f"Available: {available}"
                )
                continue

            supplied = build_issue_fields(draft.model_dump())
            missing = [
                field_id
                for field_id, spec in meta[draft.issue_type].items()
                if spec.get("required")
                and field_id not in supplied
                and not spec.get("hasDefaultValue")
            ]
            if missing:
                names = ", ".join(
                    meta[draft.issue_type][f].get("name", f) for f in missing
                )
                report.append(
                    f"Draft {index} ({draft.summary}): {project_key} requires "
                    f"these fields, which the draft doesn't set: {names}"
                )
    except Exception as exc:  # noqa: BLE001 — surfaced to the agent, not swallowed
        report.append(f"Could not reach Jira to verify issue types/fields: {exc}")

    if not report:
        return f"All {len(parsed)} draft(s) pass validation. Ready to create once the user confirms."

    return "Validation problems found — fix these before creating:\n" + "\n".join(
        f"- {line}" for line in report
    )


# Every read-only Jira tool. `tests/test_agent_lanes.py` checks none of these is
# in `WRITE_TOOL_NAMES` — a read behind the approval gate is a bug in the other
# direction, and just as invisible.
READ_TOOLS = [
    query_jira_issues,
    read_jira_issues_by_key,
    search_jira_issues,
    get_sprint_status,
    get_sprint_status_by_number,
    find_jira_user,
    read_jira_issue_details,
    list_jira_transitions,
    validate_ticket_drafts,
]

# Declared empty so every tool module answers the same two questions — the
# reconciliation in `tests/test_agent_lanes.py` reads both lists from each
# module and asserts no tool is left unclassified.
WRITE_TOOLS = []
