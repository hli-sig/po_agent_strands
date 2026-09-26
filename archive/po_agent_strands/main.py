"""
CLI for the PM Agent — the Strands rewrite.

    uv run main.py

A thin adapter over `pmagent.assistant.PMAssistant`: it owns terminal I/O and
nothing else, so another frontend (Slack, web) could drive the same assistant.

The one piece of real interaction logic is the approval loop. Every lane agent
carries an `ApprovalGate` hook, so a turn that wants to write *stops* before the
write and comes back here with the pending calls. Approving resumes the agent;
rejecting resumes it with a refusal it must respond to. The model cannot skip
this — it is enforced by the agent loop, not by the prompt.
"""

from __future__ import annotations

import logging
import os
import sys

from strands.types.exceptions import ContextWindowOverflowException

from pmagent import env
from pmagent.assistant import PMAssistant, TurnResult
from pmagent.cli.approval import describe_write, unchecked_scope_warning
from pmagent.cli.echo import BOLD, DIM, GREEN, RED, RESET, YELLOW, ConsoleEcho

BANNER = """\
PM Agent — Jira assistant (Strands edition)
  Ask about sprints, search Jira, draft and create tickets, or build the
  FY budget Snowflake-ingest CSV from Finance's workbooks.
  Anything that writes always pauses for your approval first.

  /new    start a fresh conversation
  /help   show this again
  /exit   quit
"""


class _RoutePrinter:
    """Prints `[route: x]` when the lane changes.

    Not decoration: which lane you are in decides which tools exist, so "the
    agent says it can't create a ticket" is usually "this turn went to the
    read-only query lane". Without this line that failure looks like a refusal.
    """

    def __init__(self) -> None:
        self.last: str | None = None

    def __call__(self, route: str) -> None:
        if route != self.last:
            self.last = route
            print(f"{DIM}  [route: {route}]{RESET}")


def resolve_approvals(assistant: PMAssistant, result: TurnResult, ask=input) -> bool:
    """Ask about every pending write until the turn finishes. False if the user quit."""
    while result.pending:
        print(f"\n{YELLOW}{'-' * 60}{RESET}")
        print(f"{YELLOW}APPROVAL REQUIRED — the agent wants to make a change{RESET}")
        for call in result.pending:
            print(f"\n  {describe_write(call)}")
        warning = unchecked_scope_warning(assistant.messages, result.pending)
        if warning:
            print(f"\n  {RED}{warning}{RESET}")
        print(f"{YELLOW}{'-' * 60}{RESET}")

        try:
            answer = ask(f"{BOLD}Approve? [y/N]{RESET} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            assistant.discard_pending()
            return False

        approved = answer in ("y", "yes")
        print(f"{GREEN}Approved.{RESET}" if approved else f"{RED}Rejected — nothing was written.{RESET}")
        result = assistant.resume(approved)
    return True


def handle_turn(assistant: PMAssistant, user_input: str, ask=input) -> bool:
    """Run one user turn end to end. Returns False if the user quit mid-approval."""
    try:
        result = assistant.send(user_input)
        if result.reply:
            print(f"\n{BOLD}Agent{RESET}: {result.reply}")
        return resolve_approvals(assistant, result, ask)
    except ContextWindowOverflowException:
        assistant.discard_pending()
        print(f"\n{RED}The conversation is too long for the model's context window.{RESET} "
              "Type /new to start a fresh one.")
    except Exception as exc:  # noqa: BLE001 — a CLI should not die on one bad turn
        # An approval left hanging would wedge that lane; close it out first.
        assistant.discard_pending()
        print(f"\n{RED}Error:{RESET} {type(exc).__name__}: {exc}")
    return True


def build_assistant(on_route) -> PMAssistant:
    mcp = None
    if env.LUCID_MCP_ENABLED:
        from pmagent.tools.mcp_tools import start_lucid

        mcp = start_lucid()
    return PMAssistant(hooks=[ConsoleEcho()], mcp=mcp, on_route=on_route)


def _quiet_library_logs() -> None:
    """Keep Strands' own log lines out of the chat.

    With no logging configured, Python prints WARNING+ records to stderr — so
    Strands' informational warnings (e.g. "Moving image from tool message to a
    new user message for OpenAI compatibility") would interleave with the
    conversation. Errors still surface through the CLI's own error line. Set
    PMAGENT_LOG_LEVEL=DEBUG to see everything Strands does.
    """
    level = os.getenv("PMAGENT_LOG_LEVEL", "CRITICAL").upper()
    logging.basicConfig(format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("strands").setLevel(level)


def main() -> int:
    _quiet_library_logs()
    try:
        env.validate()
        env.validate_jira()
    except ValueError as exc:
        print(f"{RED}Configuration error:{RESET} {exc}")
        return 1

    print(BANNER)
    print(f"{DIM}Jira: {env.JIRA_BASE_URL}  project: {env.JIRA_PROJECT_KEY}{RESET}")
    print(f"{DIM}Model: {env.LLM_PROVIDER} / {env.LLM_MODEL}{RESET}\n")

    route_printer = _RoutePrinter()
    assistant = build_assistant(route_printer)
    try:
        while True:
            try:
                user_input = input(f"\n{BOLD}You{RESET}: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nBye.")
                return 0

            if not user_input:
                continue
            if user_input in ("/exit", "/quit"):
                print("Bye.")
                return 0
            if user_input == "/help":
                print(BANNER)
                continue
            if user_input == "/new":
                assistant.reset()
                route_printer.last = None
                print(f"{DIM}Started a new conversation.{RESET}")
                continue

            if not handle_turn(assistant, user_input):
                return 0
    finally:
        assistant.close()


if __name__ == "__main__":
    sys.exit(main())
