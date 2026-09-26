"""
Conversations, turns and approvals for the web API — the web twin of `main.py`.

`main.py` drives one `PMAssistant` from a terminal loop. This module drives one
`PMAssistant` per conversation from HTTP requests:

    POST /turns       → ConversationSession.start_turn()  → worker thread: assistant.send()
    POST /decision    → ConversationSession.decide()      → worker thread: assistant.resume()
    GET  /events      → Turn.wait_for_events()             ← every step the TraceRecorder emits

Rules carried over from the CLI, on purpose:

* A failed turn calls `assistant.discard_pending()` (as `main.handle_turn` does),
  so a crash never leaves the conversation wedged on an approval nobody can answer.
* Approval text is exactly the CLI's: `describe_write(call)` and
  `unchecked_scope_warning(assistant.messages, calls)`.

Differences, on purpose: a new message while an approval is waiting is refused
(409) instead of silently abandoning it — an API should not throw away a pending
decision as a side effect.

Concurrency: Strands' `agent()` is synchronous, so each turn runs on its own
worker thread. A per-conversation lock guards state; one turn at a time per
conversation; separate conversations run in parallel. Everything is in memory —
a restart forgets all conversations, exactly like the CLI.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime

from strands.types.exceptions import ContextWindowOverflowException

from pmagent.assistant import PMAssistant, TurnResult
from pmagent.cli.approval import describe_write, unchecked_scope_warning
from pmagent.messages import is_tool_result, text_of
from pmagent.web.problems import ProblemError, problem_body
from pmagent.web.trace import TraceRecorder

RESTING = frozenset({"awaiting_approval", "completed", "failed"})


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(9)}"


@dataclass
class Approval:
    id: str
    turn_id: str
    calls: list[dict]
    descriptions: list[str]
    warning: str | None
    created_at: str = field(default_factory=now)
    status: str = "pending"          # pending | approved | rejected | abandoned
    decided_at: str | None = None


class Turn:
    """One user message and everything the agent did about it: an append-only event log."""

    def __init__(self, conversation_id: str, message: str) -> None:
        self.id = new_id("turn")
        self.conversation_id = conversation_id
        self.message = message
        self.created_at = now()
        self.completed_at: str | None = None
        self.status = "running"
        self.route = ""
        self.route_reason = ""
        self.reply = ""
        self.error: dict | None = None
        self.usage: dict = {}
        self.approval_ids: list[str] = []
        self._events: list[dict] = []
        self._cond = threading.Condition()

    # The log. Ids are 1, 2, 3 ... per turn — what SSE's Last-Event-ID refers to.
    def emit(self, kind: str, data: dict, status: str | None = None) -> None:
        """Append an event — and, optionally, change status in the same critical
        section, so no reader ever sees one without the other."""
        with self._cond:
            event = {"id": len(self._events) + 1, "event": kind,
                     "data": {"ts": now(), "turn_id": self.id, **data}}
            self._events.append(event)
            if status is not None:
                self._set_status_locked(status)
            self._cond.notify_all()

    def set_status(self, status: str) -> None:
        with self._cond:
            self._set_status_locked(status)
            self._cond.notify_all()

    def _set_status_locked(self, status: str) -> None:
        self.status = status
        if status in ("completed", "failed"):
            self.completed_at = now()

    def events(self, after: int = 0) -> list[dict]:
        with self._cond:
            return list(self._events[after:])

    @property
    def event_count(self) -> int:
        with self._cond:
            return len(self._events)

    def wait_for_events(self, after: int, timeout: float) -> tuple[list[dict], bool]:
        """Block until there are events after `after`, the turn rests, or timeout.

        Returns `(new events, resting)`. `resting` is read under the same lock as
        the events, so a caller that sees `resting=True` has every event.
        Runs on a worker thread (see app.py) — never on the event loop.
        """
        with self._cond:
            self._cond.wait_for(
                lambda: len(self._events) > after or self.status in RESTING, timeout=timeout
            )
            return list(self._events[after:]), self.status in RESTING


class ConversationSession:
    def __init__(self, assistant_factory) -> None:
        self.id = new_id("conv")
        self.created_at = now()
        self.recorder = TraceRecorder()
        self.assistant: PMAssistant = assistant_factory(self.recorder)
        self.turns: dict[str, Turn] = {}
        self.approvals: dict[str, Approval] = {}
        # key -> (body hash, turn id). Check-and-create runs under one lock, so a
        # concurrent duplicate simply waits and then receives the stored turn.
        self._idempotency: dict[str, tuple[str, str]] = {}
        self._lock = threading.RLock()
        self._running: Turn | None = None
        self._current: Turn | None = None

    # -- read side ------------------------------------------------------------

    @property
    def status(self) -> str:
        with self._lock:
            if self._running is not None:
                return "running"
            return "awaiting_approval" if self.pending_approval else "idle"

    @property
    def pending_approval(self) -> Approval | None:
        with self._lock:
            return next((a for a in self.approvals.values() if a.status == "pending"), None)

    def transcript(self) -> list[dict]:
        """What the human typed and what the agent said — the chat, without tool plumbing."""
        items = []
        for message in self.assistant.messages:
            if is_tool_result(message):
                continue
            text = text_of(message)
            if text:
                items.append({"role": message["role"], "text": text})
        return items

    # -- write side -----------------------------------------------------------

    def start_turn(self, message: str, idempotency_key: str | None = None) -> tuple[Turn, bool]:
        """Start a turn. Returns (turn, created). A retry with the same key returns the original."""
        body_hash = hashlib.sha256(
            json.dumps({"message": message}, sort_keys=True).encode()
        ).hexdigest()
        with self._lock:
            if idempotency_key is not None and idempotency_key in self._idempotency:
                stored_hash, turn_id = self._idempotency[idempotency_key]
                if stored_hash != body_hash:
                    raise ProblemError("idempotency-key-reuse")
                return self.turns[turn_id], False
            if self._running is not None:
                raise ProblemError("turn-in-progress",
                                   f"Turn {self._running.id} is still running.",
                                   turn=self._running.id)
            pending = self.pending_approval
            if pending is not None:
                raise ProblemError("approval-pending",
                                   "Decide the pending approval before sending a new message.",
                                   approval=pending.id)
            turn = Turn(self.id, message)
            self.turns[turn.id] = turn
            self._running = self._current = turn
            self.recorder.emit = turn.emit
            # Recorded only once the turn exists (the 202 is certain from here).
            if idempotency_key is not None:
                self._idempotency[idempotency_key] = (body_hash, turn.id)

        turn.emit("turn.started", {"message": message})
        self._spawn(turn, lambda: self.assistant.send(message))
        return turn, True

    def decide(self, approval_id: str, decision: str) -> Turn:
        """Record a decision and resume the paused turn. Same decision twice is a no-op."""
        with self._lock:
            approval = self.approvals.get(approval_id)
            if approval is None:
                raise ProblemError("not-found", f"No approval {approval_id} in this conversation.")
            wanted = "approved" if decision == "approve" else "rejected"
            if approval.status == wanted:
                return self.turns[approval.turn_id]          # idempotent repeat
            if approval.status != "pending":
                raise ProblemError("approval-not-pending",
                                   f"Approval {approval_id} is {approval.status}.",
                                   approval_status=approval.status)
            approval.status, approval.decided_at = wanted, now()
            turn = self.turns[approval.turn_id]
            self._running = turn
            turn.set_status("running")
        turn.emit("approval.decided", {"approval_id": approval.id, "decision": wanted,
                                       "calls": [c["name"] for c in approval.calls]})
        self._spawn(turn, lambda: self.assistant.resume(decision == "approve"))
        return turn

    def discard(self) -> None:
        """Abandon any pending approval (DELETE conversation)."""
        with self._lock:
            if self._running is not None:
                raise ProblemError("turn-in-progress", "Wait for the running turn to rest.")
            self._abandon_pending()
            self.assistant.discard_pending()

    # -- worker ---------------------------------------------------------------

    def _spawn(self, turn: Turn, work) -> None:
        threading.Thread(target=self._run, args=(turn, work), daemon=True,
                         name=f"turn-{turn.id}").start()

    def _run(self, turn: Turn, work) -> None:
        try:
            result: TurnResult = work()
        except ContextWindowOverflowException:
            self._fail(turn, "context-full")
        except Exception as exc:  # noqa: BLE001 — reported as a problem, like the CLI's error line
            self._fail(turn, "turn-failed", f"{type(exc).__name__}: {exc}")
        else:
            try:
                self._finish(turn, result)
            except Exception as exc:  # noqa: BLE001 — a turn must never be left "running"
                self._fail(turn, "turn-failed", f"while finishing the turn: {type(exc).__name__}: {exc}")

    def _finish(self, turn: Turn, result: TurnResult) -> None:
        turn.route, turn.route_reason, turn.usage = result.route, result.route_reason, result.usage
        turn.emit("usage", dict(result.usage))
        if result.pending:
            calls = result.pending
            approval = Approval(
                id=new_id("apr"),
                turn_id=turn.id,
                calls=calls,
                descriptions=[describe_write(call) for call in calls],
                warning=unchecked_scope_warning(self.assistant.messages, calls),
            )
            # Record, announce and set the status as one step under the session
            # lock: a decide() that sees the approval must also see the turn at
            # rest, or its "running" could be overwritten here afterwards.
            with self._lock:
                self.approvals[approval.id] = approval
                turn.approval_ids.append(approval.id)
                self._running = None
                turn.emit("approval.required", {
                    "approval_id": approval.id, "calls": calls,
                    "descriptions": approval.descriptions, "warning": approval.warning,
                }, status="awaiting_approval")
            return
        with self._lock:
            self._running = None
        turn.reply = result.reply or self._last_assistant_text()
        turn.emit("turn.completed", {"reply": turn.reply, "reply_is_markdown_document": bool(result.reply),
                                     "usage": dict(result.usage)}, status="completed")

    def _fail(self, turn: Turn, slug: str, detail: str = "") -> None:
        # Same cleanup as main.handle_turn: an approval left hanging would wedge
        # this conversation. The agent's own state is cleared, and our record says so.
        with self._lock:
            self._running = None
            self._abandon_pending()
            try:
                self.assistant.discard_pending()
            except Exception:  # noqa: BLE001 — cleanup must not mask the real error
                pass
        turn.error = problem_body(slug, detail, f"/api/v1/conversations/{self.id}/turns/{turn.id}")
        turn.emit("turn.failed", {"problem": turn.error}, status="failed")

    def _abandon_pending(self) -> None:
        for approval in self.approvals.values():
            if approval.status == "pending":
                approval.status, approval.decided_at = "abandoned", now()
                self.turns[approval.turn_id].emit("approval.decided", {
                    "approval_id": approval.id, "decision": "abandoned",
                    "calls": [c["name"] for c in approval.calls],
                })

    def _last_assistant_text(self) -> str:
        messages = self.assistant.messages
        if messages and messages[-1].get("role") == "assistant":
            return text_of(messages[-1])
        return ""


class SessionStore:
    """All conversations, in memory."""

    def __init__(self, assistant_factory) -> None:
        self._factory = assistant_factory
        self._sessions: dict[str, ConversationSession] = {}
        self._lock = threading.Lock()

    def create(self) -> ConversationSession:
        session = ConversationSession(self._factory)
        with self._lock:
            self._sessions[session.id] = session
        return session

    def get(self, conversation_id: str) -> ConversationSession:
        with self._lock:
            session = self._sessions.get(conversation_id)
        if session is None:
            raise ProblemError("not-found", f"No conversation {conversation_id}.")
        return session

    def delete(self, conversation_id: str) -> None:
        session = self.get(conversation_id)
        session.discard()
        with self._lock:
            self._sessions.pop(conversation_id, None)
