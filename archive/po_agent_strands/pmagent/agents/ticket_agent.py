"""
Ticket Agent.

Specialises in backlog quality: it drafts well-formed Jira issues from plain
requirements — one or a batch — and creates them only after the user approves.

This module just declares *what the agent is* — its toolset and system prompt.
The Strands Agent is built generically by `make_lane_agent` in
`agents/common.py`. Keeping the "what" and the "how it's wired"
separate is what makes adding a fourth agent later a copy-paste-and-tweak job.

Note the toolset shape: read (`query_jira_issues`) for duplicate/context lookup,
a safe pre-flight (`validate_ticket_drafts`), and the single write
(`create_jira_issues`) behind the human-approval gate the prompt enforces.

The ticket standard lives in `skills/ticket/SKILL.md` and is injected into the
prompt here — same pattern as the PRD skill in `requirements.py`, so the
standard can be edited without touching Python.
"""

from pmagent.prompts import prompts
from pmagent.skills import load_skill
from pmagent.tools.confluence_tools import read_confluence_page, search_confluence
from pmagent.tools.jira import (
    add_jira_comment,
    assign_jira_issue,
    create_jira_issues,
    find_jira_user,
    list_jira_transitions,
    move_jira_issues_to_sprint,
    query_jira_issues,
    read_jira_issue_details,
    read_jira_issues_by_key,
    remove_jira_issues_from_sprint,
    transition_jira_issues,
    update_jira_issue,
    validate_ticket_drafts,
)


TOOLS = [
    query_jira_issues,
    read_jira_issue_details,
    read_jira_issues_by_key,
    find_jira_user,
    list_jira_transitions,
    # Confluence is where the standards a ticket must satisfy are written — the
    # enterprise data model, naming conventions, design decisions. A draft
    # written without them is a draft the team sends back.
    search_confluence,
    read_confluence_page,
    validate_ticket_drafts,
    create_jira_issues,
    assign_jira_issue,
    update_jira_issue,
    add_jira_comment,
    transition_jira_issues,
    move_jira_issues_to_sprint,
    remove_jira_issues_from_sprint,
]
SYSTEM_PROMPT = prompts.inject_skill(
    prompts.ticket_agent_system_prompt, load_skill("ticket")
)
