"""Jira integration: a thin client, the Strands `@tool`s the agents call, and
the deterministic sprint-metric functions.

This was one 2,000-line module. It is now a package split along the seams that
already existed inside it, and the dependency graph only ever points one way —
every module below `client.py` is pure, so it imports and unit-tests with no
credentials and no network:

    fields.py       what we read from Jira, and how a draft becomes a payload
    jql.py          JqlBuilder — the only place JQL is quoted or escaped
    metrics.py      done/blocked/bucket arithmetic; compute_sprint_metrics
    matching.py     pick_assignee / pick_transition — exact match or refuse
    render.py       every string a tool returns, and every truncation cap
    validate.py     the machine-checkable half of skills/ticket/SKILL.md
        ↓
    client.py       JiraClient, get_client(), resolve_assignee — all the I/O
        ↓
    tools_read.py   read-only @tools   (READ_TOOLS)
    tools_write.py  mutating @tools    (WRITE_TOOLS) — gated in gate.py

The read/write tool split is the load-bearing one: `gate.py` gates writes by
name, and which file a tool lives in now decides which side of that gate it
belongs on, checked by `tests/test_agent_lanes.py`.

Everything the rest of the codebase used to import from `jira_tools` is
re-exported here, so lanes and tests import `pmagent.tools.jira` and never need
to know which module a name lives in.
"""

from pmagent.tools.jira.client import (
    JiraClient,
    get_client,
    resolve_assignee,
)
from pmagent.tools.jira.fields import (
    build_issue_fields,
    build_issue_update_fields,
    get_flagged_field,
    get_sprint_field,
    get_story_points_field,
    jira_read_fields,
)
from pmagent.tools.jira.jql import JqlBuilder
from pmagent.tools.jira.matching import pick_assignee, pick_transition
from pmagent.tools.jira.metrics import (
    bucket_of,
    compute_sprint_metrics,
    days_in_current_status,
    is_blocked,
    is_done,
    parse_jira_datetime,
)
from pmagent.tools.jira.render import (
    format_candidates,
    format_issue_line,
    format_transitions,
    render_issue_detail,
    render_sprint_report,
)
from pmagent.tools.jira.tools_read import (
    READ_TOOLS,
    find_jira_user,
    get_sprint_status,
    get_sprint_status_by_number,
    list_jira_transitions,
    query_jira_issues,
    read_jira_issue_details,
    read_jira_issues_by_key,
    search_jira_issues,
    validate_ticket_drafts,
)
from pmagent.tools.jira.tools_write import (
    WRITE_TOOLS,
    add_jira_comment,
    assign_jira_issue,
    create_jira_issue,
    create_jira_issues,
    move_jira_issues_to_sprint,
    remove_jira_issues_from_sprint,
    transition_jira_issues,
    update_jira_issue,
)
from pmagent.tools.jira.validate import (
    check_draft_standard,
    normalise_key,
    parse_drafts,
    reconcile_scope,
)

__all__ = [
    # client
    "JiraClient",
    "get_client",
    "resolve_assignee",
    # fields
    "build_issue_fields",
    "build_issue_update_fields",
    "get_flagged_field",
    "get_sprint_field",
    "get_story_points_field",
    "jira_read_fields",
    # jql
    "JqlBuilder",
    # matching
    "pick_assignee",
    "pick_transition",
    # metrics
    "bucket_of",
    "compute_sprint_metrics",
    "days_in_current_status",
    "is_blocked",
    "is_done",
    "parse_jira_datetime",
    # render
    "format_candidates",
    "format_issue_line",
    "format_transitions",
    "render_issue_detail",
    "render_sprint_report",
    # validate
    "check_draft_standard",
    "normalise_key",
    "parse_drafts",
    "reconcile_scope",
    # read tools
    "READ_TOOLS",
    "find_jira_user",
    "get_sprint_status",
    "get_sprint_status_by_number",
    "list_jira_transitions",
    "query_jira_issues",
    "read_jira_issue_details",
    "read_jira_issues_by_key",
    "search_jira_issues",
    "validate_ticket_drafts",
    # write tools
    "WRITE_TOOLS",
    "add_jira_comment",
    "assign_jira_issue",
    "create_jira_issue",
    "create_jira_issues",
    "move_jira_issues_to_sprint",
    "remove_jira_issues_from_sprint",
    "transition_jira_issues",
    "update_jira_issue",
]
