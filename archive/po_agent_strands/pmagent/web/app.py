"""
The HTTP API and the web page — FastAPI over the same `PMAssistant` the CLI uses.

    uv run web.py        → http://127.0.0.1:8000

Resources (all under the versioned prefix /api/v1):

    conversations ─┬─ turns ── events (SSE: the thinking chain)
                   └─ approvals ── decision

API BEST PRACTICE applied here (lesson 10 walks through each):

* nouns in paths, a version prefix, and the right status codes — 201 + Location
  on create, 202 + Location for work that continues in the background, 204 on
  delete, 409 for state conflicts, 422 for invalid input;
* one error format, RFC 9457 problem+json (`problems.py`);
* typed schemas → an accurate OpenAPI document (`schemas.py`);
* `Idempotency-Key` on the one non-idempotent POST that costs money;
* long-running work as a resource you can poll *or* stream;
* security for a local tool: loopback-only Host allowlist (DNS rebinding), an
  Origin check on state-changing requests (CSRF), optional bearer token, and a
  strict Content-Security-Policy for the page, which renders model output.
"""

from __future__ import annotations

import hmac
import json
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from sse_starlette import EventSourceResponse, ServerSentEvent

from pmagent import env
from pmagent.web import problems
from pmagent.web.problems import ProblemError, problem_response
from pmagent.web.schemas import (
    ApprovalOut,
    ConversationOut,
    DecisionCreate,
    Health,
    Meta,
    Problem,
    TurnCreate,
    TurnDetail,
    TurnList,
    TurnOut,
)
from pmagent.web.sessions import Approval, ConversationSession, SessionStore, Turn

VERSION = "1.0.0"
API = "/api/v1"
STATIC = Path(__file__).parent / "static"

# SSE waits run on worker threads; a dedicated limiter keeps idle browser tabs
# from starving the thread pool that ordinary (sync) requests use.
SSE_LIMITER = anyio.CapacityLimiter(16)
SSE_WAIT_SECONDS = 15.0

STRICT_CSP = (
    "default-src 'self'; img-src 'self' data:; connect-src 'self'; script-src 'self'; "
    "style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
# FastAPI's Swagger UI loads from a CDN with an inline bootstrap script. Only
# /api/docs gets this relaxed policy; it renders no model output.
DOCS_CSP = (
    "default-src 'self'; img-src 'self' data: https://fastapi.tiangolo.com; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' https://cdn.jsdelivr.net; frame-ancestors 'none'"
)

# Error responses, documented per route — each route lists only what it can return.
# (Every route can also return 400 bad-host, 403 forbidden-origin on writes, and
# 401 when PMAGENT_API_TOKEN is set; those come from middleware and auth.)
NOT_FOUND = {404: {"model": Problem, "description": "Not found (problem+json)"}}
CONFLICT = {409: {"model": Problem, "description": "State conflict (problem+json)"}}
INVALID = {422: {"model": Problem, "description": "Invalid request (problem+json)"}}


# ---------------------------------------------------------------------------
# Security middleware (plain ASGI so it never buffers the SSE stream)
# ---------------------------------------------------------------------------


class SecurityMiddleware:
    """Host allowlist, Origin check, and security headers on every response."""

    SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

    def __init__(self, app, allowed_hosts: set[str]) -> None:
        self.app = app
        self.allowed_hosts = allowed_hosts

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        host_header = headers.get("host", "")
        hostname = host_header.rsplit(":", 1)[0] if not host_header.startswith("[") else host_header

        csp = DOCS_CSP if scope["path"].startswith("/api/docs") else STRICT_CSP

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                extra = [
                    (b"content-security-policy", csp.encode()),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                ]
                message["headers"] = list(message.get("headers", [])) + extra
            await send(message)

        # DNS rebinding: a hostile page that re-resolves its name to 127.0.0.1
        # still sends *its* name as Host. Only loopback names are answered.
        if hostname not in self.allowed_hosts:
            return await problem_response("bad-host", f"Host {host_header!r} is not allowed.")(
                scope, receive, send_with_headers)

        # CSRF: browsers send Origin on state-changing requests. It must be us.
        origin = headers.get("origin")
        if scope["method"] not in self.SAFE_METHODS and origin is not None:
            own = f"{scope.get('scheme', 'http')}://{host_header}"
            if origin != own:
                return await problem_response("forbidden-origin", f"Origin {origin!r} is not {own!r}.")(
                    scope, receive, send_with_headers)

        await self.app(scope, receive, send_with_headers)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def default_assistant_factory(app: FastAPI):
    """Build each conversation's PMAssistant exactly like the CLI does, plus the recorder."""
    from pmagent import llm
    from pmagent.assistant import PMAssistant

    def factory(recorder):
        # One model object per conversation for Anthropic (its client is bound
        # to an event loop); OpenAI builds a client per request, so share it.
        model = llm.build_model() if env.LLM_PROVIDER == "anthropic" else llm.get_model()
        return PMAssistant(model=model, mcp=app.state.mcp, **recorder.assistant_kwargs())

    return factory


def create_app(
    assistant_factory=None,
    *,
    api_token: str | None = None,
    extra_hosts: tuple[str, ...] = (),
    enable_mcp: bool = False,
) -> FastAPI:
    """Build the app. Tests pass a factory that uses a ScriptedModel."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # MCP lives as long as the server — never per conversation — so deleting
        # one conversation cannot cut Lucid off for the others.
        app.state.mcp = None
        if enable_mcp and env.LUCID_MCP_ENABLED:
            from pmagent.tools.mcp_tools import start_lucid

            app.state.mcp = await anyio.to_thread.run_sync(start_lucid)
        yield
        if app.state.mcp is not None:
            app.state.mcp.close()

    app = FastAPI(
        title="PM Agent API",
        version=VERSION,
        description="Chat with the PM Agent's lanes, watch its thinking chain over SSE, "
                    "and approve or reject every write. Errors are RFC 9457 problem+json.",
        openapi_url=f"{API}/openapi.json",
        docs_url="/api/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.mcp = None
    app.state.store = SessionStore(assistant_factory or default_assistant_factory(app))
    app.state.api_token = api_token
    problems.install(app)
    app.add_middleware(SecurityMiddleware, allowed_hosts={"127.0.0.1", "localhost", *extra_hosts})

    bearer = HTTPBearer(auto_error=False, description="Only when PMAGENT_API_TOKEN is set.")

    def require_token(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:
        token = app.state.api_token
        if not token:
            return
        supplied = credentials.credentials if credentials else ""
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            raise ProblemError("unauthorized", headers={"WWW-Authenticate": "Bearer"})

    def store() -> SessionStore:
        return app.state.store

    # -- helpers -------------------------------------------------------------

    def conv_url(session: ConversationSession) -> str:
        return f"{API}/conversations/{session.id}"

    def turn_url(session, turn: Turn) -> str:
        return f"{conv_url(session)}/turns/{turn.id}"

    def turn_out(session, turn: Turn, detail: bool = False):
        body = dict(
            id=turn.id, conversation_id=session.id, message=turn.message, status=turn.status,
            created_at=turn.created_at, completed_at=turn.completed_at, route=turn.route,
            route_reason=turn.route_reason, reply=turn.reply, error=turn.error,
            usage=turn.usage or {}, approval_ids=list(turn.approval_ids),
            event_count=turn.event_count, url=turn_url(session, turn),
            events_url=f"{turn_url(session, turn)}/events",
        )
        return TurnDetail(**body, events=turn.events()) if detail else TurnOut(**body)

    def approval_out(session, approval: Approval) -> ApprovalOut:
        return ApprovalOut(
            id=approval.id, turn_id=approval.turn_id, status=approval.status,
            created_at=approval.created_at, decided_at=approval.decided_at,
            calls=approval.calls, descriptions=approval.descriptions, warning=approval.warning,
            decision_url=f"{conv_url(session)}/approvals/{approval.id}/decision",
        )

    def get_turn(session: ConversationSession, turn_id: str) -> Turn:
        turn = session.turns.get(turn_id)
        if turn is None:
            raise ProblemError("not-found", f"No turn {turn_id} in conversation {session.id}.")
        return turn

    # -- routes ----------------------------------------------------------------

    @app.get(f"{API}/health", response_model=Health, tags=["meta"], summary="Liveness check")
    def health() -> Health:
        """Liveness. No authentication."""
        return Health()

    @app.get(f"{API}/meta", response_model=Meta, tags=["meta"], dependencies=[Depends(require_token)],
             summary="Model, Jira project, lanes and reasoning availability")
    def meta() -> Meta:
        """What the header of the UI shows: model, Jira project, lanes, reasoning."""
        from pmagent import llm
        from pmagent.agents.router import ROUTE_TO_LANE

        available = llm.reasoning_available()
        note = ("Reasoning summaries stream into the thinking chain when the model writes one — "
                "it decides per step, and often skips them for simple steps." if available else
                "This model returns no reasoning with the current settings; set "
                "LLM_REASONING_EFFORT and LLM_REASONING_SUMMARY (OpenAI) to stream a summary.")
        return Meta(
            app="PM Agent", version=VERSION, provider=env.LLM_PROVIDER, model=env.LLM_MODEL,
            jira_base_url=env.JIRA_BASE_URL, jira_project=env.JIRA_PROJECT_KEY,
            lanes=sorted(set(ROUTE_TO_LANE.values())), reasoning_available=available,
            reasoning_note=note, lucid_connected=app.state.mcp is not None,
            auth_required=bool(app.state.api_token),
        )

    @app.post(f"{API}/conversations", status_code=201, response_model=ConversationOut,
              tags=["conversations"], dependencies=[Depends(require_token)],
              summary="Start a conversation")
    def create_conversation(response: Response, sessions: SessionStore = Depends(store)):
        """Start a conversation (the CLI's `/new`). Returns 201 and its Location."""
        session = sessions.create()
        response.headers["Location"] = conv_url(session)
        return conversation_body(session)

    def conversation_body(session: ConversationSession) -> ConversationOut:
        pending = session.pending_approval
        return ConversationOut(
            id=session.id, created_at=session.created_at, status=session.status,
            route=session.assistant.route, messages=session.transcript(),
            pending_approval=approval_out(session, pending) if pending else None,
            turns_url=f"{conv_url(session)}/turns",
        )

    @app.get(f"{API}/conversations/{{conversation_id}}", response_model=ConversationOut,
             responses=NOT_FOUND, tags=["conversations"], dependencies=[Depends(require_token)],
             summary="The chat so far and any pending approval")
    def get_conversation(conversation_id: str, sessions: SessionStore = Depends(store)):
        """The chat so far, and the approval waiting for a decision, if any."""
        return conversation_body(sessions.get(conversation_id))

    @app.delete(f"{API}/conversations/{{conversation_id}}", status_code=204,
                responses={**NOT_FOUND, **CONFLICT}, tags=["conversations"],
                dependencies=[Depends(require_token)], summary="Forget a conversation")
    def delete_conversation(conversation_id: str, sessions: SessionStore = Depends(store)):
        """Forget a conversation. A pending approval is abandoned — nothing is written."""
        sessions.delete(conversation_id)
        return Response(status_code=204)

    @app.post(f"{API}/conversations/{{conversation_id}}/turns", status_code=202,
              response_model=TurnOut, responses={**NOT_FOUND, **CONFLICT, **INVALID}, tags=["turns"],
              dependencies=[Depends(require_token)], summary="Send a message (runs in the background)")
    def create_turn(
        conversation_id: str,
        body: TurnCreate,
        response: Response,
        idempotency_key: str | None = Header(default=None, max_length=200,
                                             description="Retry-safe key: the same key and body "
                                                         "return the same turn."),
        sessions: SessionStore = Depends(store),
    ):
        """Send a message. The agent works in the background: follow `events_url`
        (SSE) for the thinking chain, or poll the turn."""
        session = sessions.get(conversation_id)
        turn, _created = session.start_turn(body.message, idempotency_key)
        response.headers["Location"] = turn_url(session, turn)
        return turn_out(session, turn)

    @app.get(f"{API}/conversations/{{conversation_id}}/turns", response_model=TurnList,
             responses=NOT_FOUND, tags=["turns"], dependencies=[Depends(require_token)],
             summary="Every turn with its events")
    def list_turns(conversation_id: str, sessions: SessionStore = Depends(store)):
        """Every turn with its events — how the page rebuilds itself after a reload."""
        session = sessions.get(conversation_id)
        return TurnList(items=[turn_out(session, t, detail=True) for t in list(session.turns.values())])

    @app.get(f"{API}/conversations/{{conversation_id}}/turns/{{turn_id}}", response_model=TurnDetail,
             responses=NOT_FOUND, tags=["turns"], dependencies=[Depends(require_token)],
             summary="One turn: status, reply and events so far")
    def get_turn_route(conversation_id: str, turn_id: str, sessions: SessionStore = Depends(store)):
        """A turn's status, reply and every event so far (the polling alternative to SSE)."""
        session = sessions.get(conversation_id)
        return turn_out(session, get_turn(session, turn_id), detail=True)

    @app.get(
        f"{API}/conversations/{{conversation_id}}/turns/{{turn_id}}/events",
        response_class=EventSourceResponse, summary="The thinking chain, streamed (SSE)",
        responses={**NOT_FOUND, 200: {
            "content": {"text/event-stream": {}},
            "description": "Server-Sent Events. Each event: `id`, `event` (type), `data` (JSON). "
                           "The stream closes when the turn rests (awaiting_approval, completed, "
                           "failed); reconnect with Last-Event-ID to continue.",
        }},
        tags=["turns"], dependencies=[Depends(require_token)],
    )
    async def turn_events(
        conversation_id: str,
        turn_id: str,
        request: Request,
        after: int = 0,
        last_event_id: str | None = Header(default=None),
        sessions: SessionStore = Depends(store),
    ):
        """The thinking chain, live: replays from `Last-Event-ID` (or `?after=`), then streams."""
        session = sessions.get(conversation_id)
        turn = get_turn(session, turn_id)
        cursor = int(last_event_id) if last_event_id and last_event_id.isdigit() else after

        async def stream():
            nonlocal cursor
            yield ServerSentEvent(retry=3000, comment="thinking chain")
            while True:
                events, resting = await anyio.to_thread.run_sync(
                    turn.wait_for_events, cursor, SSE_WAIT_SECONDS,
                    abandon_on_cancel=True, limiter=SSE_LIMITER,
                )
                for event in events:
                    cursor = event["id"]
                    yield ServerSentEvent(id=str(event["id"]), event=event["event"],
                                          data=json.dumps(event["data"]))
                if resting and not turn.events(cursor):
                    return
                if await request.is_disconnected():
                    return

        return EventSourceResponse(
            stream(), ping=15,
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get(f"{API}/conversations/{{conversation_id}}/approvals/{{approval_id}}",
             response_model=ApprovalOut, responses=NOT_FOUND, tags=["approvals"],
             dependencies=[Depends(require_token)], summary="What the agent wants to write")
    def get_approval(conversation_id: str, approval_id: str, sessions: SessionStore = Depends(store)):
        """The writes the agent wants to make, described exactly as the CLI shows them."""
        session = sessions.get(conversation_id)
        approval = session.approvals.get(approval_id)
        if approval is None:
            raise ProblemError("not-found", f"No approval {approval_id}.")
        return approval_out(session, approval)

    @app.post(f"{API}/conversations/{{conversation_id}}/approvals/{{approval_id}}/decision",
              status_code=202, response_model=TurnOut, responses={**NOT_FOUND, **CONFLICT, **INVALID},
              tags=["approvals"], dependencies=[Depends(require_token)],
              summary="Approve or reject; the paused turn resumes")
    def decide(conversation_id: str, approval_id: str, body: DecisionCreate, response: Response,
               sessions: SessionStore = Depends(store)):
        """Approve or reject. The paused turn resumes in the background; its events
        continue on the same stream. Repeating the same decision is harmless."""
        session = sessions.get(conversation_id)
        turn = session.decide(approval_id, body.decision)
        response.headers["Location"] = turn_url(session, turn)
        return turn_out(session, turn)

    # -- the page ---------------------------------------------------------------

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    return app
