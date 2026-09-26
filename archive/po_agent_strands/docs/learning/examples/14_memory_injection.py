"""Lesson 14 — long-term memory: a custom MemoryStore + MemoryManager injection.

Shows the three facts lesson 14 builds on:
  1. a store is any object with name/description/max_search_results/writable/
     extraction and an async search();
  2. MemoryManager injects matching entries into the MODEL INPUT each call...
  3. ...without writing them into agent.messages (the durable history).

    uv run docs/learning/examples/14_memory_injection.py
"""

from _common import banner, model

from strands import Agent
from strands.memory import MemoryEntry, MemoryManager
from tests.fakes import text

banner("14 memory injection")


class TinyStore:
    """Confirmed facts only; keyword search. (Your FactStore adds files, provenance, delete.)"""

    name = "pm"
    description = "Facts the user stated"
    max_search_results = 5
    writable = False
    extraction = None

    def __init__(self, facts):
        self.facts = facts

    async def search(self, query, options=None):
        words = set(query.lower().split())
        return [MemoryEntry(content=f) for f in self.facts if words & set(f.lower().split())]


m = model([[text("Ada owns FX, so I'd assign it to Ada.")]])
agent = Agent(model=m, callback_handler=None,
              memory_manager=MemoryManager(stores=[TinyStore(["Ada owns FX ingestion"])]))
agent("Who should I assign the FX bug to?")

seen = m.requests[-1]["messages"][-1]["content"]
print("what the MODEL saw (last user message):")
for block in seen:
    print("   ", (block.get("text") or "")[:120].replace("\n", " "))
print("\nwhat the HISTORY kept:", agent.messages[0]["content"])
print("tools the agent got:", agent.tool_names)
