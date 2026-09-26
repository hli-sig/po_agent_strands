"""
TraceRecorder — turns what a Strands agent does into the "thinking chain".

STRANDS CONCEPT — two ways to watch an agent, used together here:

* the **callback handler** gets *stream-level* data as it arrives: text deltas
  (`data=`) and reasoning deltas (`reasoningText=`). That's what makes the UI
  feel live.
* **hooks** get *lifecycle* events with structure: `BeforeToolCallEvent` /
  `AfterToolCallEvent` (tool name, input, result, duration) and
  `MessageAddedEvent` (each completed assistant message).

A few steps come from outside the agent loop and are reported by `PMAssistant`
callbacks: the route decision (`route_decided`) and the PRD writer/reviewer loop
(`prd_step`). Approvals are reported by the web session, which owns them.

Every step becomes one event `{type, data}` passed to `emit`. The recorder does
not know about HTTP; `pmagent/web/sessions.py` points `emit` at the running turn.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookProvider, MessageAddedEvent

from pmagent.messages import result_text, text_of, tool_uses

Emit = Callable[[str, dict], None]

# Tool output shown in the chain is a preview; the model saw all of it.
PREVIEW_CHARS = 2000


def _preview(text: str) -> tuple[str, bool]:
    return (text, False) if len(text) <= PREVIEW_CHARS else (text[:PREVIEW_CHARS], True)


class TraceRecorder(HookProvider):
    """Hook provider *and* callback handler for every lane agent of one conversation."""

    def __init__(self, emit: Emit | None = None) -> None:
        self.emit: Emit = emit or (lambda kind, data: None)
        self._tool_started: dict[str, float] = {}

    # -- wiring --------------------------------------------------------------

    def assistant_kwargs(self) -> dict:
        """Everything PMAssistant needs to report into this recorder."""
        return {
            "hooks": [self],
            "callback_handler": self,
            "on_route_decided": self.route_decided,
            "on_prd_step": self.prd_step,
        }

    def register_hooks(self, registry, **kwargs) -> None:
        registry.add_callback(BeforeToolCallEvent, self._before_tool)
        registry.add_callback(AfterToolCallEvent, self._after_tool)
        registry.add_callback(MessageAddedEvent, self._message_added)

    # -- callback handler: streaming deltas ----------------------------------

    def __call__(self, **kwargs) -> None:
        if kwargs.get("reasoningText"):
            self.emit("reasoning.delta", {"text": kwargs["reasoningText"]})
        elif kwargs.get("data"):
            self.emit("text.delta", {"text": kwargs["data"]})

    # -- hooks: structured lifecycle -----------------------------------------

    def _before_tool(self, event: BeforeToolCallEvent) -> None:
        use = event.tool_use
        self._tool_started[use["toolUseId"]] = time.monotonic()
        self.emit("tool.started", {
            "tool_use_id": use["toolUseId"], "name": use["name"], "input": use.get("input") or {},
        })

    def _after_tool(self, event: AfterToolCallEvent) -> None:
        use, result = event.tool_use, event.result
        started = self._tool_started.pop(use["toolUseId"], None)
        duration = event.duration if event.duration is not None else (
            time.monotonic() - started if started else None
        )
        output, truncated = _preview(result_text(result))
        images = sum(1 for block in result.get("content") or [] if "image" in block)
        self.emit("tool.finished", {
            "tool_use_id": use["toolUseId"],
            "name": use["name"],
            "status": result.get("status", "success"),
            "duration_ms": round(duration * 1000) if duration is not None else None,
            "output": output,
            "output_truncated": truncated,
            "images": images,
        })

    def _message_added(self, event: MessageAddedEvent) -> None:
        message = event.message
        if message.get("role") != "assistant":
            return
        reasoning = " ".join(
            block["reasoningContent"].get("reasoningText", {}).get("text", "")
            or block["reasoningContent"].get("text", "")
            for block in message.get("content") or []
            if "reasoningContent" in block
        ).strip()
        self.emit("message.completed", {
            "text": text_of(message),
            "tool_calls": [use["name"] for use in tool_uses(message)],
            "has_reasoning": bool(reasoning),
        })

    # -- PMAssistant callbacks ------------------------------------------------

    def route_decided(self, route: str, reason: str) -> None:
        from pmagent.agents.router import route_to_lane

        self.emit("route.decided", {"route": route, "lane": route_to_lane(route), "reason": reason})

    def prd_step(self, kind: str, data: dict) -> None:
        self.emit("prd.step", {"step": kind, **data})
