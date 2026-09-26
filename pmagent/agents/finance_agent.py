"""Finance Agent declaration — FY budget data preparation.

The same two-line shape as every other lane. What is unusual about this one is
where the work happens: the "tool" is a large deterministic conversion package
(`pmagent/tools/fy_budget/`) that reads Finance's workbooks, allocates monthly
figures across calendar days, and validates every monthly roll-up against its
source. The model's entire job is choosing the inputs, confirming the fiscal
start with a human, and reading the validation verdict out honestly.

That makes it the sharpest instance of the repo's core split: the LLM does
judgment, plain Python does arithmetic. Here the arithmetic is somebody else's
audited package, and the prompt's main job is stopping the model from helping.
"""

from pmagent.prompts import prompts
from pmagent.skills import load_skill
from pmagent.tools.finance_tools import (
    create_fy_budget_csv,
    inspect_fy_budget_inputs,
    read_fy_budget_run,
)


TOOLS = [inspect_fy_budget_inputs, create_fy_budget_csv, read_fy_budget_run]
SYSTEM_PROMPT = prompts.inject_skill(
    prompts.finance_agent_system_prompt, load_skill("fy_budget")
)
