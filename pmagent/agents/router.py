"""
The router: decides which lane handles each user turn.

Two rules make multi-turn workflows survive per-turn routing (unchanged from the
LangGraph original, which learned them the hard way):

* **A short confirmation reuses the previous route with no model call.** The turn
  that matters most — "yes, create them" — has no topic of its own. Classified
  alone, it once sent a confirmed batch of ticket drafts to the read-only query
  lane, which correctly said it could not create anything.
* **Otherwise the classifier sees recent conversation**, not just the last
  message, plus the previous route.

STRANDS CONCEPT — classification is a structured-output call
(`llm.structured(RouteDecision, ...)`): the model *must* return one of the
`RouteDecision.route` literals, validated by Pydantic. No free-text parsing.

LangGraph original: `agents/orchestrator.py` — `classify_node` wrote
`state.route` and `route_from_classifier` was a conditional edge. Here they are
two plain functions that `PMAssistant` calls.
"""

from __future__ import annotations

import re

from strands.models import Model

from pmagent.llm import structured
from pmagent.messages import is_tool_result, last_user_text, text_of, tool_uses
from pmagent.schemas import RouteDecision

# How many recent messages to show the classifier. Enough for "draft these" ->
# "yes, do it" to be visible in one window, small enough to stay cheap.
_CONTEXT_MESSAGES = 6

# A continuation is a reply that only makes sense against the previous turn: an
# approval, a rejection, or a nudge. It has no topic of its own, so classifying
# it alone is what caused the misroute this guard exists to prevent.
#
# The test is *whole-message*, not prefix: every word must come from this
# vocabulary. A prefix match would swallow "yes, and also draft a ticket for the
# FX pipeline" — which starts as a confirmation but introduces new work and must
# be classified afresh.
_CONTINUATION_WORDS = frozenset(
    """
    y ye yes yep yeah yup ya ok okay k sure fine
    confirm confirmed confirming confirmation approve approved approval
    create creation created make it them these those all both
    do go ahead proceed continue send ship submit push
    please thanks thank you now lgtm looks good correct right agreed exactly
    n no nope nah cancel stop dont abort reject rejected hold wait
    """.split()
)

# Anything longer is carrying its own content, whatever words it uses.
_CONTINUATION_MAX_CHARS = 60

_WORD = re.compile(r"[a-z']+")


def is_continuation(text: str) -> bool:
    """True when a message only makes sense as a reply to the previous turn.

    Deliberately conservative — every word must be pure confirmation vocabulary.
    "confirm creation" and "yes please" qualify; "yes, also draft a ticket for
    FX" does not, because it introduces new work that deserves fresh
    classification.

    This is a fast path, not the whole fix. Anything it doesn't recognise — a
    typo like "conifrm creation", or a phrasing nobody anticipated — falls
    through to the classifier, which now sees the recent conversation and the
    previous route and can work it out. A guard that guesses is worse than one
    that abstains.
    """
    stripped = text.strip()
    if not stripped or len(stripped) > _CONTINUATION_MAX_CHARS:
        return False

    words = _WORD.findall(stripped.lower())
    if not words:
        return False
    return all(word in _CONTINUATION_WORDS for word in words)


def recent_context(messages: list[dict]) -> str:
    """Render the tail of the conversation for the classifier prompt.

    Strands shape (see `pmagent/messages.py`): a tool result is a `user` message,
    so it is labelled "Tool result:" — never "User:", which would make the
    classifier route on tool output as if the human had typed it. An assistant
    turn that only called tools shows the tool names, since "it just called
    create_jira_issues" is a strong routing signal.

    The window is the last 6 *messages*. One tool round-trip is 2 messages in
    Strands (1 + N in LangChain), so this covers at least as much conversation
    as the original did.
    """
    lines = []
    for message in messages[-_CONTEXT_MESSAGES:]:
        if is_tool_result(message):
            role = "Tool result"
            text = " ".join(
                block.get("text", "")
                for result in (b["toolResult"] for b in message["content"] if "toolResult" in b)
                for block in result.get("content") or []
                if "text" in block
            ).strip()
        else:
            role = {"user": "User", "assistant": "Assistant"}.get(message.get("role"), message.get("role"))
            text = text_of(message)
            if not text:
                calls = tool_uses(message)
                if calls:
                    text = "(called tools: " + ", ".join(c["name"] for c in calls) + ")"
        if text:
            lines.append(f"{role}: {text[:600]}")
    return "\n".join(lines)


def classify_with_reason(
    messages: list[dict],
    previous_route: str = "",
    model: Model | None = None,
    usage_sink=None,
) -> tuple[str, str]:
    """Return `(route, reason)` for the latest user message.

    `reason` is `"continuation"` when the pure-Python fast path kept the previous
    route (no model call), or `"classifier"` when the model decided. The web UI
    shows it in the thinking chain; the CLI ignores it.
    """
    user_text = last_user_text(messages)

    # A confirmation belongs to the workflow already in progress. Route
    # stickiness is deterministic here on purpose: asking the model to classify
    # "yes" is asking it to guess.
    if previous_route and is_continuation(user_text):
        return previous_route, "continuation"

    previous = (
        f"The previous turn was handled by the '{previous_route}' route. Stay on it "
        "unless this request has genuinely moved on to something else.\n\n"
        if previous_route
        else ""
    )
    decision = structured(
        RouteDecision,
        "Classify this project-management request into exactly one route.\n\n"
        f"{previous}"
        f"Recent conversation:\n{recent_context(messages)}\n\n"
        f"Classify this latest request: {user_text}",
        model=model,
        usage_sink=usage_sink,
    )
    return decision.route, "classifier"


def classify(messages: list[dict], previous_route: str = "", model: Model | None = None) -> str:
    """Return the route for the latest user message in `messages`."""
    return classify_with_reason(messages, previous_route, model)[0]


# Route -> lane name. Every route in `RouteDecision` must appear here — leaving
# one out once raised KeyError and took the whole app down mid-conversation.
# Unknown routes fall back to the read-only query lane, the safe default.
ROUTE_TO_LANE = {
    "ticket": "ticket_agent",
    "sprint": "sprint_agent",
    "spreadsheet": "spreadsheet_agent",
    "finance": "finance_agent",
    "requirements": "requirements",
    "diagram": "diagram_agent",
    "query": "query_agent",
}


def route_to_lane(route: str) -> str:
    return ROUTE_TO_LANE.get(route, "query_agent")
