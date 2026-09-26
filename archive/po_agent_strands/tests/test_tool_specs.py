"""Every word of a tool's docstring must reach the model.

A tool's docstring *is* its prompt: Strands turns it into the `description` of
the tool spec the model sees. Strands' docstring parser keeps the summary, the
body and a `Returns:` section, but silently **drops free text that follows the
`Args:` block** — the argument descriptions move into the input schema and any
paragraph after them vanishes. LangChain's `@tool` sent the whole docstring, so a
straight decorator swap deleted instructions such as "the images are where the
answer is" from `read_confluence_page`.

This test fails for any tool whose docstring has prose the spec lost. The fix is
always the same: move the paragraph above `Args:`.
"""

from __future__ import annotations

import importlib
import inspect
import pathlib
import re

import pytest
from strands.tools.decorator import DecoratedFunctionTool

import pmagent.tools

_SECTION = re.compile(r"^\s*(Args|Arguments|Parameters|Returns|Raises|Yields):\s*$")


def _tool_modules():
    root = pathlib.Path(pmagent.tools.__file__).parent
    for path in sorted(root.rglob("*.py")):
        module = ".".join(path.relative_to(root.parent.parent).with_suffix("").parts)
        yield importlib.import_module(module)


def all_tools() -> list[DecoratedFunctionTool]:
    found: dict[str, DecoratedFunctionTool] = {}
    for module in _tool_modules():
        for _, value in inspect.getmembers(module):
            if isinstance(value, DecoratedFunctionTool):
                found[value.tool_name] = value
    return list(found.values())


def _prose_paragraphs(docstring: str) -> list[str]:
    """Paragraphs of a docstring that are not inside an Args-style section."""
    lines = inspect.cleandoc(docstring).splitlines()
    kept: list[str] = []
    in_args = False
    for line in lines:
        header = _SECTION.match(line)
        if header:
            # Args-like sections become the input schema; Returns stays in the
            # description, so only Args-style blocks are skipped.
            in_args = header.group(1) in ("Args", "Arguments", "Parameters")
            if not in_args:
                kept.append(line)
            continue
        if in_args and (not line.strip() or line.startswith((" ", "\t"))):
            continue
        in_args = False
        kept.append(line)
    paragraphs = re.split(r"\n\s*\n", "\n".join(kept))
    return [" ".join(p.split()) for p in paragraphs if p.strip()]


def _normalise(text: str) -> str:
    return " ".join(text.split())


TOOLS = all_tools()


def test_tools_were_discovered():
    names = {t.tool_name for t in TOOLS}
    assert {"create_jira_issues", "read_confluence_page", "create_fy_budget_csv"} <= names


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.tool_name)
def test_every_docstring_paragraph_reaches_the_model(tool):
    description = _normalise(tool.tool_spec["description"])
    lost = [
        p for p in _prose_paragraphs(tool._tool_func.__doc__ or "")
        if p not in description
    ]
    assert not lost, (
        f"{tool.tool_name}: these docstring paragraphs are not in the tool spec the "
        f"model sees — move them above 'Args:':\n" + "\n".join(f"  - {p[:90]}" for p in lost)
    )
