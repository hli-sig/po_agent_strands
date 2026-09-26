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
def get_model() -> Model:
    """Return the configured model, built once per process (see `build_model`)."""
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
        from strands.models.anthropic import AnthropicModel

        # Current Claude models reject temperature/top_p/top_k, so none are sent.
        # Strands requires max_tokens for Anthropic.
        return AnthropicModel(
            client_args={"api_key": env.ANTHROPIC_API_KEY},
            model_id=env.LLM_MODEL,
            max_tokens=env.LLM_MAX_TOKENS,
        )

    if env.LLM_PROVIDER == "openai":
        effort = env.LLM_REASONING_EFFORT
        client_args = {"api_key": env.OPENAI_API_KEY}

        if effort == "none":
            from strands.models.openai import OpenAIModel

            # gpt-5.x rejects function tools on /v1/chat/completions unless
            # reasoning is explicitly off (verified: HTTP 400 "Function tools with
            # reasoning_effort are not supported ..."). Every lane has tools, and
            # Strands' structured output is itself a tool, so this is mandatory.
            # Sending reasoning_effort also re-enables temperature on gpt-5.
            # Do not add max_tokens: gpt-5 rejects it on this endpoint.
            return OpenAIModel(
                client_args=client_args,
                model_id=env.LLM_MODEL,
                params={"reasoning_effort": "none", "temperature": 0.1},
            )

        # Any real reasoning effort needs the Responses API, which supports tools
        # and reasoning together. Note: Strands does not carry reasoning content
        # between turns on this path.
        from strands.models.openai_responses import OpenAIResponsesModel

        reasoning = {"effort": effort}
        if env.LLM_REASONING_SUMMARY:
            # Streams as `reasoningText` through Strands' callback handler —
            # the web UI's thinking chain shows it (verified live on gpt-5.6-luna
            # with effort=medium, summary=detailed).
            reasoning["summary"] = env.LLM_REASONING_SUMMARY
        return OpenAIResponsesModel(
            client_args=client_args,
            model_id=env.LLM_MODEL,
            params={"reasoning": reasoning},
        )

    raise ValueError(
        f"Unknown LLM_PROVIDER={env.LLM_PROVIDER!r}. Use 'anthropic' or 'openai'."
    )


def structured(
    schema: type[T],
    prompt: str,
    *,
    system_prompt: str | None = None,
    model: Model | None = None,
    usage_sink: Callable[[dict], None] | None = None,
) -> T:
    """Ask the model for one instance of `schema`. Stateless: one prompt in, one object out.

    STRANDS CONCEPT — structured output. Passing `structured_output_model=` to an
    agent call makes Strands give the model a hidden tool whose input schema is
    the Pydantic model, and validate what comes back (retrying on a validation
    error). The parsed object is on `result.structured_output`.

    A *fresh* Agent is built on every call, on purpose:
      - `callback_handler=None` — the default handler prints the model's stream
        to stdout, which would dump raw structured-output tool calls into the CLI.
      - a new, empty message list — the router, PRD writer/reviewer and diagram
        brief are all stateless jobs, and their hidden tool traffic must never
        leak into the conversation the user is having with the lanes.
      - no hooks — in particular no approval gate: there is nothing to approve.

    `usage_sink`, if given, receives this call's token usage
    (`result.metrics.latest_agent_invocation.usage`) — how the web UI counts
    every model call in a turn, including these hidden ones.

    LangGraph original: `get_llm().with_structured_output(Schema).invoke(...)`.
    """
    agent = Agent(
        model=model or get_model(),
        system_prompt=system_prompt,
        messages=[],
        callback_handler=None,
    )
    result = agent(prompt, structured_output_model=schema)
    if usage_sink is not None:
        usage_sink(invocation_usage(result))
    return result.structured_output


def invocation_usage(result) -> dict:
    """Token usage of the agent invocation that produced `result`.

    `result.metrics.accumulated_usage` is cumulative over the agent's whole life;
    the per-call figure is on the latest `AgentInvocation`.
    """
    invocation = result.metrics.latest_agent_invocation
    usage = dict(invocation.usage) if invocation else {}
    return {
        "input_tokens": usage.get("inputTokens", 0),
        "output_tokens": usage.get("outputTokens", 0),
        "total_tokens": usage.get("totalTokens", 0),
        "model_calls": len(invocation.cycles) if invocation else 0,
    }
