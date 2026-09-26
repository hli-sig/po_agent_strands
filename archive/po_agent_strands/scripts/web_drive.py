"""
Drive the running web API like the page does, and record the thinking chain.

    uv run scripts/smoke.py --web --port 8765 &          # write-blocked server
    uv run scripts/web_drive.py http://127.0.0.1:8765 out.jsonl \
        "who owns CSCI-1934?" "comment 'hi' on CSCI-1934::reject"

Each argument is one message. A `::approve` / `::reject` suffix answers any
approval that message triggers (default: reject). Every SSE event is written to
the JSONL file, one per line, with the turn's message — evidence of exactly what
the agent did. Uses only the public HTTP API.
"""

from __future__ import annotations

import json
import sys

import httpx


def read_stream(client: httpx.Client, url: str, last_id: int) -> tuple[list[dict], int]:
    headers = {"Last-Event-ID": str(last_id)} if last_id else {}
    out, current = [], {}
    with client.stream("GET", url, headers=headers, timeout=600) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if line.startswith("id:"):
                current["id"] = int(line[3:].strip())
            elif line.startswith("event:"):
                current["event"] = line[6:].strip()
            elif line.startswith("data:"):
                current["data"] = json.loads(line[5:].strip())
            elif line == "" and "event" in current:
                out.append(current)
                last_id = current["id"]
                current = {}
    return out, last_id


def drive(base: str, out_path: str, messages: list[str]) -> int:
    with httpx.Client(base_url=base, timeout=30) as client, open(out_path, "w") as out:
        conv = client.post("/api/v1/conversations").json()
        print(f"conversation {conv['id']}")
        for raw in messages:
            message, _, answer = raw.partition("::")
            answer = answer or "reject"
            turn = client.post(f"/api/v1/conversations/{conv['id']}/turns",
                               json={"message": message},
                               headers={"Idempotency-Key": f"drive-{abs(hash(raw))}"})
            turn.raise_for_status()
            body, last = turn.json(), 0
            print(f"\n>>> {message}   [{turn.status_code} {turn.headers['location']}]")
            while True:
                events, last = read_stream(client, body["events_url"], last)
                for event in events:
                    out.write(json.dumps({"message": message, **event}) + "\n")
                    print(f"  {event['id']:>3} {event['event']:<18} {summarise(event)}")
                final = events[-1]["event"] if events else ""
                if final != "approval.required":
                    break
                approval_id = events[-1]["data"]["approval_id"]
                decision = client.post(
                    f"/api/v1/conversations/{conv['id']}/approvals/{approval_id}/decision",
                    json={"decision": answer})
                print(f"  --> decision {answer}: {decision.status_code}")
        return 0


def summarise(event: dict) -> str:
    d = event["data"]
    kind = event["event"]
    if kind == "route.decided":
        return f"{d['lane']} ({d['reason']})"
    if kind in ("text.delta", "reasoning.delta"):
        return repr(d["text"][:60])
    if kind == "tool.started":
        return f"{d['name']} {json.dumps(d['input'])[:80]}"
    if kind == "tool.finished":
        return f"{d['name']} {d['status']} {d['duration_ms']}ms images={d['images']} {d['output'][:60]!r}"
    if kind == "approval.required":
        return " | ".join(d["descriptions"])[:120]
    if kind == "approval.decided":
        return d["decision"]
    if kind == "prd.step":
        return f"{d['step']} {json.dumps({k: v for k, v in d.items() if k not in ('ts', 'turn_id', 'step')})[:90]}"
    if kind in ("turn.completed",):
        return repr(d["reply"][:80])
    if kind == "turn.failed":
        return d["problem"]["type"]
    if kind == "usage":
        return f"{d['total_tokens']} tokens, {d['model_calls']} calls"
    if kind == "message.completed":
        return f"text={len(d['text'])} tools={d['tool_calls']}"
    return ""


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(2)
    sys.exit(drive(sys.argv[1], sys.argv[2], sys.argv[3:]))
