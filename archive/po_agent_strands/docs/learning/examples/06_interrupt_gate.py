"""Lesson 6 — interrupts: a human-approval gate in 20 lines.

The hook calls event.interrupt(...). The first time, that raises: the agent
stops with stop_reason="interrupt". You resume by calling the agent with
interruptResponse blocks; the hook runs again and interrupt() now RETURNS the
answer. Setting event.cancel refuses the whole tool batch.
This is a minimal pmagent/gate.py.

    uv run docs/learning/examples/06_interrupt_gate.py [--live] [--ask]
    (--ask prompts you; otherwise it approves once and rejects once)
"""

import sys

from _common import banner, model

from strands import Agent, tool
from strands.hooks import BeforeToolsEvent, HookProvider
from tests.fakes import call, text

banner("06 interrupt gate")
WRITES = {"post_comment"}


@tool
def post_comment(issue: str, comment: str) -> str:
    """Post a comment on an issue. (Pretend write — prints only.)"""
    print(f"    *** post_comment ran: {issue}: {comment!r}")
    return "posted"


class Gate(HookProvider):
    def register_hooks(self, registry, **kwargs):
        registry.add_callback(BeforeToolsEvent, self.check)

    def check(self, event):
        uses = [b["toolUse"] for b in event.message["content"] if "toolUse" in b]
        writes = [u for u in uses if u["name"] in WRITES]
        if not writes:
            return
        answer = event.interrupt("approval", reason=writes)  # raises the 1st time
        if answer != "yes":
            event.cancel = "The user rejected this. Nothing was written."


def closing(messages):
    """The scripted model's second turn answers what actually happened:
    a cancelled batch comes back as an error result carrying the gate's text."""
    result = messages[-1]["content"][0]["toolResult"]
    if result["status"] == "error":
        return [text("Understood — nothing was posted.")]
    return [text("Posted.")]


def run(prompt, decide):
    agent = Agent(
        model=model([
            [call("post_comment", issue="CSCI-1", comment="nudge")],
            closing,
        ]),
        tools=[post_comment],
        hooks=[Gate()],
        callback_handler=None,
    )
    result = agent(prompt)
    while result.stop_reason == "interrupt":
        for i in result.interrupts:
            print("  PAUSED before:", [(u["name"], u["input"]) for u in i.reason])
        answer = decide()
        result = agent([
            {"interruptResponse": {"interruptId": i.id, "response": answer}}
            for i in result.interrupts
        ])
    print("  final:", str(result).strip())


if "--ask" in sys.argv:
    ask = lambda: input("  approve? yes/no: ").strip()   # noqa: E731
    for n in (1, 2):
        print(f"Run {n} (your choice):")
        run("Comment 'nudge' on CSCI-1.", ask)
else:
    print("Run 1 (approve):")
    run("Comment 'nudge' on CSCI-1.", lambda: "yes")
    print("\nRun 2 (reject):")
    run("Comment 'nudge' on CSCI-1.", lambda: "no")
