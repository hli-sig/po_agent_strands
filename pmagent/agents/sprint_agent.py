"""
Sprint Agent.

Specialises in sprint health: completion %, blocked/stuck tickets, risk level,
and standup summaries.

Crucially, the *numbers* come from `compute_sprint_metrics` (plain Python) via
the `get_sprint_status` tool — the LLM only writes prose on top of them. This is
the blueprint's "LLM + deterministic systems" principle: never let the model do
the arithmetic that drives a delivery decision.

Like the Ticket Agent, this module only declares the toolset and prompt; the
agent itself is built generically by `agents/common.make_lane_agent`.
"""

from pmagent.prompts import prompts
from pmagent.tools.jira import (
    add_jira_comment,
    get_sprint_status,
    get_sprint_status_by_number,
    list_jira_transitions,
    move_jira_issues_to_sprint,
    query_jira_issues,
    read_jira_issue_details,
    read_jira_issues_by_key,
    remove_jira_issues_from_sprint,
    transition_jira_issues,
)

# `get_sprint_status_by_number` is the one the prompt actually tells the agent
# to reach for — users say "sprint 31", never the internal sprint ID.
# `query_jira_issues` covers follow-ups the metrics don't answer ("which of
# those are mine?").
#
# Scope changes ("move these into 31", "drop that one") land in this lane as
# often as in the ticket lane, so the two sprint-membership writes live here
# too. They are in `WRITE_TOOL_NAMES`, so the approval gate stops them like any other
# write — this lane is no longer read-only.
#
# `add_jira_comment` and `transition_jira_issues` are here for the same reason:
# chasing a stalled ticket, or moving a finished one along, is the natural next
# step after a sprint review names it — and that conversation happens in this
# lane, not the ticket one. `list_jira_transitions` is the read-only lookup
# behind the latter, so it stays out of `WRITE_TOOL_NAMES`.
TOOLS = [
    get_sprint_status_by_number,
    get_sprint_status,
    query_jira_issues,
    read_jira_issue_details,
    read_jira_issues_by_key,
    list_jira_transitions,
    add_jira_comment,
    transition_jira_issues,
    move_jira_issues_to_sprint,
    remove_jira_issues_from_sprint,
]
SYSTEM_PROMPT = prompts.sprint_agent_system_prompt