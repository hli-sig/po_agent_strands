"""Lessons 4 & 7 — many agents, one conversation (the lanes pattern).

Each lane is its own Agent (own prompt, own tools). Pointing them all at the
same messages list gives the ticket lane memory of what the query lane found.
This is exactly what pmagent/assistant.py does.

    uv run docs/learning/examples/04_shared_history.py [--live]
"""

from _common import banner, model

from strands import Agent
from tests.fakes import text

banner("04 shared history")

m = model([[text("CSCI-5 is open and unassigned.")], [text("Drafted a follow-up for CSCI-5.")]])
shared: list[dict] = []

query_lane = Agent(model=m, system_prompt="You answer read-only questions.", callback_handler=None)
ticket_lane = Agent(model=m, system_prompt="You draft Jira tickets.", callback_handler=None)

for lane, prompt in [(query_lane, "Is CSCI-5 open?"), (ticket_lane, "Draft a follow-up for it.")]:
    lane.messages = shared          # every lane works on the one conversation
    print(f"{lane.system_prompt!r:35} -> {str(lane(prompt)).strip()}")
    shared = lane.messages

print(f"\n{len(shared)} messages in the one shared history:")
for msg in shared:
    print(f"  {msg['role']:9} {msg['content'][0]['text']}")
