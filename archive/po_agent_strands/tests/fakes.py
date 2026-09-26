"""A scripted Strands `Model` for testing agents offline — no network, no API key.

STRANDS CONCEPT — a `Model` is just an adapter with an async `stream()` that
yields provider-neutral events:

    messageStart → (contentBlockStart → contentBlockDelta* → contentBlockStop)* → messageStop → metadata

Strands' event loop assembles those into an assistant message, executes any
`toolUse` blocks, appends the results and calls `stream()` again. So a fake that
replays a fixed script of assistant turns drives the *real* agent loop, the real
hooks and the real interrupt machinery — only the model's choices are canned.

Structured output: when an agent is called with `structured_output_model=X`,
Strands does NOT call `Model.structured_output`. It adds a hidden tool whose name
is the schema's name (e.g. "RouteDecision") and waits for the model to call it.
So a scripted structured reply is simply a tool call — see `structured()`.

Usage:
    model = ScriptedModel([
        [call("query_jira_issues", jql="project = CSCI")],   # turn 1: a tool call
        [text("There are 3 open issues.")],                   # turn 2: the answer
    ])
    agent = Agent(model=model, tools=[...], callback_handler=None)
"""

from __future__ import annotations

import copy
import itertools
import json
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel
from strands.models import Model

_ids = itertools.count(1)


def text(value: str) -> dict:
    """An assistant text block."""
    return {"text": value}


def reasoning(value: str) -> dict:
    """A reasoning block — what a thinking model streams before it answers.

    Emitted as a `reasoningContent` delta, which Strands hands to the callback
    handler as `reasoningText` (how the web UI's thinking chain receives it).
    """
    return {"reasoningContent": {"text": value}}


def call(name: str, **args: Any) -> dict:
    """An assistant tool call."""
    return {"toolUse": {"name": name, "toolUseId": f"tooluse_{next(_ids)}", "input": args}}


def structured(schema: type[BaseModel], **fields: Any) -> dict:
    """A structured-output reply: a call to the hidden tool named after `schema`."""
    return call(schema.__name__, **schema(**fields).model_dump(by_alias=True))


Turn = list[dict] | Callable[[list[dict]], list[dict]]


class ScriptedModel(Model):
    """Replays scripted assistant turns and records what it was asked.

    Each entry of `turns` is a list of blocks (`text(...)`, `call(...)`), or a
    function `messages -> blocks` for replies that depend on the conversation.
    `self.requests` records, per model call, the system prompt, tool names and a
    copy of the messages the model saw.
    """

    def __init__(self, turns: list[Turn] | None = None) -> None:
        self.turns: list[Turn] = list(turns or [])
        self.requests: list[dict] = []
        self.config: dict = {"model_id": "scripted"}

    def update_config(self, **model_config: Any) -> None:
        self.config.update(model_config)

    def get_config(self) -> dict:
        return self.config

    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise NotImplementedError("Agents use the structured-output tool; script a structured() call.")
        yield  # pragma: no cover — makes this an async generator

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.requests.append({
            "system_prompt": system_prompt,
            "tools": [spec["name"] for spec in tool_specs or []],
            "messages": copy.deepcopy(messages),
        })
        if not self.turns:
            raise AssertionError("ScriptedModel ran out of scripted turns")
        turn = self.turns.pop(0)
        blocks = turn(messages) if callable(turn) else turn

        yield {"messageStart": {"role": "assistant"}}
        has_tool_use = False
        for block in blocks:
            if "reasoningContent" in block:
                yield {"contentBlockStart": {"start": {}}}
                yield {"contentBlockDelta": {"delta": {"reasoningContent": {"text": block["reasoningContent"]["text"]}}}}
                yield {"contentBlockStop": {}}
            elif "text" in block:
                yield {"contentBlockStart": {"start": {}}}
                yield {"contentBlockDelta": {"delta": {"text": block["text"]}}}
                yield {"contentBlockStop": {}}
            elif "toolUse" in block:
                has_tool_use = True
                use = block["toolUse"]
                yield {"contentBlockStart": {"start": {"toolUse": {
                    "name": use["name"], "toolUseId": use["toolUseId"]}}}}
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(use["input"])}}}}
                yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use" if has_tool_use else "end_turn"}}
        yield {"metadata": {
            "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
            "metrics": {"latencyMs": 0},
        }}

    @property
    def remaining(self) -> int:
        return len(self.turns)
