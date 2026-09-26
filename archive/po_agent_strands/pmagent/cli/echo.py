"""
ConsoleEcho — prints the conversation as it happens, from a Strands hook.

STRANDS CONCEPT — hooks are also how you *observe* an agent. `MessageAddedEvent`
fires every time the agent appends a message to its history: the user's prompt,
each assistant turn (text and/or tool calls), each batch of tool results. This
hook prints the last two and ignores the first (the user just typed it).

Why a hook and not the default `callback_handler`? The callback handler sees
low-level stream chunks (token deltas, partial tool input). The CLI wants whole
messages, and wants to print tool *results* too — which only exist at the
message level. The LangGraph original got the same effect by diffing the state's
message list after every graph step (`_print_new_messages`).

Every lane agent gets this hook; the stateless structured-output agents (router,
PRD writer/reviewer, diagram brief) do not, so their hidden tool traffic never
reaches the terminal.
"""

from __future__ import annotations

import json

from strands.hooks import HookProvider, HookRegistry, MessageAddedEvent

from pmagent.messages import result_text, text_of, tool_results, tool_uses

DIM, BOLD, YELLOW, GREEN, RED, RESET = (
    "\033[2m", "\033[1m", "\033[33m", "\033[32m", "\033[31m", "\033[0m"
)

_PREVIEW_CHARS = 600


def brief(args: dict) -> str:
    """Compact one-line rendering of tool arguments."""
    rendered = json.dumps(args, default=str)
    return rendered if len(rendered) <= 120 else rendered[:120] + " ...}"


def render_message(message: dict) -> list[str]:
    """The terminal lines for one message (empty for the user's own prompt)."""
    lines: list[str] = []
    if message.get("role") == "assistant":
        text = text_of(message)
        if text:
            lines.append(f"\n{BOLD}Agent{RESET}: {text}")
        for use in tool_uses(message):
            lines.append(f"{DIM}  -> {use['name']}({brief(use.get('input') or {})}){RESET}")
    else:
        for result in tool_results(message):
            body = result_text(result).strip()
            if not body:
                continue
            preview = body if len(body) <= _PREVIEW_CHARS else body[:_PREVIEW_CHARS] + " ..."
            lines += [f"{DIM}  | {line}{RESET}" for line in preview.splitlines()]
    return lines


class ConsoleEcho(HookProvider):
    def __init__(self, write=print) -> None:
        self._write = write

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(MessageAddedEvent, self.on_message)

    def on_message(self, event: MessageAddedEvent) -> None:
        for line in render_message(event.message):
            self._write(line)
