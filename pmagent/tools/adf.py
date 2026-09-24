"""Markdown -> Atlassian Document Format (ADF).

Jira Cloud's REST v3 API will not accept a plain string as an issue
description; it wants ADF, a nested JSON document tree. Dumping raw text into a
single `paragraph` node "works" in the sense that the request succeeds, but
every newline, heading and bullet collapses into one unreadable blob — which is
what makes acceptance criteria worth drafting and then worthless on arrival.

This module is deliberately a *subset* of Markdown — headings, bullet and
ordered lists, fenced code blocks, paragraphs, plus inline bold and code. That
is everything the ticket standard in `skills/ticket/SKILL.md` actually uses.
Pure functions, no I/O, no LLM.
"""

from __future__ import annotations

import re

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_FENCE = re.compile(r"^\s*```\s*(\w+)?\s*$")
# Bold before code so `**a**` inside backticks isn't split first.
_INLINE = re.compile(r"(\*\*.+?\*\*|`[^`]+`)")


def inline_nodes(text: str) -> list[dict]:
    """Turn one line of text into ADF text nodes, applying inline marks.

    ADF rejects text nodes with an empty `text`, so empty fragments are dropped.
    """
    nodes: list[dict] = []

    for part in _INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            nodes.append(
                {"type": "text", "text": part[2:-2], "marks": [{"type": "strong"}]}
            )
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            nodes.append({"type": "text", "text": part[1:-1], "marks": [{"type": "code"}]})
        else:
            nodes.append({"type": "text", "text": part})

    return nodes


def _paragraph(lines: list[str]) -> dict | None:
    """Build a paragraph node from buffered lines, or None if there's nothing."""
    text = " ".join(line.strip() for line in lines).strip()
    if not text:
        return None
    return {"type": "paragraph", "content": inline_nodes(text)}


def _list_item(text: str) -> dict:
    return {"type": "listItem", "content": [{"type": "paragraph", "content": inline_nodes(text)}]}


def to_adf(markdown: str) -> dict:
    """Render a Markdown subset into an ADF `doc` node.

    ADF requires a non-empty `content` array, so an empty input still yields one
    empty paragraph rather than an invalid document Jira would reject.
    """
    content: list[dict] = []
    paragraph_buffer: list[str] = []
    list_buffer: list[str] = []
    list_kind: str | None = None

    code_lines: list[str] = []
    code_language: str | None = None
    in_code = False

    def flush_paragraph() -> None:
        nonlocal paragraph_buffer
        node = _paragraph(paragraph_buffer)
        if node:
            content.append(node)
        paragraph_buffer = []

    def flush_list() -> None:
        nonlocal list_buffer, list_kind
        if list_buffer and list_kind:
            node: dict = {
                "type": list_kind,
                "content": [_list_item(item) for item in list_buffer],
            }
            if list_kind == "orderedList":
                node["attrs"] = {"order": 1}
            content.append(node)
        list_buffer = []
        list_kind = None

    def flush_all() -> None:
        flush_paragraph()
        flush_list()

    for line in (markdown or "").splitlines():
        fence = _FENCE.match(line)

        if in_code:
            if fence:
                attrs = {"language": code_language} if code_language else {}
                node = {"type": "codeBlock", "attrs": attrs}
                body = "\n".join(code_lines)
                if body:
                    node["content"] = [{"type": "text", "text": body}]
                content.append(node)
                code_lines, code_language, in_code = [], None, False
            else:
                code_lines.append(line)
            continue

        if fence:
            flush_all()
            in_code = True
            code_language = fence.group(1)
            continue

        if not line.strip():
            flush_all()
            continue

        heading = _HEADING.match(line)
        if heading:
            flush_all()
            content.append(
                {
                    "type": "heading",
                    "attrs": {"level": len(heading.group(1))},
                    "content": inline_nodes(heading.group(2).strip()),
                }
            )
            continue

        bullet = _BULLET.match(line)
        ordered = _ORDERED.match(line)

        if bullet or ordered:
            kind = "bulletList" if bullet else "orderedList"
            if list_kind and list_kind != kind:
                flush_list()
            flush_paragraph()
            list_kind = kind
            list_buffer.append((bullet or ordered).group(1).strip())
            continue

        flush_list()
        paragraph_buffer.append(line)

    # An unterminated code fence still has to produce something valid.
    if in_code:
        node = {"type": "codeBlock", "attrs": {"language": code_language} if code_language else {}}
        body = "\n".join(code_lines)
        if body:
            node["content"] = [{"type": "text", "text": body}]
        content.append(node)

    flush_all()

    if not content:
        content = [{"type": "paragraph", "content": []}]

    return {"type": "doc", "version": 1, "content": content}


_LIST_TYPES = {"bulletList", "orderedList"}


def to_text(node: dict | None) -> str:
    """Render an ADF document back into readable plain text.

    The inverse direction of `to_adf`, and the reason it exists: everything Jira
    stores as rich text — descriptions, comment bodies — comes back as a node
    tree, so an agent asked "why is this blocked?" would otherwise be reading
    `{'type': 'doc', 'content': [...]}` at itself.

    Deliberately lossy in one direction only: structure that carries meaning
    (headings, list bullets, code fences, quotes, table rows) is preserved as
    light Markdown, while styling that doesn't (bold, colour, alignment) is
    dropped. Unknown node types recurse into their children rather than being
    skipped — a future Jira node type should degrade to its text, not vanish.

    **Mentions are resolved to names.** `@Alan Yuen` in a comment is stored as a
    node with an account id and a display name; keeping the id and losing the
    name would be exactly backwards for a human reading a blocker.
    """
    if not isinstance(node, dict):
        return ""

    kind = node.get("type", "")
    attrs = node.get("attrs") or {}
    children = node.get("content") or []

    if kind == "text":
        return node.get("text", "")

    if kind == "hardBreak":
        return "\n"

    if kind == "mention":
        # Jira stores the display name with a leading "@" already.
        name = attrs.get("text") or attrs.get("displayName") or "unknown"
        return name if name.startswith("@") else f"@{name}"

    if kind == "emoji":
        return attrs.get("text") or attrs.get("shortName") or ""

    if kind in ("inlineCard", "blockCard"):
        return attrs.get("url") or ""

    if kind == "rule":
        return "---"

    if kind == "codeBlock":
        body = "".join(to_text(child) for child in children)
        language = attrs.get("language") or ""
        return f"```{language}\n{body}\n```"

    if kind == "heading":
        level = int(attrs.get("level", 1))
        return f"{'#' * level} " + "".join(to_text(child) for child in children)

    if kind in _LIST_TYPES:
        ordered = kind == "orderedList"
        lines = []
        for index, item in enumerate(children, start=int(attrs.get("order", 1))):
            marker = f"{index}." if ordered else "-"
            body = to_text(item).strip()
            # Keep nested lines under their bullet rather than breaking the list.
            lines.append(f"{marker} " + body.replace("\n", "\n  "))
        return "\n".join(lines)

    if kind == "blockquote":
        body = "\n\n".join(to_text(child) for child in children).strip()
        return "\n".join(f"> {line}" for line in body.splitlines())

    if kind in ("tableRow", "tableCell", "tableHeader"):
        joined = " | ".join(to_text(child).strip() for child in children)
        return f"| {joined} |" if kind == "tableRow" else joined

    # doc, paragraph, listItem, panel, table and anything unrecognised: join the
    # children. Block-level containers separate with blank lines so paragraphs
    # don't run together; a table's rows are single-spaced so it still reads as
    # a table; inline containers just concatenate.
    if kind == "table":
        return "\n".join(p for p in (to_text(c) for c in children) if p)

    separator = "\n\n" if kind in ("doc", "panel", "listItem") else ""
    parts = [to_text(child) for child in children]
    return separator.join(p for p in parts if p) if separator else "".join(parts)


def render_description(
    description: str,
    acceptance_criteria: list[str] | None = None,
    source_issue: str | None = None,
) -> dict:
    """Render a ticket body plus its acceptance criteria into one ADF document.

    Acceptance criteria get their own heading and bullet list so they land in
    Jira as a scannable checklist rather than as prose the reviewer has to
    excavate.

    `source_issue` is appended as a Related line. It exists so that provenance —
    "this card was generated from CSCI-1379" — survives past the approval prompt
    into the ticket itself: a batch drafted per-issue is exactly where a card for
    the wrong parent slips through, and the only durable record of which one it
    came from is the one written into the description.
    """
    markdown = (description or "").strip()

    if acceptance_criteria:
        bullets = "\n".join(f"- {criterion}" for criterion in acceptance_criteria)
        markdown = f"{markdown}\n\n## Acceptance Criteria\n\n{bullets}".strip()

    if source_issue and source_issue.strip():
        markdown = f"{markdown}\n\nRelated: {source_issue.strip()}".strip()

    return to_adf(markdown)
