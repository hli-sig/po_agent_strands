"""
Screenshot the web UI in real states, by driving it like a person would.

    uv run --with playwright scripts/ui_screenshots.py http://127.0.0.1:8765 docs/evidence/ui [--reasoning]

Needs Chromium: `uv run --with playwright python -m playwright install chromium`.
On WSL without its system libraries, fetch them without root
(`apt-get download libnspr4 libnss3 libasound2t64`, `dpkg -x` each) and set
LD_LIBRARY_PATH to the extracted `usr/lib/x86_64-linux-gnu`.

Point it only at a write-blocked server (`scripts/smoke.py --web`): it clicks
Approve. Also records every browser console error — a CSP violation would show
up here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


def main(base: str, out_dir: str, reasoning: bool) -> int:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    console: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.on("console", lambda m: console.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: console.append(f"pageerror: {e}"))
        page.goto(base + "/")
        page.wait_for_selector("#meta-line:not(:text('connecting…'))")
        page.screenshot(path=out / "01-start.png")

        def ask(text: str) -> None:
            page.fill("#message", text)
            page.press("#message", "Enter")

        if reasoning:
            ask("Look at sprint 31. Weigh the blocked work against what was completed, decide whether "
                "the blockers or the unstarted work is the bigger delivery risk, and justify it in one paragraph.")
            page.wait_for_selector(".reasoning", timeout=180_000)
            page.wait_for_timeout(2500)
            page.screenshot(path=out / "02-reasoning-streaming.png")
            page.wait_for_selector(".pill--completed", timeout=240_000)
            page.screenshot(path=out / "03-reasoning-done.png")
        else:
            ask("What's the status of sprint 31?")
            page.wait_for_selector(".pill--completed", timeout=240_000)
            page.screenshot(path=out / "02-sprint-chain.png")

            ask('Post the comment "Smoke test from the Strands web UI - please ignore" on CSCI-1934. '
                "I confirm the text - call the tool now.")
            page.wait_for_selector(".approval", timeout=240_000)
            page.screenshot(path=out / "03-approval-card.png")
            page.click(".approval >> text=Reject")
            page.wait_for_selector(".pill--completed", timeout=240_000)
            page.screenshot(path=out / "04-rejected.png")

            ask("Write a PRD from these notes: the warehouse team wants a daily stock-on-hand snapshot "
                "in Snowflake by 6am, per store and SKU, with a Slack alert when a store is missing.")
            # (wait_for_function would eval a string, which the page's CSP rightly forbids)
            page.locator(".msg--agent").last.locator("table").first.wait_for(timeout=300_000)
            page.wait_for_selector(".pill--completed", timeout=60_000)
            page.screenshot(path=out / "05-prd.png", full_page=False)

            page.set_viewport_size({"width": 400, "height": 860})
            page.reload()
            page.wait_for_selector(".msg--agent")
            page.screenshot(path=out / "06-phone-after-reload.png")

        browser.close()

    (out / ("console-reasoning.json" if reasoning else "console.json")).write_text(json.dumps(console, indent=2))
    print(f"screenshots in {out}; console errors/warnings: {len(console)}")
    for line in console:
        print("  ", line)
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2], "--reasoning" in sys.argv))
