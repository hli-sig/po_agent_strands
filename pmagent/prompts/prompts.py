"""Loads system prompts from the `.md` files next to this module.

Prompts live in Markdown so they can be read, diffed and iterated on without
touching Python. Editing behaviour means editing a `.md` file here; only control
flow belongs in code.
"""

import os

_DIR = os.path.dirname(os.path.abspath(__file__))

def _load(name:str) -> str:
    with open(os.path.join(_DIR,name),encoding='utf-8') as f:
        return f.read()

def inject_skill(template: str, skill: str) -> str:
    """Substitute a skill's Markdown into a prompt's `{skill}` placeholder.

    Uses a literal replace rather than `str.format`, because prompt files are
    Markdown and routinely contain braces — JSON examples, code blocks, JQL —
    which `.format` would try to interpret as fields and raise KeyError on.
    """
    return template.replace("{skill}", skill)


orchestrator_system_prompt = _load("orchestrator.md")
ticket_agent_system_prompt = _load("ticket_agent.md")
sprint_agent_system_prompt = _load("sprint_agent.md")
requirements_writer_system_prompt = _load("requirements_writer.md")
requirements_reviewer_system_prompt = _load("requirements_reviewer.md")
diagram_agent_system_prompt = _load("diagram_agent.md")
spreadsheet_agent_system_prompt = _load("spreadsheet_agent.md")
finance_agent_system_prompt = _load("finance_agent.md")