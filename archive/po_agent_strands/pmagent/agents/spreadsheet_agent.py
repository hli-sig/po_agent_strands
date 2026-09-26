"""Spreadsheet Agent declaration.

This lane manages project-control workbooks through Microsoft Graph. It is
limited to creating proposals and applying only rows that a human has already
approved inside the workbook's approval table.
"""

from pmagent.prompts import prompts
from pmagent.tools.spreadsheet_tools import (
    apply_approved_spreadsheet_updates,
    prepare_spreadsheet_approval_queue,
    propose_spreadsheet_cell_update,
)


TOOLS = [
    prepare_spreadsheet_approval_queue,
    propose_spreadsheet_cell_update,
    apply_approved_spreadsheet_updates,
]
SYSTEM_PROMPT = prompts.spreadsheet_agent_system_prompt
