"""Finance's FY budget converter, absorbed into this repo (see PROVENANCE.md).

Deterministic conversion of approved fiscal-year budget workbooks into the
24-column daily Snowflake-ingest CSV, plus a `.metadata.json` audit sidecar.

**No finance logic lives outside this package**, and nothing here knows about
LangGraph, agents or prompts. `pmagent/tools/finance_tools.py` is the only
caller: it selects input files, calls `pipeline.convert_fy_budget`, checks the
validation result and renders it for the agent. It does not compute, allocate,
map, round or reshape a single figure — that is entirely this package's job, and
it must stay that way.

Nothing is imported eagerly here. The modules below pull in pandas and openpyxl,
which is a second or so of import time that every non-finance turn would
otherwise pay just for `finance_tools` to be importable — the same reason
`jira.get_client()` is lazy. Import the submodule you need:

    from pmagent.tools.fy_budget.pipeline import convert_fy_budget
    from pmagent.tools.fy_budget import convert_budget as cb
"""
