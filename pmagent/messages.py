"""
Small readers over Strands' message format.

STRANDS CONCEPT — a conversation is `agent.messages`: a plain list of dicts in
the Bedrock Converse shape. Every message is `{"role": ..., "content": [blocks]}`
and a block is one of (among others):

    {"text": "..."}
    {"toolUse":    {"toolUseId": "...", "name": "...", "input": {...}}}        # role: assistant
    {"toolResult": {"toolUseId": "...", "status": "success"|"error",
                    "content": [{"text": ...} | {"image": ...} | {"json": ...}]}}  # role: user
    {"image": {"format": "png", "source": {"bytes": b"..."}}}

Two things differ from LangChain's message classes and matter throughout this
repo:

* **Tool results are `role: "user"` messages.** "The last user message" is not
  "the last thing the user typed" — you must skip messages that carry a
  `toolResult`. `last_user_text` does exactly that.
* **One tool round-trip is two messages** (assistant with N toolUse blocks, then
  one user message with N toolResult blocks), where LangChain had 1 + N.
"""

from __future__ import annotations

def blocks(message: dict) -> list[dict]:
    return message.get("content") or []

def text_of(message: dict) -> str:
    """The message's text blocks, joined. Tool calls/results and images are ignored."""
    return " ".join(b["text"] for b in blocks(message) if "text" in b).strip()

def tool_uses(message: dict) -> list[dict]:
    return [b["toolUse"] for b in blocks(message) if "toolUse" in b]


def tool_results(message: dict) -> list[dict]:
    return [b["toolResult"] for b in blocks(message) if "toolResult" in b]


def is_tool_result(message: dict) -> bool:
    """A user-role message that carries tool output rather than something typed."""
    return message.get("role") == "user" and bool(tool_results(message))

def last_user_text(messages: list[dict]) -> str:
    """The most recent thing the human actually typed, or ''."""
    for message in reversed(messages):
        if message.get("role") == "user" and not is_tool_result(message):
            text = text_of(message)
            if text:
                return text
    return ""

def user_message(text: str) -> dict:
    return {"role": "user", "content": [{"text": text}]}


def assistant_message(text: str) -> dict:
    return {"role": "assistant", "content": [{"text": text}]}


def result_text(result: dict) -> str:
    """Render a toolResult's content for display: text as-is, images as a label."""
    parts: list[str] = []
    for block in result.get("content") or []:
        if "text" in block:
            parts.append(block["text"])
        elif "image" in block:
            parts.append(f"[image: {block['image'].get('format', '?')}]")
        elif "json" in block:
            parts.append(str(block["json"]))
        elif "document" in block:
            parts.append("[document]")
    return "\n".join(parts)