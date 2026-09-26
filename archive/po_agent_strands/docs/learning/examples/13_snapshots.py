"""Lesson 13 — persisting a conversation with Strands snapshots (single agent).

Agent.take_snapshot() captures messages (and state, and interrupt state with the
"session" preset); load_snapshot() restores them into a fresh agent — the
restart story. PMAssistant can't use this directly (six agents share one list),
which is why lesson 13 builds its own store; this is the idea it builds on.

    uv run docs/learning/examples/13_snapshots.py
"""

from _common import banner, model

from strands import Agent
from tests.fakes import text

banner("13 snapshots")

before = Agent(model=model([[text("Noted: your board is CSCI.")]]), callback_handler=None)
before("My board is CSCI.")
snap = before.take_snapshot(preset="session", app_data={"route": "query"})
print("schema:", snap.schema_version, "| fields:", sorted(snap.data), "| app_data:", snap.app_data)

# ... the process restarts: a brand-new agent, restored from the snapshot ...
after = Agent(model=model([[text("You said your board is CSCI.")]]), callback_handler=None)
after.load_snapshot(snap)
print("restored messages:", len(after.messages))
print("answer:", str(after("Which board did I say?")).strip())
