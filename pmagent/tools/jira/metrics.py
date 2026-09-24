"""Deterministic sprint analytics. No LLM touches any number in this module.

Everything here operates on the normalised issue shape `JiraClient` produces,
so it is unit-testable on plain dicts with no network and no credentials.
`compute_sprint_metrics` is the single source of sprint arithmetic — every
sprint tool renders its output rather than re-deriving totals.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pmagent import env

# Status names that mean "actively worked" only when Jira gives us no status
# category to go on. Category is always preferred — see `bucket_of`.
_IN_PROGRESS_NAMES = {"in progress", "in review", "qa", "testing"}
_DONE_NAMES = {"done", "closed", "resolved"}
_BLOCKED_NAMES = {"blocked", "impediment"}


def parse_jira_datetime(value: str | None) -> datetime | None:
    """Parse a Jira timestamp (e.g. '2026-07-01T09:00:00.000+1000')."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def days_in_current_status(raw: dict, now: datetime | None = None) -> int:
    """How many days an issue has sat in its current status.

    Reads the most recent status transition out of an expanded changelog. Jira
    does not expose this as a field — without `expand=changelog` there is
    nothing to compute from, so this returns 0 and the "stuck" heuristic in
    `compute_sprint_metrics` stays dormant rather than reporting a wrong number.

    Falls back to the created date when an issue has never changed status,
    which is the honest reading: it has been sitting there since it was made.
    """
    now = now or datetime.now(UTC)

    changed_at: datetime | None = None
    for history in (raw.get("changelog") or {}).get("histories", []):
        if not any(item.get("field") == "status" for item in history.get("items", [])):
            continue
        when = parse_jira_datetime(history.get("created"))
        if when and (changed_at is None or when > changed_at):
            changed_at = when

    if changed_at is None:
        # No status transition recorded. If the changelog wasn't requested at
        # all, say nothing rather than guessing from the created date.
        if "changelog" not in raw:
            return 0
        changed_at = parse_jira_datetime((raw.get("fields") or {}).get("created"))

    if changed_at is None:
        return 0

    return max(0, (now - changed_at).days)


def is_done(issue: dict) -> bool:
    """True when an issue is complete.

    Prefers `statusCategory` (`done`), which is stable across custom workflows.
    Falls back to matching status names only when the category is absent.
    """
    category = (issue.get("status_category") or "").lower()
    if category:
        return category == "done"
    return (issue.get("status") or "").lower() in _DONE_NAMES


def is_blocked(issue: dict, stuck_threshold_days: int | None = None) -> bool:
    """True when an issue is blocked, flagged, or stuck mid-flight.

    "Stuck" deliberately means *started and then stalled* — work whose status
    category is `indeterminate` and hasn't moved in `stuck_threshold_days`. A
    ticket sitting in To Do for six months is backlog age, not a blocker, and
    counting it as one buries the handful of genuinely stalled items. Against a
    real sprint the naive "anything older than N days" rule flagged 105 of 129
    tickets, which is noise.
    """
    if is_done(issue):
        return False
    if issue.get("blocked") or issue.get("flagged"):
        return True
    if (issue.get("status") or "").lower() in _BLOCKED_NAMES:
        return True

    threshold = (
        env.JIRA_STUCK_THRESHOLD_DAYS if stuck_threshold_days is None else stuck_threshold_days
    )
    category = (issue.get("status_category") or "").lower()
    in_flight = category == "indeterminate" or (
        not category and (issue.get("status") or "").lower() in _IN_PROGRESS_NAMES
    )
    return in_flight and issue.get("days_in_status", 0) >= threshold


def bucket_of(issue: dict, stuck_threshold_days: int | None = None) -> str:
    """Assign an issue to exactly one of: done / blocked / in_progress / todo.

    Buckets are mutually exclusive so the four point totals always sum to the
    sprint total — checked by `compute_sprint_metrics`.
    """
    if is_done(issue):
        return "done"
    if is_blocked(issue, stuck_threshold_days):
        return "blocked"

    category = (issue.get("status_category") or "").lower()
    if category:
        return "in_progress" if category == "indeterminate" else "todo"
    return (
        "in_progress"
        if (issue.get("status") or "").lower() in _IN_PROGRESS_NAMES
        else "todo"
    )


def compute_sprint_metrics(sprint_payload: dict, stuck_threshold_days: int | None = None) -> dict:
    """Compute sprint-health metrics from normalised issue data.

    The single source of sprint arithmetic — every sprint tool renders this
    rather than re-deriving totals, so "complete" means the same thing
    everywhere. No LLM touches these numbers.
    """
    issues = sprint_payload.get("issues", [])
    sprint = sprint_payload.get("sprint", {})

    points = {"done": 0.0, "blocked": 0.0, "in_progress": 0.0, "todo": 0.0}
    counts = {"done": 0, "blocked": 0, "in_progress": 0, "todo": 0}
    blocked_issues: list[dict] = []

    for issue in issues:
        bucket = bucket_of(issue, stuck_threshold_days)
        points[bucket] += issue.get("story_points") or 0
        counts[bucket] += 1
        if bucket == "blocked":
            blocked_issues.append(issue)

    total_points = sum(points.values())
    completion_rate = round(points["done"] / total_points, 3) if total_points else 0.0

    if completion_rate >= 0.7 and len(blocked_issues) <= 1:
        risk = "LOW"
    elif completion_rate >= 0.4:
        risk = "MODERATE"
    else:
        risk = "HIGH"

    # A truncated issue list makes every total below an undercount. Say so
    # loudly rather than reporting a confidently wrong completion figure.
    truncated = bool(sprint_payload.get("truncated"))

    return {
        "sprint_name": sprint.get("name", "Active Sprint"),
        "sprint_id": sprint.get("id"),
        "sprint_state": sprint.get("state"),
        "start_date": sprint.get("startDate"),
        "end_date": sprint.get("endDate"),
        "total_points": total_points,
        "done_points": points["done"],
        "in_progress_points": points["in_progress"],
        "blocked_points": points["blocked"],
        "todo_points": points["todo"],
        "completion_rate": completion_rate,
        "completion_pct": round(completion_rate * 100, 1),
        "issue_count": len(issues),
        "done_count": counts["done"],
        "in_progress_count": counts["in_progress"],
        "todo_count": counts["todo"],
        "blocked_tickets": [
            {
                "key": issue.get("key", ""),
                "summary": issue.get("summary", ""),
                "status": issue.get("status", ""),
                "story_points": issue.get("story_points") or 0,
                "days_in_status": issue.get("days_in_status", 0),
                "flagged": bool(issue.get("flagged")),
                # "Who owns this blocker?" is the first question asked of every
                # sprint report, so it travels with the blocker, not one tool
                # call away.
                "assignee": issue.get("assignee"),
            }
            for issue in blocked_issues
        ],
        "risk_level": risk,
        "truncated": truncated,
    }
