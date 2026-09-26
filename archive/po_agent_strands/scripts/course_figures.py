"""
Screenshot the web UI on DEMO data for the published course page.

    PMAGENT_CHROMIUM_LIBS=... uv run --with playwright scripts/course_figures.py

Everything is offline and synthetic, so the published course contains no real
Jira data or colleagues' names:

* the offline demo in scripts/demo_web.py: the real FastAPI app, PMAssistant,
  Strands agents and approval gate, with a ScriptedModel and a fake Jira client.

The screenshots in docs/evidence/ui/ come from live runs against the real
tenant. They are the evidence, and are kept local. These are the illustrations.
Output: docs/learning/figures/*.png, which scripts/build_course.py embeds.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

from scripts.demo_web import start_in_background  # noqa: E402

OUT = ROOT / "docs" / "learning" / "figures"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    srv, base = start_in_background()
    env = dict(os.environ)
    if os.environ.get("PMAGENT_CHROMIUM_LIBS"):
        env["LD_LIBRARY_PATH"] = os.environ["PMAGENT_CHROMIUM_LIBS"]
    with sync_playwright() as p:
        browser = p.chromium.launch(env=env)

        def fresh(width=1360, height=820):
            page = browser.new_page(viewport={"width": width, "height": height})
            page.goto(base + "/")
            page.wait_for_selector("#meta-line:not(:text('connecting…'))")
            return page

        def ask(page, message):
            page.fill("#message", message)
            page.press("#message", "Enter")

        page = fresh()
        ask(page, "Is the FX epic at risk? Weigh blockers against unstarted work.")
        page.wait_for_selector(".pill--completed")
        page.screenshot(path=OUT / "thinking-chain.png")

        page = fresh()
        ask(page, "Post a comment on DEMO-101 asking what is blocking it.")
        page.wait_for_selector(".approval")
        page.screenshot(path=OUT / "approval.png")

        page = fresh()
        ask(page, "Write a PRD from these notes: daily FX rates by 7am, AUD base, alert if late.")
        page.wait_for_selector(".pill--completed")
        page.screenshot(path=OUT / "prd-loop.png")

        page.set_viewport_size({"width": 400, "height": 820})
        page.reload()
        page.wait_for_selector(".pill--completed")
        page.screenshot(path=OUT / "phone.png")
        browser.close()
    srv.should_exit = True
    print(f"wrote {sorted(p.name for p in OUT.glob('*.png'))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
