"""Jira `@tool`s that change something. Every one of these is human-gated.

Every lane agent carries `pmagent.gate.ApprovalGate`, a Strands hook that pauses
the agent (an interrupt) before any tool batch containing one of these runs, so
nothing here executes until a human approves. The gate is keyed by tool *name*
(`gate.WRITE_TOOL_NAMES`), cross-checked by the tests against `WRITE_TOOLS`
below — and anything the gate does not know as a read is gated too.

Adding a write tool is therefore three steps, and `tests/test_agent_lanes.py`
fails if you miss one:

1. Define it here.
2. Add it to `WRITE_TOOLS` at the bottom.
3. Add its name to `WRITE_TOOL_NAMES` in `pmagent/gate.py`.

Docstrings here carry the "this WRITES to Jira, only call it when the user asked
for it" instruction. That is the prompt-level guard; the interrupt is the real
one. Both exist because a prompt instruction is not a control.
"""

from __future__ import annotations

from strands import tool

from pmagent import env
from pmagent.tools.jira.client import get_client, resolve_assignee
from pmagent.tools.jira.render import _render_batch_result
from pmagent.tools.jira.validate import parse_drafts, reconcile_scope


@tool
def create_jira_issues(drafts: list[dict], scope: list[str] | None = None) -> str:
    """Create one or more Jira issues from confirmed drafts.

    ONLY call this AFTER the user has explicitly confirmed the drafts. Never
    call it to preview, and never to validate — use validate_ticket_drafts for
    that.

    Creates the whole batch in one bulk request (chunked at 50). Jira reports
    per-issue failures, so a partial success is reported honestly: you get one
    line per draft, either its new key or the reason it was rejected.

    Each draft accepts:
      summary (required), description (required), issue_type
      (Epic/Story/Bug/Task/Spike/Sub-task), acceptance_criteria (list of str —
      rendered as a bulleted "Acceptance Criteria" section), story_points,
      labels, components, priority, parent_key (Epic for a Story, Story for a
      Sub-task), assignee (display name or email), sprint (the visible sprint
      number or name, e.g. 31 — NOT the internal id, which is resolved for
      you), source_issue (the existing card this one derives from), and
      project_key (defaults to JIRA_PROJECT_KEY).

    Args:
        drafts: List of ticket draft objects, one per issue to create.
        scope: The issue keys the USER named, whenever they named any —
            ["CSCI-1712", "CSCI-1714", ...]. Pass it and every draft's
            source_issue is checked against it: nothing is created unless the
            two sets agree. This is how a batch is stopped from quietly
            covering a ticket nobody asked about. Omit it only when the user
            named no keys at all; do not omit it to avoid the check.
    """
    parsed, errors = parse_drafts(drafts)
    if errors:
        return "Nothing was created — these drafts are malformed:\n" + "\n".join(
            f"- {e}" for e in errors
        )
    if not parsed:
        return "No drafts supplied; nothing to create."

    # Intent vs action. This runs before anything touches Jira, and before the
    # assignee lookups below, because a scope mismatch means the whole batch is
    # wrong — there is no point resolving people for tickets that must not be
    # created. Aborting rather than dropping the odd draft is the same rule the
    # assignee check follows: a partly-right batch is worse than none.
    if scope:
        mismatches = reconcile_scope(parsed, scope)
        if mismatches:
            return (
                "Nothing was created. The drafts do not match the scope you named "
                f"({len(parsed)} draft(s) against {len(scope)} key(s)):\n"
                + "\n".join(f"- {m}" for m in mismatches)
                + "\n\nThis is not something to work around by re-running without "
                "scope. Either the draft set or the key list is wrong — ask which."
            )

    # Resolve any assignee names to account ids up front. An unresolvable name
    # aborts the whole batch: creating half the tickets unassigned and then
    # reporting a name problem leaves the user worse off than creating nothing.
    for index, draft in enumerate(parsed, start=1):
        project_key = draft.project_key or env.JIRA_PROJECT_KEY

        if draft.assignee and not draft.assignee_account_id:
            account_id, message = resolve_assignee(project_key, draft.assignee)
            if not account_id:
                return (
                    f"Nothing was created. Draft {index} ({draft.summary}) has an "
                    f"assignee that could not be resolved. {message}"
                )
            draft.assignee_account_id = account_id

        # "Sprint 31" is the number a person reads off the board; Jira's field
        # wants the internal id (31 -> 3435 here). Resolving it late, in the one
        # place that talks to Jira, keeps the agent from having to know the
        # difference — and a wrong guess lands the ticket in a real but wrong
        # sprint, which no error would ever surface.
        if draft.sprint is not None and draft.sprint_id is None:
            try:
                draft.sprint_id = get_client().resolve_sprint(project_key, draft.sprint)
            except ValueError as exc:
                return (
                    f"Nothing was created. Draft {index} ({draft.summary}) names a "
                    f"sprint that could not be resolved. {exc}"
                )

    results = get_client().create_issues([d.model_dump() for d in parsed])

    created = [r for r in results if r.get("key")]
    failed = [r for r in results if not r.get("key")]

    lines = [f"Created {len(created)} of {len(results)} issue(s)."]
    lines += [f"- {r['key']}: {r['summary']}" for r in created]

    if failed:
        lines.append("")
        lines.append(f"FAILED ({len(failed)}) — these were NOT created:")
        lines += [f"- {r['summary']}: {r['error']}" for r in failed]

    return "\n".join(lines)


@tool
def assign_jira_issue(issue_key: str, assignee: str) -> str:
    """Assign an existing Jira issue to a person.

    Accepts a display name ("Han Li"), an email, or an account id — you do not
    need to look the account id up first. If the name matches more than one
    person, nothing is assigned and you are told who the candidates are; ask
    the user which one they meant rather than guessing.

    Pass assignee="unassigned" to clear the current assignee.

    This WRITES to Jira. Only call it when the user has asked for this
    assignment.

    Args:
        issue_key: The issue to assign, e.g. "CSCI-1841".
        assignee: Display name, email, account id, or "unassigned".
    """
    client = get_client()

    if assignee.strip().lower() in ("unassigned", "none", "nobody"):
        client.assign_issue(issue_key, None)
        return f"{issue_key} is now unassigned."

    project_key = issue_key.split("-")[0] if "-" in issue_key else env.JIRA_PROJECT_KEY

    account_id, message = resolve_assignee(project_key, assignee)
    if not account_id:
        return f"{issue_key} was NOT assigned. {message}"

    client.assign_issue(issue_key, account_id)

    updated = client.get_issue(issue_key)
    return (
        f"Assigned {issue_key} to {updated.get('assignee') or message}.\n"
        f"  {updated.get('summary', '')}"
    )


@tool
def move_jira_issues_to_sprint(issue_keys: list[str], sprint: str) -> str:
    """Move existing Jira issues into a sprint.

    Use this for tickets that already exist — setting `sprint` on a draft
    only works at creation time. Handles one or many keys in a single call
    (chunked at 50).

    `sprint` accepts the user-visible sprint number ("31") or the full sprint
    name ("Supply Chain Sprint 31"); it is resolved to Jira's internal sprint
    id for you. Never pass an internal id you haven't seen in a tool result.

    This WRITES to Jira. Only call it when the user has asked for the move.

    Args:
        issue_keys: Issues to move, e.g. ["CSCI-1709", "CSCI-1710"].
        sprint: Visible sprint number or full sprint name.
    """
    if not issue_keys:
        return "No issue keys supplied; nothing was moved."

    first = issue_keys[0]
    project_key = first.split("-")[0] if "-" in first else env.JIRA_PROJECT_KEY
    client = get_client()

    try:
        sprint_id = client.resolve_sprint(project_key, sprint)
    except ValueError as exc:
        return f"Nothing was moved. {exc}"

    meta = client.get_sprint_meta(sprint_id)
    name = meta.get("name") or str(sprint)

    if (meta.get("state") or "").lower() == "closed":
        return (
            f"Nothing was moved. '{name}' (sprint id {sprint_id}) is closed — "
            f"Jira does not accept issues into a closed sprint."
        )

    results = client.move_issues_to_sprint(sprint_id, issue_keys)
    return _render_batch_result(results, "moved", f"{name} (sprint id {sprint_id})")


@tool
def remove_jira_issues_from_sprint(issue_keys: list[str]) -> str:
    """Pull existing Jira issues out of their sprint, back to the backlog.

    The counterpart to move_jira_issues_to_sprint — Jira has no "clear the
    sprint field" edit, so descoping goes through the backlog endpoint.

    This WRITES to Jira. Only call it when the user has asked for it.

    Args:
        issue_keys: Issues to descope, e.g. ["CSCI-1709"].
    """
    if not issue_keys:
        return "No issue keys supplied; nothing was moved."

    results = get_client().move_issues_to_backlog(issue_keys)
    return _render_batch_result(results, "moved", "the backlog")


@tool
def transition_jira_issues(issue_keys: list[str], status: str) -> str:
    """Move one or more existing Jira issues to a different workflow status.

    Use the destination status the user named ("In Review", "Done", "Blocked").
    You do not need a transition id — it is looked up per issue. If the status
    does not match, the result lists what that issue can actually move to; use
    `list_jira_transitions` to check before retrying rather than guessing at
    names.

    Note this is workflow state only. It does not assign, comment, or re-sprint
    — use the dedicated tools for those, and expect a status change to be one
    step of a multi-part request ("assign it to Alan and move it to In Review").

    This WRITES to Jira. Transitions can fire notifications and workflow
    post-functions, so only call it when the user has asked for the move.

    Args:
        issue_keys: Issues to move, e.g. ["CSCI-1805"].
        status: Destination status name, e.g. "In Review".
    """
    if not issue_keys:
        return "No issue keys supplied; nothing was transitioned."
    if not (status or "").strip():
        return "No target status supplied; nothing was transitioned."

    results = get_client().transition_issues(issue_keys, status)
    # Show the destination Jira actually used, not the name the user typed —
    # "In Review" landing in "In review" is worth seeing.
    return _render_batch_result(
        results,
        "moved",
        f"'{status}'",
        describe=lambda r: f"{r['key']} -> {r['to_status']}",
    )


@tool
def add_jira_comment(issue_keys: list[str], comment: str) -> str:
    """Post a comment on one or more existing Jira issues.

    One call handles a single ticket or a whole batch — the same comment text is
    posted on every key. Use it to leave a note for the team on a ticket: chase
    a stalled item, record a decision, or ask the assignee a question.

    The comment supports the same Markdown as a description (headings, bullets,
    numbered lists, **bold**, `inline code`, fenced code).

    This WRITES to Jira and the comment is visible to everyone on the project,
    so only call it when the user has asked for the comment, and show them the
    exact text first if you drafted it yourself. Comments cannot be edited or
    deleted through this agent.

    Args:
        issue_keys: Issues to comment on, e.g. ["CSCI-1709", "CSCI-1710"].
        comment: The comment body, as Markdown.
    """
    if not issue_keys:
        return "No issue keys supplied; nothing was commented on."
    if not (comment or "").strip():
        return "No comment text supplied; nothing was posted."

    results = get_client().add_comments(issue_keys, comment)
    return _render_batch_result(results, "commented on")


@tool
def update_jira_issue(
    issue_key: str,
    summary: str | None = None,
    description: str | None = None,
    acceptance_criteria: list[str] | None = None,
    story_points: int | None = None,
    labels: list[str] | None = None,
    components: list[str] | None = None,
    priority: str | None = None,
    parent_key: str | None = None,
    issue_type: str | None = None,
) -> str:
    """Edit fields on an existing Jira issue.

    Only the arguments you pass are changed; anything omitted is left exactly
    as it is. Labels and components are REPLACED wholesale, not merged — read
    the issue first and pass the full intended list.

    `acceptance_criteria` is rendered into the description, so it can only be
    updated together with `description`; pass both, or neither.

    Sprint is NOT editable here — use move_jira_issues_to_sprint. Assignee is
    not either — use assign_jira_issue, which resolves names.

    This WRITES to Jira. Only call it when the user has asked for the change,
    and show them what you intend to change first.

    Args:
        issue_key: The issue to edit, e.g. "CSCI-1709".
    """
    changes = {
        "summary": summary,
        "description": description,
        "acceptance_criteria": acceptance_criteria,
        "story_points": story_points,
        "labels": labels,
        "components": components,
        "priority": priority,
        "parent_key": parent_key,
        "issue_type": issue_type,
    }
    changes = {k: v for k, v in changes.items() if v is not None}

    client = get_client()
    try:
        client.update_issue(issue_key, changes)
    except ValueError as exc:
        return f"{issue_key} was NOT updated. {exc}"

    updated = client.get_issue(issue_key)
    changed = ", ".join(sorted(changes))
    return f"Updated {issue_key} ({changed}).\n  {updated.get('summary', '')}"


@tool
def create_jira_issue(
    summary: str,
    description: str,
    issue_type: str = "Task",
    story_points: int | None = None,
    labels: list[str] | None = None,
    components: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
) -> str:
    """Create a single Jira issue.

    ONLY call this AFTER the user has explicitly confirmed the drafted ticket.
    Never call it to preview. Prefer create_jira_issues, which handles one or
    many and supports the full draft shape.
    """
    draft = {
        "summary": summary,
        "description": description,
        "issue_type": issue_type,
        "story_points": story_points,
        "labels": labels or [],
        "components": components or [],
        "acceptance_criteria": acceptance_criteria or [],
        "project_key": env.JIRA_PROJECT_KEY,
    }
    result = get_client().create_issue(draft)
    return f"Created issue {result['key']}."


# Every Jira tool that mutates something. Kept in step with
# `gate.WRITE_TOOL_NAMES` by `tests/test_agent_lanes.py` — a tool defined here
# but missing from that set would execute with no approval prompt.
WRITE_TOOLS = [
    create_jira_issues,
    create_jira_issue,
    assign_jira_issue,
    update_jira_issue,
    add_jira_comment,
    transition_jira_issues,
    move_jira_issues_to_sprint,
    remove_jira_issues_from_sprint,
]

# Declared empty for the same reason `tools_read.py` declares an empty
# WRITE_TOOLS: every tool module answers both questions, so "I forgot" and
# "there are none" can't look alike.
READ_TOOLS = []
