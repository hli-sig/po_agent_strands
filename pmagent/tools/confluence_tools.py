"""Read-only Confluence Cloud access.

Same Atlassian site and the same API token as Jira, so there are no new
credentials — `env.validate_jira()` covers both.

Three facts about the real content drove every design decision here, all of them
measured against the EDP space's *Enterprise Data Model* page:

1. **Pages are ADF**, the same document tree Jira uses for descriptions and
   comments. `tools/adf.py:to_text` already reads it, so nothing here parses a
   document format. That page is 248 KB of ADF.
2. **A page is far too big to read whole.** The same page renders to ~40,000
   characters of text across 220 headings. Reading it without a section is a
   context-window accident, so an unscoped read of a large page returns its
   *outline* and asks which section — the cheap read that makes the expensive
   read unnecessary.
3. **The content is mostly screenshots.** 85 of that page's 96 entity sections
   carry their column definitions in an image, not a table. `DimEmployee`
   renders to 211 characters of prose and nothing else. A text-only reader would
   hand the agent one sentence and let it describe the entity confidently, which
   is the worst failure this repo knows about.

So a section read returns text **and** its images. In Strands that is simply a
`ToolResult` whose content mixes `{"text": ...}` and `{"image": ...}` blocks.

STRANDS NOTE — what changed from the LangGraph version: OpenAI's chat completions
API rejects image blocks inside a tool message. The LangGraph version worked
around that by returning a `Command` that appended a follow-up `HumanMessage`
carrying the images, tagged so the classifier would not mistake it for the user.
Strands' OpenAI provider already does that split itself when it formats the
request (`OpenAIModel._split_tool_message_images`), and Anthropic accepts images
in tool results natively, so the tool just returns images and the whole
injected-message apparatus is gone. One consequence survives: on the OpenAI path
the images arrive in a *separate* message from the text, matched only by order —
so the text labels every image and says they follow in order.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any

import requests
from requests.auth import HTTPBasicAuth
from strands import tool

from pmagent import env
from pmagent.tools.adf import to_text


# A page longer than this (rendered characters) is returned as an outline rather
# than in full. ~6k characters is a large-but-readable section; the page that
# motivated this is nearly seven times that.
_FULL_PAGE_CHAR_LIMIT = 6000

# Hard ceiling on the text of any single read, applied after section slicing.
_SECTION_CHAR_LIMIT = 12000

# Images are the expensive part of a read — each one costs roughly a page of
# text in tokens. Four is enough for a data-model section; more than that and
# the model should be asked to narrow down.
_MAX_IMAGES = 4

# Providers reject very large images, and a multi-megabyte diagram is not
# something a model reads usefully anyway.
_MAX_IMAGE_BYTES = 4_000_000

_SEARCH_EXCERPT_CHARS = 200


class ConfluenceError(RuntimeError):
    """Raised when Confluence rejects a request or a page cannot be resolved."""


# ---------------------------------------------------------------------------
# Pure helpers — no I/O, unit-tested in tests/test_confluence_tools.py
# ---------------------------------------------------------------------------


def page_id_from_url(value: str) -> str:
    """Pull a page id out of anything a user is likely to paste.

    Accepts a bare id, a modern `/wiki/spaces/EDP/pages/<id>/Title` URL, and the
    `/pages/viewpage.action?pageId=<id>` legacy form. A short `/wiki/x/AbCd`
    tiny-link cannot be resolved without a request, so it is refused by name
    rather than guessed at.
    """
    text = (value or "").strip().strip("<>")
    if not text:
        raise ConfluenceError("No Confluence page given.")
    if text.isdigit():
        return text

    match = re.search(r"/pages/(\d+)", text)
    if match:
        return match.group(1)
    match = re.search(r"[?&]pageId=(\d+)", text)
    if match:
        return match.group(1)
    if re.search(r"/wiki/x/", text):
        raise ConfluenceError(
            f"{text} is a Confluence short link, which does not contain the page "
            "id. Open it and paste the full URL, or search for the page by title."
        )
    raise ConfluenceError(
        f"Could not find a page id in {text!r}. Paste the full page URL (it looks "
        "like .../wiki/spaces/EDP/pages/1622769666/Enterprise+Data+Model) or the "
        "numeric id."
    )


def section_from_url(value: str) -> str:
    """The `#DimEmployee` fragment of a pasted URL, if there is one.

    Pasting the exact link you were reading should scope the read to the exact
    heading you were looking at — the anchor is already the user's intent.
    """
    _, _, fragment = (value or "").partition("#")
    return fragment.strip()


def _normalise(name: str) -> str:
    """Compare headings the way an anchor does: letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def heading_text(node: dict) -> str:
    """The plain text of a heading node."""
    return "".join(
        child.get("text", "") for child in node.get("content", []) or []
    ).strip()


def split_sections(doc: dict) -> list[dict]:
    """Split an ADF document into `{level, title, nodes}` sections by heading.

    Content before the first heading becomes an untitled leading section, so
    nothing is silently dropped.
    """
    content = (doc or {}).get("content") or []
    sections: list[dict] = []
    current = {"level": 0, "title": "", "nodes": []}
    for node in content:
        if node.get("type") == "heading":
            if current["nodes"] or current["title"]:
                sections.append(current)
            current = {
                "level": (node.get("attrs") or {}).get("level", 1),
                "title": heading_text(node),
                "nodes": [node],
            }
        else:
            current["nodes"].append(node)
    if current["nodes"] or current["title"]:
        sections.append(current)
    return sections


def find_section(sections: list[dict], name: str) -> tuple[dict | None, list[str]]:
    """Resolve a heading name to its section, refusing to guess on a near miss.

    Returns `(section, ambiguous_titles)`. Matching ignores case and punctuation
    so a URL anchor resolves, but it is otherwise exact — same contract as
    `pick_transition` in `jira/matching.py`. Returning the wrong section of a data
    model is worse than returning none, because it looks like an answer.
    """
    target = _normalise(name)
    if not target:
        return None, []
    matches = [s for s in sections if _normalise(s["title"]) == target]
    if len(matches) == 1:
        return matches[0], []
    if len(matches) > 1:
        return None, [s["title"] for s in matches]
    return None, []


def section_and_children(sections: list[dict], index: int) -> list[dict]:
    """A section plus every deeper heading beneath it, up to the next peer.

    `DimEmployee` is a level-2 heading whose detail sits under a level-3
    `Platinum` heading; returning the level-2 slice alone would drop it.
    """
    top = sections[index]
    out = [top]
    for section in sections[index + 1:]:
        if section["level"] <= top["level"]:
            break
        out.append(section)
    return out


def media_ids(nodes: list[dict]) -> list[str]:
    """Every media (attachment) id referenced under a set of ADF nodes."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        if node.get("type") == "media":
            media_id = (node.get("attrs") or {}).get("id")
            if media_id and media_id not in found:
                found.append(media_id)
        walk(node.get("content"))

    walk(nodes)
    return found


def clip(text: str, limit: int, label: str) -> str:
    """Truncate loudly or not at all."""
    if len(text) <= limit:
        return text
    return (
        text[:limit]
        + f"\n\n[clipped: showing {limit} of {len(text)} characters of {label}. "
        "Ask for a narrower section for the rest.]"
    )


def render_outline(page: dict, sections: list[dict], rendered_chars: int) -> str:
    """The page's headings, for when reading the whole thing is not sensible."""
    lines = [
        f"{page.get('title')} (page {page.get('id')}) — {rendered_chars} characters "
        f"of text across {len(sections)} sections. Too large to read whole.",
        "",
        "Sections (pass one as `section` to read it, with its images):",
    ]
    for section in sections:
        if not section["title"]:
            continue
        indent = "  " * max(0, section["level"] - 1)
        images = len(media_ids(section["nodes"]))
        suffix = f"  [{images} image(s)]" if images else ""
        lines.append(f"  {indent}- {section['title']}{suffix}")
    return "\n".join(lines)


def render_section(page: dict, nodes: list[dict], title: str, image_count: int) -> str:
    """Render one section's text, and say plainly what is only in its images."""
    body = to_text({"type": "doc", "version": 1, "content": nodes}).strip()
    header = f"{page.get('title')} > {title} (page {page.get('id')})"
    lines = [header, "", clip(body, _SECTION_CHAR_LIMIT, "section text")]
    if image_count:
        lines += [
            "",
            f"This section's detail is in {image_count} image(s), which follow this "
            f"text in order, 1 to {image_count}, each announced by a label. Read them "
            "as part of the page — on these pages the column definitions are usually "
            "only in the image.",
        ]
    else:
        lines += ["", "This section has no images; the text above is all of it."]
    return "\n".join(lines)


def render_search_results(results: list[dict], query: str, base_url: str) -> str:
    """One line per hit, with the URL needed to read it."""
    if not results:
        return (
            f"No Confluence pages matched {query!r}. Try fewer or different words, "
            "or name the space."
        )
    lines = [f"{len(results)} Confluence result(s) for {query!r}:"]
    for item in results:
        content = item.get("content") or {}
        title = content.get("title") or item.get("title") or "(untitled)"
        page_id = content.get("id", "?")
        space = (item.get("resultGlobalContainer") or {}).get("title", "")
        excerpt = re.sub(r"<[^>]+>", "", item.get("excerpt") or "").replace("\n", " ")
        lines.append(f"- {title} (page {page_id}{f', space {space}' if space else ''})")
        if excerpt.strip():
            lines.append(f"    {excerpt.strip()[:_SEARCH_EXCERPT_CHARS]}")
        # Use the link Confluence returns rather than assembling one: a guessed
        # /pages/<id> path is not a route Confluence serves, and a link that
        # 404s is worse than no link.
        webui = item.get("url") or ((content.get("_links") or {}).get("webui")) or ""
        if webui:
            lines.append(f"    {base_url}{webui}")
    return "\n".join(lines)


# Confluence media types -> the image formats a Strands `ImageContent` accepts.
_IMAGE_FORMATS = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/gif": "gif",
    "image/webp": "webp",
}


def image_blocks(images: list[tuple[str, str, bytes]]) -> list[dict]:
    """Build the Strands content blocks for a section's images.

    `images` is `(title, media_type, data)`. Each image is preceded by a text
    label, because an unlabelled wall of screenshots gives the model no way to
    say which one an answer came from. A format the model APIs cannot take (an
    SVG, say) becomes a note instead of being dropped silently.
    """
    blocks: list[dict] = []
    for position, (title, media_type, data) in enumerate(images, start=1):
        label = f"Confluence image {position} of {len(images)}: {title}"
        image_format = _IMAGE_FORMATS.get(media_type.lower())
        if image_format is None:
            blocks.append({"text": f"{label} [not shown: {media_type} is not a supported image format]"})
            continue
        blocks.append({"text": label})
        blocks.append({"image": {"format": image_format, "source": {"bytes": data}}})
    return blocks


# ---------------------------------------------------------------------------
# Client — one seam (`_session`) so tests can inject a fake transport
# ---------------------------------------------------------------------------


class ConfluenceClient:
    """The narrow slice of Confluence Cloud this repo reads.

    Build it with `get_client()`. Never construct one at import time: `__init__`
    validates credentials, which would make this module unimportable without a
    configured `.env` and break the test suite — the same rule as `JiraClient`.
    """

    def __init__(self) -> None:
        env.validate_jira()  # Confluence uses the same Atlassian credentials
        self._base = (env.CONFLUENCE_BASE_URL or "").rstrip("/")
        self._session = requests.Session()
        self._session.auth = HTTPBasicAuth(env.JIRA_EMAIL, env.JIRA_API_TOKEN)
        self._session.headers.update({"Accept": "application/json"})

    def _get(self, path: str, params: dict | None = None) -> dict:
        response = self._session.get(self._base + path, params=params, timeout=30)
        if not response.ok:
            raise ConfluenceError(
                f"Confluence {response.status_code} on {path}: {response.text[:300]}"
            )
        return response.json()

    def get_page(self, page_id: str) -> dict:
        """A page with its body as ADF."""
        return self._get(f"/api/v2/pages/{page_id}", {"body-format": "atlas_doc_format"})

    def page_doc(self, page: dict) -> dict:
        """The ADF document tree of a page fetched by `get_page`.

        Pages written in the legacy editor have no ADF representation, only
        storage-format XHTML. Say so rather than returning an empty document
        that would read as an empty page.
        """
        value = (((page.get("body") or {}).get("atlas_doc_format") or {}).get("value"))
        if not value:
            raise ConfluenceError(
                f"Page {page.get('id')} has no ADF body — it was probably created in "
                "the legacy editor. Open it in Confluence and convert it to the new "
                "editor, or read it in the browser."
            )
        return json.loads(value)

    def attachments(self, page_id: str) -> list[dict]:
        """Every attachment on a page, following the cursor to the end."""
        results: list[dict] = []
        params: dict = {"limit": 250}
        while True:
            payload = self._get(f"/api/v2/pages/{page_id}/attachments", params)
            results.extend(payload.get("results") or [])
            next_link = (payload.get("_links") or {}).get("next")
            if not next_link:
                return results
            cursor = next_link.split("cursor=")[-1].split("&")[0]
            params = {"limit": 250, "cursor": cursor}

    def download(self, link: str) -> bytes:
        """Fetch an attachment body from its download link."""
        url = self._base + link if link.startswith("/") else link
        response = self._session.get(url, timeout=60)
        if not response.ok:
            raise ConfluenceError(f"Could not download attachment: {response.status_code}")
        return response.content

    def search(self, query: str, space: str = "", limit: int = 10) -> list[dict]:
        """CQL text search, scoped to a space unless told otherwise."""
        escaped = query.replace('"', '\\"')
        cql = f'type = page AND text ~ "{escaped}"'
        if space:
            cql = f'space = "{space}" AND {cql}'
        payload = self._get("/rest/api/search", {"cql": cql, "limit": limit})
        return payload.get("results") or []


_client: ConfluenceClient | None = None


def get_client() -> ConfluenceClient:
    """Lazily build and cache the client."""
    global _client
    if _client is None:
        _client = ConfluenceClient()
    return _client


def collect_images(
    client: ConfluenceClient, page_id: str, wanted: list[str]
) -> tuple[list[tuple[str, str, bytes]], list[str]]:
    """Download the attachments a set of media ids refers to.

    Returns `(images, notes)`. Every skipped image produces a note — a silently
    dropped screenshot is a silently dropped answer.
    """
    notes: list[str] = []
    if not wanted:
        return [], notes

    by_file_id = {
        attachment.get("fileId"): attachment
        for attachment in client.attachments(page_id)
        if attachment.get("fileId")
    }

    selected = wanted[:_MAX_IMAGES]
    if len(wanted) > _MAX_IMAGES:
        notes.append(
            f"showing {_MAX_IMAGES} of {len(wanted)} images in this section; "
            "read a narrower sub-heading for the rest"
        )

    images: list[tuple[str, str, bytes]] = []
    for media_id in selected:
        attachment = by_file_id.get(media_id)
        if attachment is None:
            notes.append(f"image {media_id} is referenced but has no attachment")
            continue
        media_type = attachment.get("mediaType") or ""
        if not media_type.startswith("image/"):
            notes.append(f"attachment {attachment.get('title', media_id)} is {media_type}, not an image")
            continue
        if (attachment.get("fileSize") or 0) > _MAX_IMAGE_BYTES:
            notes.append(f"an image is {attachment['fileSize']} bytes, too large to read")
            continue
        link = (attachment.get("_links") or {}).get("download") or attachment.get("downloadLink")
        if not link:
            notes.append(f"image {media_id} has no download link")
            continue
        try:
            images.append((attachment.get("title") or media_id, media_type, client.download(link)))
        except ConfluenceError as exc:
            notes.append(str(exc))
    return images, notes


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool
def read_confluence_page(page: str, section: str = "") -> dict:
    """Read a Confluence page, or one section of it, including its images.

    Read-only. Use this for anything documented in Confluence — the enterprise
    data model, standards, design decisions, runbooks.

    Large pages are not returned whole: without a section you get the page's
    outline and should call again naming the section you need.

    **Sections often carry their real content in screenshots**, especially data
    model pages where the column list is an image. Those images are part of this
    tool result, after the text — read them as part of the page. If a section's
    text is one sentence and it says the detail is in an image, the image is where
    the answer is; do not report the field list as unavailable and do not infer it
    from the entity name.

    Args:
        page: A Confluence page URL or numeric page id. A pasted URL with a
            `#Heading` anchor scopes the read to that heading automatically.
        section: Heading to read, e.g. "DimEmployee". Matching ignores case and
            punctuation but is otherwise exact — it will not guess between
            similar headings.
    """
    try:
        page_id = page_id_from_url(page)
        wanted_section = section.strip() or section_from_url(page)
        client = get_client()
        payload = client.get_page(page_id)
        doc = client.page_doc(payload)
        sections = split_sections(doc)

        if not wanted_section:
            full_text = to_text(doc).strip()
            if len(full_text) > _FULL_PAGE_CHAR_LIMIT:
                return _text_only(render_outline(payload, sections, len(full_text)))
            nodes, title, images_wanted = doc.get("content") or [], "(whole page)", media_ids(doc.get("content") or [])
        else:
            match, ambiguous = find_section(sections, wanted_section)
            if ambiguous:
                return _text_only(
                    f"{len(ambiguous)} sections on {payload.get('title')} are called "
                    f"{wanted_section!r}. Ask which one is meant."
                )
            if match is None:
                titles = [s["title"] for s in sections if s["title"]]
                return _text_only(
                    f"No section called {wanted_section!r} on {payload.get('title')}. "
                    f"Its {len(titles)} sections are: {', '.join(titles[:60])}"
                    + (" ..." if len(titles) > 60 else "")
                )
            index = sections.index(match)
            slice_ = section_and_children(sections, index)
            nodes = [node for part in slice_ for node in part["nodes"]]
            title = match["title"]
            images_wanted = media_ids(nodes)

        images, notes = collect_images(client, page_id, images_wanted)
        text = render_section(payload, nodes, title, len(images))
        if notes:
            text += "\n" + "\n".join(f"[note: {note}]" for note in notes)
    except ConfluenceError as exc:
        return _text_only(f"Could not read Confluence page: {exc}")
    except Exception as exc:  # noqa: BLE001 — a lane needs an answer, not a traceback
        return _text_only(f"Could not read Confluence page: {type(exc).__name__}: {exc}")

    # A Strands ToolResult: text first, then each labelled image. Returning a
    # dict with "status" and "content" tells the @tool decorator not to wrap it.
    return {"status": "success", "content": [{"text": text}, *image_blocks(images)]}


def _text_only(text: str) -> dict:
    return {"status": "success", "content": [{"text": text}]}


@tool
def search_confluence(query: str, space: str = "", limit: int = 10) -> str:
    """Find Confluence pages by their text.

    Read-only. Use this when you need a page you don't have a link for — then
    call `read_confluence_page` with the id it returns.

    Args:
        query: Words to search for, e.g. "enterprise data model".
        space: Space key to search in. Defaults to CONFLUENCE_SPACE_KEY; pass
            "*" to search every space the user can see.
        limit: Maximum results (default 10).
    """
    scope = "" if space.strip() == "*" else (space.strip() or env.CONFLUENCE_SPACE_KEY)
    try:
        client = get_client()
        results = client.search(query, scope, limit)
    except ConfluenceError as exc:
        return f"Confluence search failed: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"Confluence search failed: {type(exc).__name__}: {exc}"
    return render_search_results(results, query, (env.CONFLUENCE_BASE_URL or "").rstrip("/"))


# Read-only by construction: this module has no write path to Confluence, so
# there is nothing to gate. Both lists are still declared so the reconciliation
# test in `tests/test_agent_lanes.py` can assert every tool here is classified.
READ_TOOLS = [read_confluence_page, search_confluence]
WRITE_TOOLS = []
