"""
Build the one-page course: docs/learning/*.md → docs/learning/course.html.

    uv run scripts/build_course.py          # write the page
    uv run scripts/build_course.py --check  # exit 1 if the committed page is stale

The Markdown lessons are the source of truth; this page is generated so the two
can never drift (tests/test_course_build.py runs --check). The page also carries:

* an API reference generated from the app's own OpenAPI document, so it lists
  exactly the routes that exist;
* real screenshots from docs/evidence/ui/, embedded as data URIs.

The output follows the Artifact page contract (no <html>/<head>/<body>: it is
wrapped at publish time; fonts only from Google Fonts; light and dark themes via
tokens), so the same file is what gets published as the course web page. Browsers
render it fine opened locally too.
"""

from __future__ import annotations

import base64
import html
import re
import struct
import sys
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent.parent
LEARN = ROOT / "docs" / "learning"
OUT = LEARN / "course.html"
# Demo-data screenshots (scripts/course_figures.py): the published page carries no
# real Jira data. The real-data screenshots in docs/evidence/ui stay local.
SHOTS = LEARN / "figures"

ORDER = [
    ("README.md", "start", "Start here"),
    ("01-agent-loop-and-models.md", "lesson-01", "1 · Agent loop & models"),
    ("02-tools.md", "lesson-02", "2 · Tools"),
    ("03-structured-output.md", "lesson-03", "3 · Structured output"),
    ("04-conversation-state.md", "lesson-04", "4 · Conversation state"),
    ("05-hooks.md", "lesson-05", "5 · Hooks"),
    ("06-interrupts.md", "lesson-06", "6 · Interrupts"),
    ("07-multi-agent.md", "lesson-07", "7 · Multi-agent patterns"),
    ("08-mcp.md", "lesson-08", "8 · MCP"),
    ("09-testing.md", "lesson-09", "9 · Testing offline"),
    ("10-serving-over-http.md", "lesson-10", "10 · Serving over HTTP"),
    ("11-thinking-chain.md", "lesson-11", "11 · The thinking chain"),
    ("LANGGRAPH_TO_STRANDS.md", "langgraph", "LangGraph → Strands"),
    (None, None, "Part 2 · Rebuild"),
    ("rebuild.md", "rebuild", "Rebuild it yourself (R0–R10)"),
    ("rebuild-domain.md", "rebuild-domain", "The domain layer (D0–D7)"),
    (None, None, "Part 3 · Extend"),
    ("12-run-log.md", "lesson-12", "12 · Logging: the run log"),
    ("13-persistence.md", "lesson-13", "13 · Persistence"),
    ("14-long-term-memory.md", "lesson-14", "14 · Long-term memory"),
    ("15-evals.md", "lesson-15", "15 · Evals"),
    ("16-claude-code-harness.md", "lesson-16", "16 · The Claude Code harness"),
    ("17-jev.md", "lesson-17", "17 · A decision model (Jev)"),
]
ANCHOR = {name: anchor for name, anchor, _ in ORDER if name}

FIGURES = [
    ("thinking-chain.png", "lesson-11",
     "The thinking chain: route, reasoning summary, tool call with duration, more reasoning, the reply. "
     "(Demo data; see docs/evidence for live runs.)"),
    ("approval.png", "lesson-06",
     "The approval gate in the web UI: the agent paused before add_jira_comment. Nothing runs until you choose."),
    ("prd-loop.png", "lesson-07",
     "The requirements workflow in the chain: the reviewer sends pass 1 back for a missing requirement, "
     "approves pass 2, then Python renders the PRD."),
    ("phone.png", "lesson-10",
     "At phone width, after a reload: the page rebuilt the chat and chain from GET …/turns."),
]


_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+\.)\s")


def loosen_lists(text: str) -> str:
    """Adapt GitHub-style Markdown lists to Python-Markdown's stricter rules.

    1. A list needs a blank line before it ("…well:" then "- item" works on
       GitHub, not here), so one is inserted.
    2. Nested items must be indented by 4 spaces; the lessons use 2–3. Inside a
       list, any line indented 1–3 spaces is re-indented to 4.
    3. Fenced code doesn't work inside a list item at all, so a fence inside a
       list becomes an indented code block (8 spaces: 4 for the item, 4 for code).
    """
    out, previous = [], ""
    in_list = False
    fence_shift = None            # None: not in a fence; 0: a normal fence; "list": a fence inside a list item
    fence_indent = 0
    for line in text.splitlines():
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)

        if fence_shift is not None:                       # inside a fence
            if stripped.startswith("```"):
                if fence_shift == "list":
                    out.append("")                        # end the indented block
                else:
                    out.append(line)
                fence_shift = None
            elif fence_shift == "list":
                out.append("        " + line[fence_indent:] if line.strip() else "")
            else:
                out.append(line)
            previous = line
            continue

        if stripped.startswith("```"):
            if in_list and 0 < indent < 4:
                fence_shift, fence_indent = "list", indent
                out.append("")                            # an indented code block needs a blank line before it
            else:
                if indent == 0:
                    in_list = False
                fence_shift, fence_indent = 0, 0
                out.append(line)
            previous = line
            continue

        is_item = bool(_LIST_ITEM.match(line))
        if is_item and indent == 0:
            if previous.strip() and not _LIST_ITEM.match(previous) and not previous.startswith((" ", "\t", "|")):
                out.append("")
            in_list = True
        elif in_list and line.strip() and 0 < indent < 4:
            line = "    " + stripped                      # nested item or continuation
        elif line.strip() and indent == 0 and not is_item:
            in_list = False
        out.append(line)
        previous = line
    return "\n".join(out) + "\n"


def md_to_html(text: str) -> str:
    body = markdown.markdown(loosen_lists(text), extensions=["tables", "fenced_code", "sane_lists"])
    # Cross-links between lessons become in-page anchors.
    body = re.sub(r'href="([\w.-]+\.md)"', lambda m: f'href="#{ANCHOR.get(m.group(1), "start")}"', body)
    # External links open in a new tab.
    body = re.sub(r'<a href="(https?://[^"]+)"', r'<a href="\1" target="_blank" rel="noopener"', body)
    # Wide tables and code scroll inside their own box, never the page.
    body = body.replace("<table>", '<div class="scroll"><table>').replace("</table>", "</table></div>")
    body = body.replace("<pre>", '<pre class="scroll">')
    return body


def api_reference() -> str:
    sys.path.insert(0, str(ROOT))
    from pmagent.web.app import create_app

    spec = create_app(assistant_factory=lambda recorder: None).openapi()
    rows = []
    for path, operations in spec["paths"].items():
        for method, op in operations.items():
            codes = ", ".join(sorted(op.get("responses", {})))
            summary = op.get("summary", "")
            rows.append(
                f"<tr><td><code>{method.upper()}</code></td><td><code>{html.escape(path)}</code></td>"
                f"<td>{html.escape(summary)}</td><td class=\"num\">{codes}</td></tr>"
            )
    return (
        '<p>Generated from the app\'s own OpenAPI document (<code>/api/v1/openapi.json</code>), '
        "so this list is exactly what the server exposes. Every error is "
        "<code>application/problem+json</code>. Besides the codes listed per route, any "
        "route can return <code>400</code> (a Host this server doesn't answer to), "
        "<code>403</code> (a write from another origin) and, when "
        "<code>PMAGENT_API_TOKEN</code> is set, <code>401</code> (every route except "
        "<code>/api/v1/health</code>).</p>"
        '<div class="scroll"><table><thead><tr><th>Method</th><th>Path</th><th>What it does</th>'
        "<th>Status codes</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )


def figure(name: str, caption: str) -> str:
    path = SHOTS / name
    if not path.exists():
        return ""
    raw = path.read_bytes()
    width, height = struct.unpack(">II", raw[16:24])   # PNG IHDR: reserve the space, no layout shift
    data = base64.b64encode(raw).decode()
    return (f'<figure><img src="data:image/png;base64,{data}" alt="{html.escape(caption)}" '
            f'width="{width}" height="{height}" loading="lazy">'
            f'<figcaption>{html.escape(caption)}</figcaption></figure>')


def window(anchor: str, title: str, body: str) -> str:
    return (f'<section class="win" id="{anchor}" aria-labelledby="{anchor}-t">'
            f'<div class="bar"><span class="box" aria-hidden="true"></span>'
            f'<h2 class="bar__t" id="{anchor}-t">{html.escape(title)}</h2></div>'
            f'<div class="doc">{body}</div></section>')


def build() -> str:
    sections, nav = [], []
    nav.append('<li class="part">Part 1 · Understand</li>')
    for name, anchor, title in ORDER:
        if name is None:                                   # a part divider: nav only
            nav.append(f'<li class="part">{html.escape(title)}</li>')
            continue
        body = md_to_html((LEARN / name).read_text(encoding="utf-8"))
        body = re.sub(r"^<h1>.*?</h1>", "", body, count=1, flags=re.S)  # the window bar is the title
        for fig_name, fig_anchor, caption in FIGURES:
            if fig_anchor == anchor:
                body += figure(fig_name, caption)
        sections.append(window(anchor, title, body))
        nav.append(f'<li><a href="#{anchor}">{html.escape(title)}</a></li>')
        if anchor == "lesson-11":
            sections.append(window("api", "API reference", api_reference()))
            nav.append('<li><a href="#api">API reference</a></li>')

    return (PAGE
            .replace("{{NAV}}", "\n".join(nav))
            .replace("{{SECTIONS}}", "\n".join(sections)))


PAGE = """<title>PM Agent Strands Course</title>
<meta name="description" content="Learn Strands Agents from a real multi-agent app: understand it (lessons 1–11), rebuild it step by step, then extend it with logging, persistence, memory, evals and a harness (lessons 12–17).">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Schibsted+Grotesk:wght@400;500;600&family=Silkscreen&display=swap">
<style>
:root {
  --ink: #1e1e1e; --paper: #fefefe; --desk: #dedede; --rule: #c4c4c4; --muted: #5d5a5c;
  --accent: #f386a1; --accent-2: #d45bb6; --sheet: #f6f5f5;
  --mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  --sans: "Schibsted Grotesk", "Helvetica Neue", Arial, system-ui, sans-serif;
  --pixel: "Silkscreen", "JetBrains Mono", ui-monospace, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ink: #fefefe; --paper: #1e1e1e; --desk: #121212; --rule: #4a4749; --muted: #b3aeb1;
    --accent: #f386a1; --accent-2: #e58ad0; --sheet: #2a2829; color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --ink: #fefefe; --paper: #1e1e1e; --desk: #121212; --rule: #4a4749; --muted: #b3aeb1;
  --accent: #f386a1; --accent-2: #e58ad0; --sheet: #2a2829; color-scheme: dark;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--desk); color: var(--ink); font: 400 16px/1.6 var(--sans); }
.wrap { max-width: 1180px; margin: 0 auto; padding-inline: 16px; padding-block: 24px 64px;
  display: grid; grid-template-columns: 250px minmax(0, 1fr); gap: 24px; align-items: start; }
.hero { grid-column: 1 / -1; }
.hero h1 { font: 400 clamp(1.4rem, 3.4vw, 2.2rem)/1.2 var(--pixel); margin: 0 0 10px; text-wrap: balance; }
.hero p { max-width: 68ch; margin: 0 0 8px; }
.hero code { font-family: var(--mono); font-size: .88em; background: var(--paper); border: 1px solid var(--rule); padding: 0 4px; }
.win { background: var(--paper); border: 1px solid var(--ink); box-shadow: 3px 3px 0 var(--ink); margin-bottom: 28px; scroll-margin-top: 16px; }
.bar { display: flex; align-items: center; gap: 10px; padding: 5px 10px; border-bottom: 1px solid var(--ink);
  background: repeating-linear-gradient(0deg, var(--ink) 0 1px, transparent 1px 3px); }
.box { width: 13px; height: 13px; border: 1px solid var(--ink); background: var(--paper); flex: none; }
.bar__t { margin: 0; padding: 0 8px; background: var(--paper); font: 400 .8rem/1.5 var(--pixel); letter-spacing: .04em; }
nav.toc { position: sticky; top: 16px; }
nav.toc ol { list-style: none; margin: 0; padding: 8px 0; }
nav.toc a { display: block; padding: 4px 12px; font: 400 .82rem/1.4 var(--mono); color: var(--ink); text-decoration: none; }
nav.toc a:hover, nav.toc a:focus-visible { background: var(--ink); color: var(--paper); }
nav.toc li.part { padding: 10px 12px 2px; font: 400 .72rem/1.3 var(--pixel); color: var(--muted); text-transform: uppercase; }
.doc { padding: 8px 24px 20px; max-width: 76ch; }
.doc h2 { font: 600 1.2rem/1.3 var(--sans); margin: 1.4em 0 .4em; text-wrap: balance; }
.doc h3 { font: 600 1rem/1.3 var(--sans); margin: 1.2em 0 .3em; }
.doc p, .doc li { max-width: 70ch; }
.doc code { font-family: var(--mono); font-size: .86em; background: var(--sheet); padding: 0 3px; }
.doc pre { background: var(--sheet); border: 1px solid var(--rule); padding: 12px; font: 400 .8rem/1.5 var(--mono); }
.doc pre code { background: none; padding: 0; font-size: inherit; }
.scroll { overflow-x: auto; max-width: 100%; }
.doc table { border-collapse: collapse; font-size: .85rem; margin: 10px 0; }
.doc th, .doc td { border: 1px solid var(--rule); padding: 5px 8px; text-align: left; vertical-align: top; }
.doc th { background: var(--sheet); font: 500 .78rem/1.4 var(--mono); }
.doc td.num { font-family: var(--mono); font-variant-numeric: tabular-nums; white-space: nowrap; }
.doc a { color: var(--ink); text-decoration-color: var(--accent-2); text-underline-offset: 2px; }
.doc blockquote { border-left: 3px solid var(--accent); margin: 10px 0; padding: 2px 12px; color: var(--muted); }
figure { margin: 18px 0 6px; border: 1px solid var(--ink); background: var(--sheet); }
figure img { display: block; width: 100%; max-width: 100%; height: auto; border-bottom: 1px solid var(--ink); }
figcaption { padding: 6px 10px; font: 400 .78rem/1.45 var(--mono); color: var(--muted); }
a:focus-visible { outline: 2px solid var(--accent-2); outline-offset: 2px; }
@media (max-width: 860px) {
  .wrap { grid-template-columns: minmax(0, 1fr); }
  nav.toc { position: static; }
  .doc { padding: 6px 14px 16px; }
}
@media (prefers-reduced-motion: no-preference) { html { scroll-behavior: smooth; } }
</style>

<div class="wrap">
  <header class="hero">
    <h1>Learning Strands Agents, one real app at a time</h1>
    <p>This course teaches the Strands Agents SDK through <strong>PO Agent</strong>, a multi-agent
      project-management assistant rewritten from LangGraph. It works with real Jira and Confluence
      data, gates every write behind a human, writes PRDs with a writer↔reviewer loop, and has a web
      UI that streams its thinking chain.</p>
    <p>Each lesson explains a concept, then shows where it lives in the repo, how the LangGraph
      original did it, and gives exercises. Everything runs offline with no API key: the
      examples (<code>uv run docs/learning/examples/&lt;lesson&gt;_*.py</code>) and the web
      demo (<code>uv run scripts/demo_web.py</code>). Setup and the words this course uses
      are in <a href="#start">Start here</a>.</p>
    <p>The course has three parts. <strong>Understand</strong> (lessons 1–11) explains the finished
      app. <strong>Rebuild</strong> has you build it again from an empty folder, with the repo's own
      tests as checkpoints. <strong>Extend</strong> (lessons 12–17) are guided builds of what the
      project has designed but not built: a run log, persistence, long-term memory, evals, a Claude
      Code harness and Jev.</p>
  </header>
  <nav class="toc win" aria-label="Lessons">
    <div class="bar"><span class="box" aria-hidden="true"></span><span class="bar__t">Contents</span></div>
    <ol>
{{NAV}}
    </ol>
  </nav>
  <main>
{{SECTIONS}}
  </main>
</div>
"""


def main(argv: list[str]) -> int:
    page = build()
    if "--check" in argv:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != page:
            print("docs/learning/course.html is stale — run: uv run scripts/build_course.py")
            return 1
        print("course.html is up to date")
        return 0
    OUT.write_text(page, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(page) // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
