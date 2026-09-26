"""
The one place a model provider is chosen, plus the one helper for stateless
structured-output calls.

STRANDS CONCEPT — a `Model` is a provider adapter. An `Agent` is
`model + system prompt + tools + conversation`, and it runs the tool loop
itself. Swapping Claude for GPT is swapping the `Model` object; nothing that
uses the agent changes. That is why this module exists and why it should stay
small.

LangGraph original: `get_llm()` returned a LangChain chat model, and callers did
`.bind_tools(...)` / `.with_structured_output(...)` on it. In Strands, tools are
given to the `Agent`, and structured output is a per-call argument — see
`structured()` below.

Usage:
    from pmagent.llm import get_model, structured
    agent = Agent(model=get_model(), tools=[...], system_prompt="...")
    decision = structured(RouteDecision, "Classify this: ...")
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cache
from typing import TypeVar

from pydantic import BaseModel
from strands import Agent
from strands.models import Model

from pmagent import env

T = TypeVar("T", bound=BaseModel)

@cache
def get_modol() -> Model:
    return build_model()


def reasoning_available() -> bool:
    """True when the configured model will stream reasoning the UI can show."""
    return (
        env.LLM_PROVIDER == "openai"
        and env.LLM_REASONING_EFFORT != "none"
        and bool(env.LLM_REASONING_SUMMARY)
    )


def build_model() -> Model:
    """Build a new model object for the configured provider.

    `get_model()` caches one per process, which is right for the CLI. The web
    server builds one per conversation for Anthropic, whose model object holds a
    single async HTTP client that should not be shared across the event loops of
    concurrently running conversations.

    Provider imports are lazy so you only need the SDK for the provider you use.
    """

    if env.LLM_PROVIDER == "anthropic":
        