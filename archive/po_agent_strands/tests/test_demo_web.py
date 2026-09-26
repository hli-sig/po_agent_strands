"""The offline demo that lessons 10–11 rely on (scripts/demo_web.py) must keep working."""

import pytest
from fastapi.testclient import TestClient

import pmagent.tools.jira.tools_read as tools_read
import pmagent.tools.jira.tools_write as tools_write
from pmagent.web.app import create_app
from scripts import demo_web
from tests.test_web_api import events, kinds


@pytest.fixture
def client(monkeypatch):
    # What demo_web.configure() does, but reverted after the test.
    monkeypatch.setattr(tools_read, "get_client", lambda: demo_web.DemoJira())
    monkeypatch.setattr(tools_write, "get_client", lambda: demo_web.DemoJira())
    demo_web.DemoJira.comments = []
    return TestClient(create_app(demo_web.factory), base_url="http://127.0.0.1")


def turn(client, cid, message):
    body = client.post(f"/api/v1/conversations/{cid}/turns", json={"message": message}).json()
    return body, events(client, body["events_url"])


def test_the_three_demo_prompts_and_a_fallback(client):
    cid = client.post("/api/v1/conversations").json()["id"]

    _, risk = turn(client, cid, "Is the FX epic at risk?")
    assert {"reasoning.delta", "tool.started", "turn.completed"} <= set(kinds(risk))

    body, comment = turn(client, cid, "Post a comment on DEMO-101")
    assert comment[-1]["event"] == "approval.required"
    client.post(f"/api/v1/conversations/{cid}/approvals/{comment[-1]['data']['approval_id']}/decision",
                json={"decision": "reject"})
    after = events(client, body["events_url"], last_id=comment[-1]["id"])
    assert "nothing was posted" in after[-1]["data"]["reply"]
    assert demo_web.DemoJira.comments == []

    _, prd = turn(client, cid, "Write a PRD from these notes: FX by 7am, alert if late.")
    steps = [e["data"]["step"] for e in prd if e["event"] == "prd.step"]
    assert steps.count("reviewer.finished") == 2 and prd[-1]["data"]["reply"].startswith("# Daily FX feed")

    _, other = turn(client, cid, "hello")
    assert "offline demo" in other[-1]["data"]["reply"]
