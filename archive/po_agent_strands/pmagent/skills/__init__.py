"""
Loads skill files (Markdown knowledge bundles) from `pmagent/skills/`.

A "skill" here is just a folder of Markdown that encodes domain knowledge the
agents need — kept separate from code so it can be edited without touching
Python. Currently three: `prd` (Requirements Agent), `ticket` (Ticket Agent) and
`fy_budget` (Finance Agent — the budget partition/allocation rules, which Finance
owns and can edit without a code change).

Inject one into a prompt with `prompts.inject_skill(template, load_skill(name))`.
"""

import os

_DIR = os.path.dirname(os.path.abspath(__file__))


def load_skill(name: str, file: str = "SKILL.md") -> str:
    """Return the text of a skill file, e.g. load_skill('prd')."""
    with open(os.path.join(_DIR, name, file), "r",encoding='utf-8') as f:
        return f.read()
