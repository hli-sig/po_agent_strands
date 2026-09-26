"""The HTTP API, end to end, offline.

Real FastAPI app, real PMAssistant, real Strands agents and gate; the model is a
ScriptedModel and the router a stub. Streams are read with TestClient — the
server closes them when the turn reaches a resting state, so reads terminate.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from strands.types.exceptions import ContextWindowOverflowException

import pmagent.tools.jira.tools_read as tools_read
import pmagent.tools.jira.tools_write as tools_write
from pmagent.assistant import PMAssistant
from pmagent.cli.approval import describe_write, unchecked_scope_warning
from pmagent.gate import REJECTION_MESSAGE
from pmagent.schemas import PRD, ReviewResult
from pmagent.web.app import STATIC, create_app
from tests.fakes import ScriptedModel, call, reasoning, structured, text

BASE = "http://127.0.0.1"


class FakeJira:
    comments: list = []

    def add_comments(self, keys, comment):
        FakeJira.comments.append((keys, comment))
        return [{"key": k, "error": None} for k in keys]

    def list_transitions(self, key):
        return [{"name": "Done", "to_status": "Done"}]


@pytest.fixture(autouse=True)
def fake_jira(monkeypatch):
    FakeJira.comments = []
    monkeypatch.setattr(tools_write, "get_client", lambda: FakeJira())
    monkeypatch.setattr(tools_read, "get_client", lambda: FakeJira())


def make_client(*scripts, route="ticket", **app_kwargs):
    """One ScriptedModel per conversation, in creation order."""
    queue = list(scripts)
    models: list[ScriptedModel] = []

    def factory(recorder):
        model = ScriptedModel(queue.pop(0) if queue else [])
        models.append(model)
        return PMAssistant(model=model, classify=lambda m, p: route, **recorder.assistant_kwargs())

    client = TestClient(create_app(factory, **app_kwargs), base_url=BASE)
    client.models = models
    return client


def events(client, url, last_id=None):
    headers = {"Last-Event-ID": str(last_id)} if last_id else {}
    out = []
    with client.stream("GET", url, headers=headers) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        current = {}
        for line in response.iter_lines():
            if line.startswith("id:"):
                current["id"] = int(line[3:].strip())
            elif line.startswith("event:"):
                current["event"] = line[6:].strip()
            elif line.startswith("data:"):
                current["data"] = json.loads(line[5:].strip())
            elif line == "" and "event" in current:
                out.append(current)
                current = {}
    return out


def kinds(evts):
    return [e["event"] for e in evts]


def start(client, message="hi", **headers):
    conv = client.post("/api/v1/conversations").json()
    turn = client.post(f"/api/v1/conversations/{conv['id']}/turns", json={"message": message},
                       headers=headers)
    return conv, turn


COMMENT = [call("add_jira_comment", issue_keys=["CSCI-1"], comment="chasing this")]


# -- conversations -----------------------------------------------------------------


def test_create_get_delete_conversation():
    client = make_client([])
    created = client.post("/api/v1/conversations")
    assert created.status_code == 201
    cid = created.json()["id"]
    assert created.headers["location"] == f"/api/v1/conversations/{cid}"
    assert cid.startswith("conv_")

    got = client.get(created.headers["location"]).json()
    assert got["status"] == "idle" and got["messages"] == [] and got["pending_approval"] is None

    assert client.delete(f"/api/v1/conversations/{cid}").status_code == 204
    gone = client.get(f"/api/v1/conversations/{cid}")
    assert gone.status_code == 404
    assert gone.headers["content-type"] == "application/problem+json"
    assert gone.json()["type"] == "/problems/not-found"


# -- a read-only turn: the thinking chain ---------------------------------------------


def test_a_turn_is_accepted_and_streams_its_thinking_chain():
    client = make_client([
        [reasoning("The user wants transitions; look them up."),
         call("list_jira_transitions", issue_key="CSCI-1")],
        [text("CSCI-1 can move to Done.")],
    ], route="query")
    conv, turn = start(client, "where can CSCI-1 go?")

    assert turn.status_code == 202
    body = turn.json()
    assert turn.headers["location"] == body["url"]
    assert body["status"] == "running" and body["id"].startswith("turn_")

    evts = events(client, body["events_url"])
    assert kinds(evts) == [
        "turn.started", "route.decided", "reasoning.delta", "message.completed",
        "tool.started", "tool.finished", "text.delta", "message.completed",
        "usage", "turn.completed",
    ]
    assert [e["id"] for e in evts] == list(range(1, len(evts) + 1))
    by_kind = {e["event"]: e["data"] for e in evts}
    assert by_kind["route.decided"] == {**by_kind["route.decided"], "route": "query",
                                        "lane": "query_agent", "reason": "custom"}
    assert by_kind["reasoning.delta"]["text"] == "The user wants transitions; look them up."
    assert by_kind["tool.started"]["input"] == {"issue_key": "CSCI-1"}
    finished = by_kind["tool.finished"]
    assert finished["status"] == "success" and "CSCI-1 can move to" in finished["output"]
    assert isinstance(finished["duration_ms"], int)
    assert by_kind["turn.completed"]["reply"] == "CSCI-1 can move to Done."

    final = client.get(body["url"]).json()
    assert final["status"] == "completed" and final["reply"] == "CSCI-1 can move to Done."
    assert final["event_count"] == len(evts) and len(final["events"]) == len(evts)


# -- the write gate over HTTP -----------------------------------------------------------


def _await_approval(client, turn_body):
    evts = events(client, turn_body["events_url"])
    assert evts[-1]["event"] == "approval.required"
    return evts


def test_approve_flow_resumes_the_same_turn_and_runs_the_write_once():
    client = make_client([COMMENT, [text("Posted.")]])
    conv, turn = start(client, "comment 'chasing this' on CSCI-1")
    body = turn.json()
    evts = _await_approval(client, body)
    assert FakeJira.comments == []                                  # nothing ran yet
    approval_id = evts[-1]["data"]["approval_id"]
    assert approval_id.startswith("apr_")

    conversation = client.get(f"/api/v1/conversations/{conv['id']}").json()
    assert conversation["status"] == "awaiting_approval"
    assert conversation["pending_approval"]["id"] == approval_id

    decided = client.post(f"/api/v1/conversations/{conv['id']}/approvals/{approval_id}/decision",
                          json={"decision": "approve"})
    assert decided.status_code == 202
    assert decided.headers["location"] == body["url"]

    more = events(client, body["events_url"], last_id=evts[-1]["id"])
    assert more[0]["id"] == evts[-1]["id"] + 1                      # no replay
    assert kinds(more) == ["approval.decided", "tool.started", "tool.finished", "text.delta",
                           "message.completed", "usage", "turn.completed"]
    assert more[0]["data"]["decision"] == "approved"
    assert FakeJira.comments == [(["CSCI-1"], "chasing this")]
    assert client.get(body["url"]).json()["status"] == "completed"


def test_reject_never_runs_the_write():
    client = make_client([COMMENT, [text("OK, nothing posted.")]])
    conv, turn = start(client)
    evts = _await_approval(client, turn.json())
    approval_id = evts[-1]["data"]["approval_id"]

    client.post(f"/api/v1/conversations/{conv['id']}/approvals/{approval_id}/decision",
                json={"decision": "reject"})
    more = events(client, turn.json()["events_url"], last_id=evts[-1]["id"])

    assert FakeJira.comments == []
    assert "tool.started" not in kinds(more)                       # cancelled before execution
    assert more[0]["data"]["decision"] == "rejected"
    rejected = client.get(f"/api/v1/conversations/{conv['id']}/approvals/{approval_id}").json()
    assert rejected["status"] == "rejected"
    seen = client.models[0].requests[-1]["messages"][-1]["content"][0]["toolResult"]
    assert seen["content"][0]["text"] == REJECTION_MESSAGE


def test_the_approval_text_is_exactly_what_the_cli_prints():
    drafts = [{"summary": f"S{i}", "description": "d", "source_issue": f"CSCI-{i}"} for i in range(5)]
    client = make_client([[call("create_jira_issues", drafts=drafts)]])
    conv, turn = start(client, "for CSCI-1, CSCI-2 and CSCI-3 make cards")
    evts = _await_approval(client, turn.json())
    approval = client.get(
        f"/api/v1/conversations/{conv['id']}/approvals/{evts[-1]['data']['approval_id']}").json()

    call_ = approval["calls"][0]
    assert approval["descriptions"] == [describe_write(call_)]
    messages = [{"role": "user", "content": [{"text": "for CSCI-1, CSCI-2 and CSCI-3 make cards"}]}]
    assert approval["warning"] == unchecked_scope_warning(messages, [call_])
    assert "declared no scope" in approval["warning"]


def test_two_approvals_in_one_turn_get_distinct_ids():
    client = make_client([COMMENT, COMMENT, [text("Both posted.")]])
    conv, turn = start(client)
    url = turn.json()["events_url"]
    first = _await_approval(client, turn.json())
    a1 = first[-1]["data"]["approval_id"]
    client.post(f"/api/v1/conversations/{conv['id']}/approvals/{a1}/decision", json={"decision": "approve"})
    second = events(client, url, last_id=first[-1]["id"])
    assert second[-1]["event"] == "approval.required"
    a2 = second[-1]["data"]["approval_id"]
    assert a2 != a1
    client.post(f"/api/v1/conversations/{conv['id']}/approvals/{a2}/decision", json={"decision": "approve"})
    events(client, url, last_id=second[-1]["id"])
    assert len(FakeJira.comments) == 2
    assert client.get(turn.json()["url"]).json()["approval_ids"] == [a1, a2]


# -- conflicts ---------------------------------------------------------------------------


def test_a_new_message_while_an_approval_waits_is_a_409_naming_it():
    client = make_client([COMMENT])
    conv, turn = start(client)
    evts = _await_approval(client, turn.json())
    again = client.post(f"/api/v1/conversations/{conv['id']}/turns", json={"message": "other"})
    assert again.status_code == 409
    problem = again.json()
    assert problem["type"] == "/problems/approval-pending"
    assert problem["approval"] == evts[-1]["data"]["approval_id"]


def test_decisions_are_idempotent_but_cannot_be_changed():
    client = make_client([COMMENT, [text("Posted.")]])
    conv, turn = start(client)
    evts = _await_approval(client, turn.json())
    url = f"/api/v1/conversations/{conv['id']}/approvals/{evts[-1]['data']['approval_id']}/decision"
    assert client.post(url, json={"decision": "approve"}).status_code == 202
    events(client, turn.json()["events_url"], last_id=evts[-1]["id"])
    assert client.post(url, json={"decision": "approve"}).status_code == 202     # same again: fine
    changed = client.post(url, json={"decision": "reject"})
    assert changed.status_code == 409 and changed.json()["type"] == "/problems/approval-not-pending"
    assert len(FakeJira.comments) == 1


def test_a_turn_while_one_is_running_is_a_409_and_delete_waits():
    gate = threading.Event()

    def parked(messages):
        assert gate.wait(timeout=10), "test never released the model"
        return [text("finally")]

    client = make_client([parked], route="query")
    try:
        conv, turn = start(client, "slow one")
        again = client.post(f"/api/v1/conversations/{conv['id']}/turns", json={"message": "x"})
        assert again.status_code == 409 and again.json()["type"] == "/problems/turn-in-progress"
        assert client.delete(f"/api/v1/conversations/{conv['id']}").status_code == 409
    finally:
        gate.set()
    assert kinds(events(client, turn.json()["events_url"]))[-1] == "turn.completed"


# -- failure cleanup (review finding R2) ----------------------------------------------------


def test_a_failed_resume_abandons_the_approval_and_the_conversation_keeps_working():
    def outage(messages):
        raise RuntimeError("model outage")

    client = make_client([COMMENT, outage, [text("back")]])
    conv, turn = start(client)
    evts = _await_approval(client, turn.json())
    approval_id = evts[-1]["data"]["approval_id"]
    decision_url = f"/api/v1/conversations/{conv['id']}/approvals/{approval_id}/decision"
    client.post(decision_url, json={"decision": "approve"})
    more = events(client, turn.json()["events_url"], last_id=evts[-1]["id"])

    assert more[-1]["event"] == "turn.failed"
    problem = more[-1]["data"]["problem"]
    assert problem["type"] == "/problems/turn-failed" and "model outage" in problem["detail"]
    assert client.get(f"/api/v1/conversations/{conv['id']}").json()["status"] == "idle"

    # the tool ran before the model failed; the approval itself was decided
    assert client.get(f"/api/v1/conversations/{conv['id']}/approvals/{approval_id}").json()["status"] == "approved"
    nxt = client.post(f"/api/v1/conversations/{conv['id']}/turns", json={"message": "again"})
    assert nxt.status_code == 202
    assert kinds(events(client, nxt.json()["events_url"]))[-1] == "turn.completed"


def test_a_failure_while_an_approval_is_pending_marks_it_abandoned():
    client = make_client([COMMENT])
    conv, turn = start(client)
    evts = _await_approval(client, turn.json())
    approval_id = evts[-1]["data"]["approval_id"]
    session = client.app.state.store.get(conv["id"])
    session._fail(session.turns[turn.json()["id"]], "turn-failed", "simulated")

    approval = client.get(f"/api/v1/conversations/{conv['id']}/approvals/{approval_id}").json()
    assert approval["status"] == "abandoned"
    late = client.post(approval["decision_url"], json={"decision": "approve"})
    assert late.status_code == 409 and late.json()["type"] == "/problems/approval-not-pending"
    assert FakeJira.comments == []


def test_deleting_a_conversation_abandons_its_approval():
    client = make_client([COMMENT])
    conv, turn = start(client)
    _await_approval(client, turn.json())
    session = client.app.state.store.get(conv["id"])
    assert client.delete(f"/api/v1/conversations/{conv['id']}").status_code == 204
    assert [a.status for a in session.approvals.values()] == ["abandoned"]
    assert not session.assistant.pending


def test_a_context_overflow_is_its_own_problem_type():
    def overflow(messages):
        raise ContextWindowOverflowException("too long")

    client = make_client([overflow], route="query")
    conv, turn = start(client)
    evts = events(client, turn.json()["events_url"])
    assert evts[-1]["data"]["problem"]["type"] == "/problems/context-full"


# -- idempotency -------------------------------------------------------------------------


def test_an_idempotent_retry_returns_the_same_turn_and_runs_the_model_once():
    client = make_client([[text("once")]], route="query")
    conv = client.post("/api/v1/conversations").json()
    url = f"/api/v1/conversations/{conv['id']}/turns"
    first = client.post(url, json={"message": "hello"}, headers={"Idempotency-Key": "k1"})
    events(client, first.json()["events_url"])
    retry = client.post(url, json={"message": "hello"}, headers={"Idempotency-Key": "k1"})

    assert retry.status_code == 202 and retry.json()["id"] == first.json()["id"]
    assert retry.headers["location"] == first.headers["location"]
    assert len(client.get(url).json()["items"]) == 1
    assert client.models[0].remaining == 0 and len(client.models[0].requests) == 1

    reuse = client.post(url, json={"message": "different"}, headers={"Idempotency-Key": "k1"})
    assert reuse.status_code == 422 and reuse.json()["type"] == "/problems/idempotency-key-reuse"


# -- the requirements lane ------------------------------------------------------------------


def test_the_prd_loop_appears_in_the_thinking_chain():
    client = make_client([
        [structured(PRD, title="FX feed", objective="Load FX.")],
        [structured(ReviewResult, approved=False, missing_requirements=["7am SLA"])],
        [structured(PRD, title="FX feed", objective="Load FX by 7am.")],
        [structured(ReviewResult, approved=True)],
    ], route="requirements")
    conv, turn = start(client, "notes: FX by 7am")
    evts = events(client, turn.json()["events_url"])
    steps = [e["data"]["step"] for e in evts if e["event"] == "prd.step"]
    assert steps == ["writer.started", "writer.finished", "reviewer.finished",
                     "writer.started", "writer.finished", "reviewer.finished", "render"]
    missing = [e["data"] for e in evts if e["event"] == "prd.step" and e["data"]["step"] == "reviewer.finished"]
    assert missing[0]["missing_requirements"] == ["7am SLA"] and missing[1]["approved"] is True
    done = evts[-1]["data"]
    assert done["reply"].startswith("# FX feed") and done["reply_is_markdown_document"] is True


# -- validation, errors, auth ---------------------------------------------------------------


@pytest.mark.parametrize("payload", [{"message": ""}, {"message": "   "}, {}, {"message": "x" * 20_001}])
def test_invalid_messages_are_422_problems(payload):
    client = make_client([])
    conv = client.post("/api/v1/conversations").json()
    response = client.post(f"/api/v1/conversations/{conv['id']}/turns", json=payload)
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "/problems/validation" and response.json()["errors"]


def test_a_text_plain_body_is_refused():
    client = make_client([])
    conv = client.post("/api/v1/conversations").json()
    response = client.post(f"/api/v1/conversations/{conv['id']}/turns",
                           content='{"message": "hi"}', headers={"Content-Type": "text/plain"})
    assert response.status_code == 422


def test_problem_types_dereference_to_a_page():
    client = make_client([])
    page = client.get("/problems/approval-pending")
    assert page.status_code == 200 and "An approval is waiting" in page.text


def test_bearer_token_when_configured():
    client = make_client([], api_token="s3cret")
    assert client.get("/api/v1/health").status_code == 200                 # liveness stays open
    denied = client.post("/api/v1/conversations")
    assert denied.status_code == 401
    assert denied.json()["type"] == "/problems/unauthorized"
    assert denied.headers["www-authenticate"] == "Bearer"
    assert client.post("/api/v1/conversations", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/api/v1/conversations",
                       headers={"Authorization": "Bearer s3cret"}).status_code == 201


# -- browser security (review findings R3, R4) ---------------------------------------------


def test_a_foreign_host_is_refused_dns_rebinding():
    client = make_client([])
    response = client.get("/api/v1/health", headers={"Host": "evil.example"})
    assert response.status_code == 400
    assert response.json()["type"] == "/problems/bad-host"
    assert client.get("/api/v1/health", headers={"Host": "localhost:8000"}).status_code == 200


def test_a_foreign_origin_cannot_approve():
    client = make_client([COMMENT])
    conv, turn = start(client)
    evts = _await_approval(client, turn.json())
    url = f"/api/v1/conversations/{conv['id']}/approvals/{evts[-1]['data']['approval_id']}/decision"
    for origin in ("http://evil.example", "null", "http://127.0.0.1:9999"):
        refused = client.post(url, json={"decision": "approve"}, headers={"Origin": origin})
        assert refused.status_code == 403, origin
        assert refused.json()["type"] == "/problems/forbidden-origin"
    assert FakeJira.comments == []
    ok = client.post(url, json={"decision": "approve"}, headers={"Origin": BASE})
    assert ok.status_code == 202


def test_security_headers_strict_on_the_page_and_api_relaxed_only_on_docs():
    client = make_client([])
    for path in ("/", "/api/v1/health"):
        headers = client.get(path).headers
        assert "script-src 'self';" in headers["content-security-policy"]
        assert "img-src 'self' data:;" in headers["content-security-policy"]
        assert headers["x-content-type-options"] == "nosniff"
    docs = client.get("/api/docs").headers["content-security-policy"]
    assert "cdn.jsdelivr.net" in docs
    assert client.get("/api/redoc").status_code == 404


def test_the_openapi_document_lists_every_route():
    spec = make_client([]).get("/api/v1/openapi.json").json()
    paths = set(spec["paths"])
    assert paths == {
        "/api/v1/health", "/api/v1/meta", "/api/v1/conversations",
        "/api/v1/conversations/{conversation_id}",
        "/api/v1/conversations/{conversation_id}/turns",
        "/api/v1/conversations/{conversation_id}/turns/{turn_id}",
        "/api/v1/conversations/{conversation_id}/turns/{turn_id}/events",
        "/api/v1/conversations/{conversation_id}/approvals/{approval_id}",
        "/api/v1/conversations/{conversation_id}/approvals/{approval_id}/decision",
    }
    create_turn = spec["paths"]["/api/v1/conversations/{conversation_id}/turns"]["post"]
    assert "202" in create_turn["responses"] and "409" in create_turn["responses"]


# -- the static page -------------------------------------------------------------------------


def test_the_page_has_no_inline_script_style_or_handlers():
    """The strict CSP would block them — and they are how injected markup would run."""
    for page in STATIC.glob("*.html"):
        html = page.read_text(encoding="utf-8")
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), page.name
        assert not re.search(r"\son\w+\s*=", html, re.I), page.name
        assert not re.search(r"\sstyle\s*=", html, re.I), page.name
        assert "<style" not in html, page.name


def test_vendored_libraries_match_their_recorded_hashes():
    readme = (STATIC / "vendor" / "README.md").read_text()
    for name in ("marked.min.js", "purify.min.js"):
        digest = hashlib.sha256((STATIC / "vendor" / name).read_bytes()).hexdigest()
        assert digest in readme, f"{name} changed without updating vendor/README.md"


def test_the_page_is_served_at_the_root():
    page = make_client([]).get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert Path(STATIC / "app.js").exists() and Path(STATIC / "app.css").exists()


# -- implementation-audit findings ------------------------------------------------------


def test_a_decision_racing_approval_required_is_not_overwritten(monkeypatch):
    """Audit #1: decide() the instant approval.required is emitted. While the
    resumed turn is still working, its status must read "running" — not be
    overwritten back to "awaiting_approval" by the pause that preceded it."""
    from pmagent.web import sessions

    gate = threading.Event()

    def parked(messages):
        return [text("Posted.")] if gate.wait(timeout=10) else [text("timeout")]

    client = make_client([COMMENT, parked])
    conv = client.post("/api/v1/conversations").json()
    session = client.app.state.store.get(conv["id"])
    original = sessions.Turn.emit

    def emit_then_decide(self, kind, data, status=None):
        original(self, kind, data, status)
        if kind == "approval.required":
            session.decide(data["approval_id"], "approve")      # as fast as a client can be

    monkeypatch.setattr(sessions.Turn, "emit", emit_then_decide)
    try:
        turn = client.post(f"/api/v1/conversations/{conv['id']}/turns", json={"message": "go"}).json()
        deadline = time.time() + 5
        while FakeJira.comments == [] and time.time() < deadline:   # the approved write ran...
            time.sleep(0.01)
        assert client.get(turn["url"]).json()["status"] == "running"  # ...and the turn says so
    finally:
        gate.set()
    events(client, turn["events_url"])
    assert client.get(turn["url"]).json()["status"] == "completed"


def test_a_crash_while_finishing_fails_the_turn_instead_of_leaving_it_running(monkeypatch):
    """Audit #2: an exception in _finish (here: the scope-warning renderer) must not
    kill the worker and leave the turn 'running' forever."""
    from pmagent.web import sessions

    def boom(messages, calls):
        raise ValueError("renderer bug")

    monkeypatch.setattr(sessions, "unchecked_scope_warning", boom)
    client = make_client([COMMENT, [text("next")]])
    conv, turn = start(client)
    evts = events(client, turn.json()["events_url"])
    assert evts[-1]["event"] == "turn.failed"
    assert "renderer bug" in evts[-1]["data"]["problem"]["detail"]
    session = client.app.state.store.get(conv["id"])
    assert not session.assistant.pending and session.status == "idle"
    assert client.post(f"/api/v1/conversations/{conv['id']}/turns", json={"message": "again"}).status_code == 202


def test_the_middlewares_own_refusals_carry_security_headers():
    """Audit #6."""
    client = make_client([])
    refused = client.get("/api/v1/health", headers={"Host": "evil.example"})
    assert refused.status_code == 400
    assert "script-src 'self'" in refused.headers["content-security-policy"]
    assert refused.headers["x-content-type-options"] == "nosniff"
