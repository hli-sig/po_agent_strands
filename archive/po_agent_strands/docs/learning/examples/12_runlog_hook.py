"""Lesson 12 — a run log as a Strands hook (toy version you'll grow into pmagent/runlog.py).

One HookProvider writes a JSON line per lifecycle event. It never raises: a
logging failure must never break a turn. Offline, with the scripted model.

    uv run docs/learning/examples/12_runlog_hook.py
"""

import json
import tempfile
from pathlib import Path

from _common import banner, model

from strands import Agent, tool
from strands.hooks import AfterToolCallEvent, BeforeToolsEvent, HookProvider, MessageAddedEvent
from tests.fakes import call, text

banner("12 run-log hook")
LOG = Path(tempfile.mkdtemp()) / "runlog.jsonl"


def record(event: str, **fields) -> None:
    """Append one JSON line. Never raises."""
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"event": event, **fields}, default=str) + "\n")
    except Exception:  # noqa: BLE001 — logging must not break the agent
        pass


class RunLogHook(HookProvider):
    def register_hooks(self, registry, **kwargs):
        registry.add_callback(BeforeToolsEvent, self.requested)
        registry.add_callback(AfterToolCallEvent, self.finished)
        registry.add_callback(MessageAddedEvent, self.message)

    def requested(self, event):
        # Requested calls, logged BEFORE the gate can pause them — so a write the
        # human later rejects still appears in the log.
        for block in event.message["content"]:
            if "toolUse" in block:
                record("tool_call", name=block["toolUse"]["name"], args=block["toolUse"]["input"])

    def finished(self, event):
        text_out = " ".join(b.get("text", "") for b in event.result.get("content", []))
        record("tool_result", name=event.tool_use["name"], status=event.result["status"],
               result=text_out[:500])        # clip: full results make the log unreadable

    def message(self, event):
        if event.message["role"] == "assistant":
            record("assistant", text=" ".join(b.get("text", "") for b in event.message["content"])[:200])


@tool
def sprint_status(sprint: int) -> str:
    """Status of a sprint by its visible number."""
    return f"Sprint {sprint}: 143.5 of 232.5 points done."


agent = Agent(model=model([[call("sprint_status", sprint=31)], [text("Sprint 31 is 62% done.")]]),
              tools=[sprint_status], hooks=[RunLogHook()], callback_handler=None)
agent("How is sprint 31?")
print(LOG.read_text())
