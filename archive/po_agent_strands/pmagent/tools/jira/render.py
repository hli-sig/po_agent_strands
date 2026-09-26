"""Every string a Jira tool hands back to the agent.

The renderer *is* the interface: a lane's entire view of a search result is the
text produced here, so a field that never reaches one of these functions is
effectively not fetched at all, no matter what `jira_read_fields()` asked for.
That is not hypothetical — `assignee` was requested, normalised and carried for
months while `format_issue_line` omitted it, so the agent truthfully reported
ownership as unavailable.

Every truncation cap in the Jira layer also lives here, for the same reason:
a limit the consumer can't tell applied is a lie, so each one is rendered with
a warning rather than applied silently.

Pure — takes dicts, returns strings. No client, no network.
"""

from __future__ import annotations

# Deep reads are unbounded by nature — a description can be pages long, and a
# long-running blocker can carry dozens of comments. These caps keep a triage
# across ~25 tickets inside a sane context budget; every one of them is reported
# when it bites rather than silently applied.
_DETAIL_MAX_ISSUES = 25
_DETAIL_BODY_CHARS = 1200


def format_issue_line(issue: dict) -> str:
    """One issue as a single readable line.

    The owner is always shown, including as an explicit "unassigned" — this is
    the *only* view of a search result the agent gets, so anything omitted here
    is effectively not fetched at all no matter what `jira_read_fields` asked
    for. An unowned blocker is also a finding in its own right, which a blank
    would hide.
    """
    return (
        f"- {issue.get('key', '')} [{issue.get('type', '')}/{issue.get('status', '')}] "
        f"{(issue.get('created') or '')[:10]} "
        f"({issue.get('story_points') or 0:g} pts, {issue.get('assignee') or 'unassigned'})"
        f" — {issue.get('summary', '')}"
    )


def render_sprint_report(metrics: dict, issues: list[dict]) -> str:
    """Render `compute_sprint_metrics` output as text for the agent to narrate.

    Pure formatting — every number here was computed in Python by `metrics.py`.
    """
    lines = [
        f"{metrics['sprint_name']} (Jira internal sprint ID: {metrics.get('sprint_id')})",
        f"State: {metrics.get('sprint_state')} | "
        f"{(metrics.get('start_date') or '')[:10]} -> {(metrics.get('end_date') or '')[:10]}",
        "",
        f"Risk level: {metrics['risk_level']}",
        f"Completion by story points: {metrics['completion_pct']}%",
        "",
        f"Total story points: {metrics['total_points']:g}",
        f"  Done:        {metrics['done_points']:g}",
        f"  In progress: {metrics['in_progress_points']:g}",
        f"  Blocked:     {metrics['blocked_points']:g}",
        f"  To do:       {metrics['todo_points']:g}",
        "",
        f"Issues: {metrics['issue_count']} "
        f"({metrics['done_count']} done, {metrics['in_progress_count']} in progress, "
        f"{len(metrics['blocked_tickets'])} blocked, {metrics['todo_count']} to do)",
    ]

    if metrics["blocked_tickets"]:
        lines += ["", "Blocked / flagged:"]
        lines += [
            f"- {t['key']} [{t['status']}] {t['story_points']:g} pts, "
            f"{t.get('assignee') or 'unassigned'} — {t['summary']}"
            for t in metrics["blocked_tickets"]
        ]

    lines += ["", "All issues:"]
    lines += [format_issue_line(i) for i in issues]

    if metrics.get("truncated"):
        lines += [
            "",
            "WARNING: the issue list was truncated, so these totals are an "
            "UNDERCOUNT. Tell the user the numbers are incomplete.",
        ]

    return "\n".join(lines)


def format_transitions(transitions: list[dict]) -> str:
    """Render available transitions as `name -> destination status`."""
    if not transitions:
        return "none"
    return ", ".join(
        f"{t.get('name')} -> {t.get('to_status')}" for t in transitions
    )


def format_candidates(candidates: list[dict]) -> str:
    """Render ambiguous matches so the user can pick one."""
    return "\n".join(
        f"- {c.get('display_name')}"
        + (f" <{c['email']}>" if c.get("email") else "")
        + f" (account id: {c.get('account_id')})"
        for c in candidates
    )


def _clip(text: str, limit: int = _DETAIL_BODY_CHARS) -> str:
    """Trim long text, saying so. Silent truncation reads as "that's all there is"."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n[... truncated, {len(text) - limit} more characters]"


def render_issue_detail(detail: dict) -> str:
    """One issue's full context — owner, description, recent comments — as text."""
    lines = [
        f"### {detail.get('key', '')} [{detail.get('type', '')}/{detail.get('status', '')}]"
        f" — {detail.get('summary', '')}",
        f"Owner: {detail.get('assignee') or 'unassigned'}"
        f" | Points: {detail.get('story_points') or 0:g}"
        f" | Flagged: {'yes' if detail.get('flagged') else 'no'}"
        f" | Days in status: {detail.get('days_in_status', 0)}",
    ]

    description = _clip(detail.get("description", ""))
    lines += ["", "Description:", description or "(empty)"]

    comments = detail.get("comments") or []
    if not comments:
        lines += ["", "Comments: none."]
        return "\n".join(lines)

    total, shown = comments[0].get("total", len(comments)), len(comments)
    header = f"Comments (showing {shown} of {total}, oldest first):"
    lines += ["", header]
    lines += [
        f"- {c.get('created')} {c.get('author')}: {_clip(c.get('body', ''), 600)}"
        for c in comments
    ]
    return "\n".join(lines)


def _render_batch_result(
    results: list[dict],
    verb: str,
    destination: str = "",
    describe=None,
) -> str:
    """Report a batch write: what succeeded, and exactly what didn't and why.

    Shared by every tool whose client method returns `{"key", "error"}` per
    input key — sprint moves, comments, transitions. One renderer means a
    partly-failed batch cannot be summarised honestly by one tool and glossed
    over by another; that divergence is the whole risk with batch writes.

    `verb` is lowercase and used twice: "Moved 3 of 4 issue(s)" and "these were
    NOT moved". `describe` overrides how a successful key is shown, for tools
    with more to say than the key itself.

    Bulk *create* deliberately does not use this: it has no key to report until
    it succeeds, so it identifies failures by draft summary instead.
    """
    done = [r for r in results if not r["error"]]
    failed = [r for r in results if r["error"]]
    describe = describe or (lambda r: r["key"])

    where = f" to {destination}" if destination else ""
    lines = [f"{verb.capitalize()} {len(done)} of {len(results)} issue(s){where}."]
    if done:
        lines.append("- " + ", ".join(describe(r) for r in done))

    if failed:
        lines.append("")
        lines.append(f"FAILED ({len(failed)}) — these were NOT {verb}:")
        lines += [f"- {r['key']}: {r['error']}" for r in failed]

    return "\n".join(lines)
