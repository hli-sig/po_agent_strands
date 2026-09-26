"""An open thinking-chain stream must not stall the server (review finding R1).

TestClient handles one request at a time, which would hide a stream that blocks
the event loop. This test runs a real uvicorn server on an ephemeral port, holds
one conversation's SSE stream open while its turn is parked mid-run, and checks
that meanwhile /health answers and a *second* conversation completes a turn.
"""

from __future__ import annotations

import json
import threading
import time

import httpx
import pytest
import uvicorn

from pmagent.assistant import PMAssistant
from pmagent.web.app import create_app
from tests.fakes import ScriptedModel, text


@pytest.fixture
def server():
    gate = threading.Event()

    def parked(messages):
        # Runs in Strands' own worker thread, not the server's event loop.
        return [text("slow done")] if gate.wait(timeout=10) else [text("TIMED OUT")]

    scripts = [[parked], [[text("fast done")]]]   # one script per conversation, in order

    def factory(recorder):
        model = ScriptedModel(scripts.pop(0))
        return PMAssistant(model=model, classify=lambda m, p: "query", **recorder.assistant_kwargs())

    config = uvicorn.Config(create_app(factory), host="127.0.0.1", port=0, log_level="warning",
                            lifespan="on")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not srv.started:
        assert time.time() < deadline, "server did not start"
        time.sleep(0.02)
    port = srv.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", gate
    finally:
        gate.set()
        srv.should_exit = True
        thread.join(timeout=10)


def test_an_open_stream_does_not_block_other_requests(server):
    base, gate = server
    with httpx.Client(base_url=base, timeout=10) as client:
        slow = client.post("/api/v1/conversations").json()
        turn = client.post(f"/api/v1/conversations/{slow['id']}/turns", json={"message": "slow"}).json()

        with client.stream("GET", turn["events_url"]) as stream:
            lines = stream.iter_lines()
            # Wait until the stream has really started delivering (turn.started),
            # so the server-side wait for the parked model is in progress.
            for line in lines:
                if line.startswith("event:") and "turn.started" in line:
                    break

            started = time.monotonic()
            with httpx.Client(base_url=base, timeout=5) as other:
                assert other.get("/api/v1/health").status_code == 200
                fast = other.post("/api/v1/conversations").json()
                fast_turn = other.post(f"/api/v1/conversations/{fast['id']}/turns",
                                       json={"message": "fast"}).json()
                with other.stream("GET", fast_turn["events_url"]) as fast_stream:
                    fast_events = [l for l in fast_stream.iter_lines() if l.startswith("event:")]
            elapsed = time.monotonic() - started

            assert fast_events[-1] == "event: turn.completed"
            assert elapsed < 5, f"other requests took {elapsed:.1f}s while a stream was open"

            gate.set()   # release the slow conversation; its stream then finishes
            rest = [l for l in lines if l.startswith(("event:", "data:"))]
        assert "event: turn.completed" in rest
        done = json.loads([l for l in rest if l.startswith("data:")][-1][5:])
        assert done["reply"] == "slow done"
