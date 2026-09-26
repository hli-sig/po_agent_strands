"""Lesson 5 — hooks: observing (and steering) the agent loop.

A HookProvider registers callbacks for lifecycle events. This one only
observes. pmagent/cli/echo.py uses MessageAddedEvent the same way to print the
conversation; pmagent/gate.py uses BeforeToolsEvent to *stop* it (lesson 6).

    uv run docs/learning/examples/05_hooks.py [--live]
"""

from _common import banner, model

from strands import Agent, tool
from strands.hooks import (
    AfterToolCallEvent,
    BeforeInvocationEvent,
    BeforeToolCallEvent,
    HookProvider,
    MessageAddedEvent,
)
from tests.fakes import call, text

banner("05 hooks")


@tool
def sprint_status(sprint: int) -> str:
    """Status of a sprint by its visible number."""
    return f"Sprint {sprint}: 60% complete"


class Tracer(HookProvider):
    def register_hooks(self, registry, **kwargs):
        registry.add_callback(BeforeInvocationEvent, lambda e: print("[hook] invocation starts"))
        registry.add_callback(MessageAddedEvent, lambda e: print(f"[hook] message added: role={e.message['role']}"))
        registry.add_callback(BeforeToolCallEvent, lambda e: print(f"[hook] about to run {e.tool_use['name']}"))
        registry.add_callback(AfterToolCallEvent, lambda e: print(f"[hook] {e.tool_use['name']} -> {e.result['status']}"))


agent = Agent(
    model=model([[call("sprint_status", sprint=31)], [text("Sprint 31 is 60% done.")]]),
    tools=[sprint_status],
    hooks=[Tracer()],
    callback_handler=None,
)
print("answer:", str(agent("How is sprint 31?")).strip())
