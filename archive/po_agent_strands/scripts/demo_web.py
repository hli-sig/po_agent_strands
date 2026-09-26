"""
The web UI, fully offline: real app, real agents, scripted model, fake Jira.

    uv run scripts/demo_web.py            # http://127.0.0.1:8000
    uv run scripts/demo_web.py --port 8081

No API key, no Jira, and nothing is written anywhere. It is the safe way to do
lessons 10–11: the real FastAPI app, `PMAssistant`, Strands agents, approval
gate and thinking chain, with a ScriptedModel instead of an LLM and made-up
issues instead of Jira. It answers three prompts (the help panel's examples
aren't among them — type these):

    Is the FX epic at risk?                    → sprint lane: reasoning + a tool call
    Post a comment on DEMO-101                 → ticket lane: the approval card
    Write a PRD from these notes: ...          → requirements lane: writer ↔ reviewer loop

Anything else gets a reply listing them. `scripts/course_figures.py` screenshots
this same demo for the course page.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

import pmagent.tools.jira.tools_read as tools_read  # noqa: E402
import pmagent.tools.jira.tools_write as tools_write  # noqa: E402
from pmagent import env  # noqa: E402
from pmagent.assistant import PMAssistant  # noqa: E402
from pmagent.schemas import PRD, ReviewResult  # noqa: E402
from pmagent.web.app import create_app  # noqa: E402
from tests.fakes import ScriptedModel, call, reasoning, structured, text  # noqa: E402

ISSUES = [
    {"key": "DEMO-101", "type": "Story", "status": "Blocked", "created": "2026-09-01",
     "story_points": 8, "assignee": "Ada Example", "summary": "Load FX rates into the raw layer"},
    {"key": "DEMO-102", "type": "Story", "status": "In Progress", "created": "2026-09-02",
     "story_points": 5, "assignee": "Sam Sample", "summary": "Model daily FX in the gold layer"},
    {"key": "DEMO-103", "type": "Task", "status": "To Do", "created": "2026-09-03",
     "story_points": 3, "assignee": None, "summary": "Late-feed alert to Slack"},
]


class DemoJira:
    """Stands in for JiraClient: made-up issues, comments recorded in memory only."""

    comments: list = []

    def search_issues_page(self, jql, max_results=50, include_changelog=False):
        return ISSUES, False

    def add_comments(self, keys, comment):
        DemoJira.comments.append((keys, comment))
        return [{"key": k, "error": None} for k in keys]


def _after_decision(messages) -> list[dict]:
    """The comment turn's second model call: answer what actually happened."""
    last = messages[-1]["content"][0].get("toolResult", {})
    if last.get("status") == "error":
        return [text("Understood — nothing was posted. What should the comment say instead?")]
    return [text("Posted the comment on DEMO-101.")]


def scripts() -> dict[str, list]:
    """Fresh scripts per conversation, keyed by a word in its first message."""
    return {
        "risk": [
            [reasoning("**Scoping the question**\n\nThe user wants the delivery risk in the FX epic. "
                       "I should list the open issues first rather than guess."),
             call("query_jira_issues", jql='project = DEMO AND text ~ "FX" AND statusCategory != Done')],
            [reasoning("**Weighing blockers against unstarted work**\n\nDEMO-101 is blocked and "
                       "carries 8 of 16 points; DEMO-103 is unassigned but small."),
             text("**The blocker is the bigger risk.** DEMO-101 (8 pts, Ada Example) is blocked and "
                  "holds half the epic's points; everything downstream waits on it. DEMO-103 is "
                  "unassigned but only 3 points — assign it this week.")],
        ],
        "comment": [
            [call("add_jira_comment", issue_keys=["DEMO-101"],
                  comment="What is blocking this? Can we pair on it tomorrow?")],
            _after_decision,
        ],
        "prd": [
            [structured(PRD, title="Daily FX feed", objective="Daily AUD-base FX rates in Snowflake by 7am.",
                        owner="Data platform",
                        requirements=[{"user_story": "As a finance analyst, I want daily FX rates by 7am, "
                                                     "so that month-end reports use current rates.",
                                       "importance": "High"}])],
            [structured(ReviewResult, approved=False, missing_requirements=["Alert when the feed is late"],
                        revision_notes="Add the late-feed alert from the notes.")],
            [structured(PRD, title="Daily FX feed", objective="Daily AUD-base FX rates in Snowflake by 7am.",
                        owner="Data platform",
                        requirements=[
                            {"user_story": "As a finance analyst, I want daily FX rates by 7am, so that "
                                           "month-end reports use current rates.", "importance": "High"},
                            {"user_story": "As the platform on-call, I want an alert when the feed is "
                                           "late, so that I can fix it before 7am.", "importance": "High"}],
                        open_questions=[{"question": "Which Slack channel receives the alert?"}])],
            [structured(ReviewResult, approved=True)],
        ],
    }


ROUTES = {"risk": "sprint", "comment": "ticket", "prd": "requirements"}
UNKNOWN = ("This is the offline demo, with a scripted model. Try one of: “Is the FX epic at risk?”, "
           "“Post a comment on DEMO-101”, or “Write a PRD from these notes: …”. "
           "For the real assistant, run `uv run scripts/smoke.py --web` (writes blocked); plain "
           "`uv run web.py` only when your `.env` is not a production Jira.")


def _key(messages) -> str | None:
    latest = [m for m in messages if m["role"] == "user" and "text" in m["content"][0]][-1]
    words = latest["content"][0]["text"].lower()
    return next((k for k in ROUTES if k in words), None)


def factory(recorder):
    queue = scripts()

    def next_turn(messages):
        key = _key(messages)
        if key is None or not queue[key]:
            return [text(UNKNOWN)]
        turn = queue[key].pop(0)
        return turn(messages) if callable(turn) else turn

    model = ScriptedModel([next_turn] * 1000)
    return PMAssistant(model=model, classify=lambda m, p: ROUTES.get(_key(m), "query"),
                       **recorder.assistant_kwargs())


def configure() -> None:
    """Point the app at the demo: fake Jira, and a header that says 'demo'."""
    env.LLM_PROVIDER, env.LLM_MODEL, env.JIRA_PROJECT_KEY = "openai", "demo (scripted model)", "DEMO"
    env.JIRA_BASE_URL = None          # no real tenant is involved, so /meta must not name one
    env.LLM_REASONING_EFFORT, env.LLM_REASONING_SUMMARY = "high", "detailed"
    tools_read.get_client = lambda: DemoJira()
    tools_write.get_client = lambda: DemoJira()


def start_in_background(port: int = 0) -> tuple[uvicorn.Server, str]:
    """Start the demo server on a thread (used by scripts/course_figures.py)."""
    configure()
    srv = uvicorn.Server(uvicorn.Config(create_app(factory), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.02)
    return srv, f"http://127.0.0.1:{srv.servers[0].sockets[0].getsockname()[1]}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PM Agent web UI — offline demo")
    parser.add_argument("--port", type=int, default=8000)
    # The demo has no credentials and fake data, so binding 0.0.0.0 is safe. The
    # Docker image does, because a container's loopback is unreachable from the host.
    parser.add_argument("--host", default="127.0.0.1", help="Interface to bind (0.0.0.0 in Docker).")
    args = parser.parse_args(argv)
    configure()
    print(f"Offline demo → http://127.0.0.1:{args.port}   (scripted model, fake Jira, nothing is written)")
    print("Try: “Is the FX epic at risk?” · “Post a comment on DEMO-101” · “Write a PRD from these notes: …”")
    uvicorn.Server(uvicorn.Config(create_app(factory), host=args.host, port=args.port,
                                  log_level="warning")).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
