"""
The one function that turns a lane declaration into a Strands Agent.

Every specialist lane is declared as a `(SYSTEM_PROMPT, TOOLS)` pair in its own
module (`ticket_agent.py`, `sprint_agent.py`, ...). This module owns the "how":
every lane agent is built the same way, so no lane can forget the approval gate.

STRANDS CONCEPT — `Agent(model, system_prompt, tools, ...)` *is* the agent loop:
call the model, run the tools it asks for, feed results back, repeat until it
answers. In the LangGraph original that loop was wired by hand for every lane
(an LLM node, a ToolNode, a conditional edge back — `make_agent_node` and
`make_tools_router`). Here it is one constructor call.
"""

from __future__ import annotations

from strands import Agent
from strands.agent.conversation_manager import NullConversationManager
from strands.hooks import HookProvider
from strands.models import Model

from pmagent.gate import ApprovalGate
from pmagent.llm import get_model


def make_lane_agent(
    name: str,
    system_prompt: str,
    tools: list,
    *,
    model: Model | None = None,
    hooks: list[HookProvider] | tuple = (),
    messages: list[dict] | None = None,
    callback_handler=None,
) -> Agent:
    """Build one lane's agent.

    - `hooks=[ApprovalGate(), ...]` — the gate is not optional; extra hooks (the
      CLI's ConsoleEcho) are added after it.
    - `NullConversationManager()` — keep the whole history, as the original did.
      The default (a sliding window) would silently drop the user's early
      messages — e.g. the list of ticket keys a batch must match — in a long
      session. The cost: a very long session eventually overflows the model's
      context window, and the CLI then tells you to /new.
    - `callback_handler=None` — the default handler streams raw model output to
      stdout. Output is the CLI's job (ConsoleEcho), not the agent's. The web
      server passes its own handler to stream text and reasoning deltas.
    """
    return Agent(
        model=model or get_model(),
        name=name,
        system_prompt=system_prompt,
        tools=tools,
        hooks=[ApprovalGate(), *hooks],
        conversation_manager=NullConversationManager(),
        callback_handler=callback_handler,
        messages=messages if messages is not None else [],
    )
