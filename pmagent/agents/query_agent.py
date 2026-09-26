"""
Query lane — the read-only default.

Answers general look-ups ("what tickets mention FX rate?", "who owns CSCI-1690
and why is it blocked?"). Anything the router can't place lands here, so this
lane can never write.

*Every* read-only tool belongs here, not just search: a read withheld from the
read lane produces the same "I can't do that" as a missing tool, with none of the
safety justification. Only writes are withheld (`create_fy_budget_csv` included).

LangGraph original: `QUERY_TOOLS` lived in `agents/orchestrator.py`, which also
held the classifier; that half now lives in `agents/router.py`.
"""

from pmagent.prompts import prompts
from pmagent.tools.confluence_tools import read_confluence_page, search_confluence
from pmagent.tools.finance_tools import inspect_fy_budget_inputs, read_fy_budget_run
from pmagent.tools.jira import (
    list_jira_transitions,
    query_jira_issues,
    read_jira_issue_details,
    read_jira_issues_by_key,
)


TOOLS = [
    query_jira_issues,
    read_jira_issue_details,
    read_jira_issues_by_key,
    list_jira_transitions,
    inspect_fy_budget_inputs,
    read_fy_budget_run,
    read_confluence_page,
    search_confluence,
]
SYSTEM_PROMPT = prompts.orchestrator_system_prompt
