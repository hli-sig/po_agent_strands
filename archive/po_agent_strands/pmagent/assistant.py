"""
PMAssistant — the application: one conversation, seven lanes, one approval gate.

    user turn ─► router.classify ─► lane ──► Strands Agent (prompt + tools + ApprovalGate)
                                     │            │
                                     │            └─ write? ─► stop_reason="interrupt" ─► CLI asks ─► resume()
                                     └─ "requirements" ─► writer ↔ reviewer loop (no tools, no gate)

    lanes: ticket_agent, sprint_agent, query_agent, spreadsheet_agent,
           finance_agent, diagram_agent, requirements

This replaces the LangGraph original's `graph.py`. A few things worth seeing:

**One conversation, many agents.** Each lane is its own Strands `Agent` (its own
system prompt and tools), but they all share ONE message list, `self.messages`.
Before a lane runs, its agent is pointed at that list (`agent.messages =
self.messages`); Strands appends to it in place. So the ticket lane sees what the
query lane found a turn earlier, exactly as the original's single `PMState.messages`
did.

**Interrupts belong to an agent.** When a lane's ApprovalGate pauses, that
specific Agent object holds the paused state (privately). `send()` returns the
pending write calls; the caller must then call `resume(approved)`, which answers
the interrupt on the *same* agent. If the approval is abandoned instead (a
crash, a new message, /new), `discard_pending()` closes the dangling tool call in
the history with an explicit "not executed" result and throws the paused agent
away — a Strands agent with an unanswered interrupt refuses any other input, and
rebuilding it is the only public way to clear that state.

**No framework for the routing itself.** Classify, look up the lane, run it.
Plain Python is the clearest orchestrator when the control flow is this simple.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from strands import Agent
from strands.hooks import HookProvider
from strands.models import Model

from pmagent.agents import (
    diagram_agent,
    finance_agent,
    query_agent,
    router,
    spreadsheet_agent,
    sprint_agent,
    ticket_agent,
)
from pmagent.agents.common import make_lane_agent
from pmagent.agents.requirements import run_requirements
from pmagent.gate import APPROVE, REJECT
from pmagent.llm import invocation_usage
from pmagent.messages import assistant_message, tool_results, tool_uses, user_message
from pmagent.tools.mcp_tools import MCPSession

# What an abandoned approval leaves in the history for the unanswered tool calls.
ABANDONED_MESSAGE = (
    "Not executed — the approval for this action was abandoned before the user "
    "answered. Nothing was written."
)


@dataclass
class TurnResult:
    route: str
    pending: list[dict] = field(default_factory=list)
    """Write calls awaiting approval ({"id", "name", "args"}); empty when the turn is done."""
    reply: str = ""
    """Text the caller must print itself. Only the requirements lane sets this —
    lane agents' output is printed live by hooks (see cli/echo.py)."""
    route_reason: str = ""
    """Why this route: "continuation" (fast path, no model call), "classifier",
    or "custom" (an injected classify function)."""
    usage: dict = field(default_factory=dict)
    """Token usage of every model call this turn so far — router, lane, PRD
    writer/reviewer, diagram brief. Cumulative across send() and resume()."""


def _empty_usage() -> dict:
    return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "model_calls": 0}


Classifier = Callable[[list[dict], str], str]


class PMAssistant:
    def __init__(
        self,
        model: Model | None = None,
        *,
        classify: Classifier | None = None,
        hooks: list[HookProvider] | tuple = (),
        mcp: MCPSession | None = None,
        on_route: Callable[[str], None] | None = None,
        on_route_decided: Callable[[str, str], None] | None = None,
        on_prd_step: Callable[[str, dict], None] | None = None,
        callback_handler=None,
    ) -> None:
        """
        Args:
            model: The Strands model every agent uses. Defaults to `llm.get_model()`.
            classify: `(messages, previous_route) -> route`. Defaults to the
                LLM router; tests pass a stub.
            hooks: Extra hooks for every lane agent (the CLI's ConsoleEcho).
            mcp: A started MCP session whose tools join the diagram lane.
            on_route: Called with the route as soon as a turn is classified,
                before the lane runs — so the CLI can print it first.
            on_route_decided: Like `on_route`, but also given the reason
                (see `TurnResult.route_reason`). Used by the web thinking chain.
            on_prd_step: Receives the requirements loop's progress
                (`requirements.run_requirements_loop`).
            callback_handler: A Strands callback handler for every lane agent —
                how the web server streams text and reasoning deltas.
        """
        self._model = model
        self._custom_classify = classify
        self._hooks = list(hooks)
        self._on_route_decided = on_route_decided
        self._on_prd_step = on_prd_step
        self._callback_handler = callback_handler
        self._usage_lock = threading.Lock()
        self._turn_usage = _empty_usage()
        self._route_reason = ""
        self._mcp = mcp
        self._on_route = on_route
        self._agents: dict[str, Agent] = {}
        self._pending_lane: str | None = None
        self._pending_interrupts: list = []
        self.messages: list[dict] = []
        self.route = ""
        self.last_prd = None

        mcp_tools = mcp.tools if mcp else []
        # Every tool-using lane, declared as (system prompt, tools).
        self.lanes: dict[str, tuple[str, list]] = {
            "ticket_agent": (ticket_agent.SYSTEM_PROMPT, ticket_agent.TOOLS),
            "sprint_agent": (sprint_agent.SYSTEM_PROMPT, sprint_agent.TOOLS),
            "query_agent": (query_agent.SYSTEM_PROMPT, query_agent.TOOLS),
            "spreadsheet_agent": (spreadsheet_agent.SYSTEM_PROMPT, spreadsheet_agent.TOOLS),
            "finance_agent": (finance_agent.SYSTEM_PROMPT, finance_agent.TOOLS),
            "diagram_agent": (
                diagram_agent.system_prompt(lucid_connected=bool(mcp_tools)),
                diagram_agent.build_tools(mcp_tools),
            ),
        }

    # -- public API ----------------------------------------------------------

    @property
    def pending(self) -> bool:
        return self._pending_lane is not None

    def send(self, text: str) -> TurnResult:
        """Handle one user message. Returns pending writes if the lane paused."""
        if self.pending:
            self.discard_pending()

        self._turn_usage = _empty_usage()
        route, reason = self._decide_route(self.messages + [user_message(text)])
        self.route, self._route_reason = route, reason
        if self._on_route:
            self._on_route(route)
        if self._on_route_decided:
            self._on_route_decided(route, reason)

        lane = router.route_to_lane(route)
        if lane == "requirements":
            return self._run_requirements(text)
        return self._run_lane(lane, text)

    def resume(self, approved: bool) -> TurnResult:
        """Answer the pending approval and let the paused lane carry on."""
        if not self.pending:
            raise RuntimeError("No approval is pending.")
        answer = APPROVE if approved else REJECT
        responses = [
            {"interruptResponse": {"interruptId": interrupt.id, "response": answer}}
            for interrupt in self._pending_interrupts
        ]
        # If this raises, the approval stays pending so the caller can
        # discard_pending() and leave the lane usable.
        return self._run_lane(self._pending_lane, responses)

    def discard_pending(self) -> None:
        """Abandon an unanswered approval. Safe to call at any time, any number of times."""
        lane = self._pending_lane
        self._pending_lane = None
        self._pending_interrupts = []
        if lane is None:
            return
        _close_unanswered_tool_calls(self.messages)
        # The paused agent refuses anything but an interrupt response; its
        # interrupt state is private, so replace the agent. Built again lazily.
        self._agents.pop(lane, None)

    def reset(self) -> None:
        """Start a fresh conversation (/new)."""
        self.discard_pending()
        self._agents.clear()
        self.messages = []
        self.route = ""
        self.last_prd = None

    def close(self) -> None:
        if self._mcp:
            self._mcp.close()

    # -- internals -----------------------------------------------------------

    def _decide_route(self, messages: list[dict]) -> tuple[str, str]:
        if self._custom_classify:
            return self._custom_classify(messages, self.route), "custom"
        return router.classify_with_reason(
            messages, self.route, self._model, usage_sink=self._add_usage
        )

    def _add_usage(self, usage: dict) -> None:
        """Usage sink. Tools call it from Strands' worker thread, hence the lock."""
        with self._usage_lock:
            for key in self._turn_usage:
                self._turn_usage[key] += usage.get(key, 0)

    def _result(self, **kwargs) -> TurnResult:
        with self._usage_lock:
            usage = dict(self._turn_usage)
        return TurnResult(route=self.route, route_reason=self._route_reason, usage=usage, **kwargs)

    def agent(self, lane: str) -> Agent:
        """The lane's agent, built on first use."""
        if lane not in self._agents:
            system_prompt, tools = self.lanes[lane]
            self._agents[lane] = make_lane_agent(
                lane, system_prompt, tools, model=self._model, hooks=self._hooks,
                callback_handler=self._callback_handler,
            )
        return self._agents[lane]

    def _run_lane(self, lane: str, prompt) -> TurnResult:
        agent = self.agent(lane)
        agent.messages = self.messages  # share the one conversation
        try:
            # STRANDS CONCEPT — invocation_state travels with this call to every
            # tool (tool_context.invocation_state); the diagram brief reports its
            # token usage through it.
            result = agent(prompt, invocation_state={"usage_sink": self._add_usage})
        finally:
            self.messages = agent.messages
        self._add_usage(invocation_usage(result))

        if result.stop_reason == "interrupt":
            self._pending_lane = lane
            self._pending_interrupts = list(result.interrupts or [])
            calls = [
                call
                for interrupt in self._pending_interrupts
                for call in (interrupt.reason or {}).get("calls", [])
            ]
            return self._result(pending=calls)

        self._pending_lane = None
        self._pending_interrupts = []
        return self._result()

    def _run_requirements(self, text: str) -> TurnResult:
        result = run_requirements(
            text, model=self._model, on_step=self._on_prd_step, usage_sink=self._add_usage
        )
        self.last_prd = result.prd
        # Added outside any Agent, so no hook sees these — hence TurnResult.reply.
        self.messages.append(user_message(text))
        self.messages.append(assistant_message(result.reply))
        return self._result(reply=result.reply)


def _close_unanswered_tool_calls(messages: list[dict]) -> None:
    """Give every unanswered tool call in the last assistant turn an explicit result.

    Keeps the toolUse message itself (providers reject a result with no call, and
    a call with no result). Adds nothing if every call already has a result — e.g.
    when a resume failed *after* the tools ran.
    """
    last_assistant = next(
        (i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "assistant"),
        None,
    )
    if last_assistant is None:
        return
    wanted = [use["toolUseId"] for use in tool_uses(messages[last_assistant])]
    answered = {
        result["toolUseId"]
        for message in messages[last_assistant + 1:]
        for result in tool_results(message)
    }
    missing = [tool_use_id for tool_use_id in wanted if tool_use_id not in answered]
    if missing:
        messages.append({
            "role": "user",
            "content": [
                {"toolResult": {"toolUseId": tool_use_id, "status": "error",
                                "content": [{"text": ABANDONED_MESSAGE}]}}
                for tool_use_id in missing
            ],
        })
