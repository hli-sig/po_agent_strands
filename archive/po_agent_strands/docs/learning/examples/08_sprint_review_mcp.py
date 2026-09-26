"""Lesson 8, case study — an MCP server you own, behind the real approval gate.

Starts sprint_review/server.py (stdio) with a scratch --root holding the
synthetic sample-jira.csv, discovers its tools, and gives them to a lane agent
built by pmagent's own make_lane_agent. Three things to watch:

1. The server *annotates* two tools as read-only — and the gate still pauses on
   them. Annotations are hints; only APPROVED_READ_TOOLS makes a tool free.
2. The numbers in the final answer are the server's (analyser.py), not the
   model's. The model only quotes them.
3. Saving the same run twice is refused by the *server*: it never overwrites.

Every pause is approved automatically here, which is safe: the server can only
touch the scratch folder, deleted at the end.

    uv run docs/learning/examples/08_sprint_review_mcp.py [--live]
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

from _common import banner, model
from mcp import StdioServerParameters, stdio_client

from pmagent.agents.common import make_lane_agent
from pmagent.gate import APPROVE, requires_approval
from strands.tools.mcp import MCPClient
from tests.fakes import call, text

banner("08 case study: the sprint review MCP server")

HERE = Path(__file__).parent
SYSTEM_PROMPT = (
    "You prepare sprint review numbers. Get every count and percentage from the "
    "sprint-review tools and quote them exactly; never calculate one yourself. "
    "Name the denominator of any percentage and cite the export's sha256."
)
PROMPT = (
    "Sprint 35 review from sample-jira.csv. Sprint started 2026-09-01. Exclude "
    "unfinished cards for Alex but keep their Done cards. Save it as run sprint-35."
)
FILTERS = {"csv_name": "sample-jira.csv", "sprint_start": "2026-09-01",
           "exclude_unfinished_assignee_first_names": ["Alex"]}


def tool_results(messages):
    """Every toolResult block in the conversation, oldest first."""
    return [b["toolResult"] for m in messages for b in m["content"] if "toolResult" in b]


def summary(messages):
    """The scripted model's last turn: quote the server's numbers, don't compute them.

    It reads the analyze_sprint result (the first tool result) the same way a
    real model reads it: as JSON text returned by the server.
    """
    analysis = json.loads(tool_results(messages)[0]["content"][0]["text"])
    m, sha = analysis["metrics"], analysis["source"]["sha256"]
    return [text(f"Sprint 35: {m['done']} Done, {m['active_flow']} in active flow, {m['blocked']} "
                 f"Blocked. {m['done_or_active_pct_of_started']}% of started work "
                 f"({m['started_work']} cards) is Done or active. Saved as run sprint-35; "
                 f"source sha256 {sha[:12]}…")]


root = Path(tempfile.mkdtemp(prefix="sprint-review-"))
shutil.copy(HERE / "sprint_review" / "sample-jira.csv", root)
client = MCPClient(lambda: stdio_client(StdioServerParameters(
    command=sys.executable, args=["-m", "sprint_review.server", "--root", str(root)], cwd=HERE)))

client.start()
try:
    tools = client.list_tools_sync()  # one page is enough for 3 tools; see mcp_tools._list_all_tools
    agent = make_lane_agent(
        "sprint_review",
        SYSTEM_PROMPT,
        list(tools),
        model=model([
            [call("analyze_sprint", **FILTERS, include_evidence=False)],
            [call("save_metrics", run_name="sprint-35", **FILTERS)],
            [call("save_metrics", run_name="sprint-35", **FILTERS)],   # the same run again
            summary,
        ]),
    )
    print("discovered tools, the server's readOnlyHint, and what the gate does:")
    for t in tools:
        hint = t.tool_spec.get("annotations", {}).get("readOnlyHint")
        gated = requires_approval(t.tool_name, agent)
        print(f"  {t.tool_name:20} readOnlyHint={hint!s:5}  gated={gated}")

    result = agent(PROMPT)
    while result.stop_reason == "interrupt":
        for i in result.interrupts:
            print("  PAUSED before:", ", ".join(c["name"] for c in i.reason["calls"]), "→ approve")
        result = agent([{"interruptResponse": {"interruptId": i.id, "response": APPROVE}}
                        for i in result.interrupts])

    print("\nwhat came back from the server process:")
    for r in tool_results(agent.messages):
        print(f"  [{r['status']}]", r["content"][0]["text"].replace("\n", " ")[:110])
    print("\nfiles the server wrote:", sorted(p.relative_to(root).as_posix() for p in root.rglob("*.json")))
    print("answer:", str(result).strip())
finally:
    client.stop(None, None, None)
    shutil.rmtree(root, ignore_errors=True)
