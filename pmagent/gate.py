"""
The approval gate: nothing that writes runs until a human says yes.

STRANDS CONCEPTS — hooks and interrupts.

* A **hook** is a callback Strands invokes at a fixed point in the agent loop.
  `ApprovalGate` is a `HookProvider` that registers one callback on
  `BeforeToolsEvent`, which fires after the model has asked for a batch of tool
  calls and *before* any of them executes.
* An **interrupt** pauses the agent from inside a hook. `event.interrupt(name,
  reason=...)` raises; Strands catches it, stops the loop and returns an
  `AgentResult` with `stop_reason == "interrupt"` and `result.interrupts` set. The
  caller (here `PMAssistant` → the CLI) shows the reason to a human, then resumes
  the agent by calling it with a list of `interruptResponse` blocks. Strands
  re-fires `BeforeToolsEvent` for the same batch, and this time
  `event.interrupt(...)` *returns* the human's answer instead of raising.
* On "reject", setting `event.cancel = "<text>"` cancels the whole batch: each
  tool call gets an error result with that text, and the model is called again
  so it can respond to the refusal.

LangGraph original: the graph was compiled with `interrupt_before=[write nodes]`
and tools were split across read/write `ToolNode`s; a rejection was injected
with `graph.update_state(..., as_node=...)`. Same guarantee, different
mechanism: here the model still cannot skip the gate, because the gate is code
in the agent loop, not an instruction in the prompt.

**Which calls are gated** (`requires_approval`):

1. A name the agent does not have is never gated. It cannot execute — Strands
   answers it with an "Unknown tool" error — and prompting a human to approve
   a call that cannot run would be a lie. (This happens: all lanes share one
   history, so the read-only query lane sometimes "remembers" a write tool it
   saw another lane use.)
2. A name in `WRITE_TOOL_NAMES` is gated.
3. Anything else the agent has is gated **unless it is a declared read** — a
   module's `READ_TOOLS`, or a reviewed MCP tool in
   `mcp_tools.APPROVED_READ_TOOLS`. So a tool discovered at runtime from an MCP
   server that nobody has reviewed is treated as a write. The LangGraph version
   failed *open* here (it gated only known write names); this one fails closed.

A batch that contains even one gated call is gated as a whole — a write is
never waved through because it was bundled with a search.
"""
from __future__ import annotations

from strands.hooks import BeforeToolsEvent, HookProvider, HookRegistry

from pmagent.tools import (
    confluence_tools,
    diagram_tools,
    finance_tools,
    mcp_tools,
    spreadsheet_tools,
)
from pmagent.tools.jira import tools_read as jira_read
from pmagent.tools.jira import tools_write as jira_write

# Every module that defines @tools. Each declares READ_TOOLS and WRITE_TOOLS;
# tests/test_agent_lanes.py walks pmagent/tools/ and fails if one is missing here.
TOOL_MODULES = (
    jira_read,
    jira_write,
    confluence_tools,
    diagram_tools,
    finance_tools,
    spreadsheet_tools,
)

# Tools that change something outside this process. Anything listed here runs
# only after a human has approved it. Kept explicit (not derived from
# WRITE_TOOLS) so adding a write tool is a deliberate two-place change that the
# tests reconcile.
WRITE_TOOL_NAMES = frozenset({
    "create_jira_issues",
    "create_jira_issue",
    "assign_jira_issue",
    "update_jira_issue",
    "add_jira_comment",
    "transition_jira_issues",
    "move_jira_issues_to_sprint",
    "remove_jira_issues_from_sprint",
    "propose_spreadsheet_cell_update",
    "apply_approved_spreadsheet_updates",
    "prepare_spreadsheet_approval_queue",
    # Writes a CSV of real financial figures plus its audit sidecar. "Outside
    # this process" includes the local filesystem — a generated budget file gets
    # ingested into Snowflake, so producing one is not a preview.
    "create_fy_budget_csv",
})


# Tools that may run without asking: every module's declared reads, plus MCP
# tools a human has reviewed.
READ_TOOL_NAMES = frozenset(
    {t.tool_name for module in TOOL_MODULES for t in module.READ_TOOLS}
    | mcp_tools.APPROVED_READ_TOOLS
)

# What a rejected tool call returns. The agent sees this and responds to it.
REJECTION_MESSAGE = (
    "The user rejected this action. Nothing was written. "
    "Ask what they would like changed."
)

# Interrupt name, and the response values the CLI sends back.
INTERRUPT_NAME = "approval"
APPROVE = "approve"
REJECT = "reject"


def _is_registered(name:str, agent) -> bool:
    """True when `agent` would actually execute a tool called `name`.

    Mirrors the executor's own lookup (dynamic tools, then the registry) rather
    than `agent.tool_names`, which omits a tool whose spec failed validation but
    which the executor would still run.
    """
    registry = agent.tool_registry
    return name in registry.dynamic_tools or name in registry.registry

def requires_approval(name: str, agent) -> bool:
    """Should a call to `name`, made by `agent`, wait for a human?"""
    if not _is_registered(name, agent):
        return False
    if name in WRITE_TOOL_NAMES:
        return True
    return name not in READ_TOOL_NAMES



class ApprovalGate(HookProvider):
    """Pause before any tool batch that contains a write; cancel it if rejected.

    Registered on every lane agent by `agents.common.make_lane_agent`.
    """

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
            registry.add_callback(BeforeToolsEvent, self.check_batch)

    def check_batch(self,event:BeforeToolsEvent) -> None:
        calls = [block["toolUse"] for block in event.message["content"] if "toolUse" in block]
        gated = [call for call in calls if requires_approval(call["name"],event.agent)]
        if not gated:
             return # pure read: never interrupted

        reason = {
            "calls": [
                {"id": call["toolUseId"], "name": call["name"], "args": call["input"]}
                for call in gated
            ]
        }
        # First time: raises, and the agent stops with stop_reason="interrupt".
        # After resume: returns the human's answer.

        answer = event.interrupt(INTERRUPT_NAME,reason=reason)
        if answer != APPROVE:
             event.cancel = REJECTION_MESSAGE
         