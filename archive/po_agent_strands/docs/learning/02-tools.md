# 2. Tools

## The concept

```python
from strands import tool

@tool
def query_jira_issues(jql: str, max_results: int = 50) -> str:
    """Extract Jira issues with a JQL query. The general-purpose read tool.

    Args:
        jql: A Jira Query Language string.
        max_results: Cap on issues returned.
    """
```

`@tool` reads the function's **type hints and docstring** and builds a `tool_spec`,
which is what the model actually sees:

- `name`: the function name, or `@tool(name="...")`.
- `description`: the docstring summary and body (and a `Returns:` section).
- `inputSchema`: a JSON schema built from the type hints. Each argument's
  description comes from `Args:`.

**The docstring is the prompt.** Instructions like "ONLY call this AFTER the user
has confirmed" belong there, because that is the text the model reads when it
decides to call the tool.

### Gotcha found during the port

Strands drops free prose that comes **after** the `Args:` block. LangChain kept
it. A straight decorator swap silently deleted instructions from three tools. The
worst case was `read_confluence_page` losing "the images are where the answer is;
do not report the field list as unavailable". Run
`docs/learning/examples/02_tools.py` to watch it happen.
`tests/test_tool_specs.py` now fails for any tool whose docstring prose doesn't
reach its spec.

### Return values

- Return a `str` (or anything JSON-serialisable) and Strands wraps it as
  `{"status": "success", "content": [{"text": ...}]}`.
- Return that dict shape yourself to control the result, for example to include
  **images**:

```python
return {"status": "success", "content": [
    {"text": "DimEmployee — the columns are in the image below"},
    {"image": {"format": "png", "source": {"bytes": png_bytes}}},
]}
```

- If a tool raises, the exception becomes an error result the model can see. This
  repo prefers catching errors and returning a readable message, because a lane
  needs an answer, not a traceback.

A decorated tool is still a plain function: `query_jira_issues("project = CSCI")`
works in a test.

## In this repo

- `pmagent/tools/jira/tools_read.py::query_jira_issues` and every tool in
  `pmagent/tools/jira/tools_write.py`: the only code changes from the original are
  the `@tool` import and `.func(jql)` → `query_jira_issues(jql)` in `tools_read.py`,
  plus docstrings that named `graph.py` (see `docs/evidence/verbatim_report.txt`).
- `pmagent/tools/diagram_tools.py::draft_diagram_brief` uses `@tool(context=True)`:
  Strands then passes a `tool_context: ToolContext` (hidden from the model's schema)
  with the calling `agent`, the `tool_use` and the `invocation_state`. The tool uses
  `tool_context.agent.model` so it runs on the same model as the lane that called it.
- `pmagent/tools/confluence_tools.py::read_confluence_page` returns text **and
  images** in one ToolResult. `pmagent/tools/confluence_tools.py::image_blocks`
  builds the Strands `image` blocks.
- Every tool module declares `READ_TOOLS` and `WRITE_TOOLS`. The approval gate
  (lesson 6) is built from those lists, and `tests/test_agent_lanes.py` reconciles
  them. That contract is unchanged from the original.

## How the LangGraph original did it

- LangChain's `@tool` has the same docstring-as-spec idea.
- Images were the hard part. OpenAI rejects images inside a tool message, so the
  original returned a LangGraph `Command` that appended a fake `HumanMessage`
  carrying the images. The classifier and the CLI then had to skip that message
  (`_is_injected`, `IMAGE_MESSAGE_NAME`).
- Strands' OpenAI provider does that split itself when it builds the request
  (`OpenAIModel._split_tool_message_images`), so the whole workaround is gone. One
  trace of it remains on purpose: since the images reach OpenAI in a *separate*
  message, the tool text labels each image and says they follow in order.

## Exercise

1. Run `02_tools.py`. Move the "comes after Args" sentence above `Args:` and
   confirm it appears in the spec.
2. Write a tool `days_until(date: str) -> int` in a scratch file, print its
   `tool_spec`, and call it directly.
3. In `pmagent/tools/finance_tools.py`, find the two paragraphs moved above
   `Args:` during the port (hint: `inspect_fy_budget_inputs`,
   `create_fy_budget_csv`). Move one back below `Args:` and run
   `uv run pytest tests/test_tool_specs.py`. Then undo it.
