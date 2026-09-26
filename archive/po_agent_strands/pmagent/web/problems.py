"""
One error format for the whole API: RFC 9457 "Problem Details for HTTP APIs".

Every error response is `application/problem+json`:

    {"type": "/problems/turn-in-progress", "title": "A turn is already running",
     "status": 409, "detail": "...", "instance": "/api/v1/conversations/conv_x/turns"}

API BEST PRACTICE — why this matters: a client (the page, a script, another
service) handles errors by switching on one stable field (`type`), not by parsing
prose. `type` is a URI; each one is served as a small HTML page at
`/problems/<slug>`, so a developer who meets an error can open it and read what
it means and how to fix it.
"""

from __future__ import annotations

from html import escape

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_JSON = "application/problem+json"

# slug -> (status, title, what it means / how to fix it)
CATALOG: dict[str, tuple[int, str, str]] = {
    "not-found": (404, "Not found",
                  "The conversation, turn or approval in the URL does not exist — it may have "
                  "been deleted, or the server restarted (conversations live in memory)."),
    "validation": (422, "Request is not valid",
                   "The request body or parameters failed validation. `errors` lists each "
                   "problem. JSON bodies need `Content-Type: application/json`."),
    "turn-in-progress": (409, "A turn is already running",
                         "This conversation is still working on a message. Wait for its "
                         "events stream to reach a resting state, then send the next one."),
    "approval-pending": (409, "An approval is waiting",
                         "The agent paused before a write and is waiting for your decision. "
                         "Approve or reject it (see `approval`), or delete the conversation."),
    "approval-not-pending": (409, "The approval is no longer pending",
                             "This approval was already decided differently, or was abandoned "
                             "(the turn failed or the conversation was reset). Nothing more "
                             "can be done with it."),
    "idempotency-key-reuse": (422, "Idempotency-Key reused with a different request",
                              "An Idempotency-Key identifies one request. Reusing it with a "
                              "different body is refused — use a new key for a new message."),
    "context-full": (422, "The conversation is too long",
                     "The conversation no longer fits in the model's context window. Start a "
                     "new conversation."),
    "turn-failed": (500, "The turn failed",
                    "The agent raised an error while handling the message. `detail` has the "
                    "cause. Any pending approval was abandoned; nothing was written by it."),
    "unauthorized": (401, "Authentication required",
                     "This server requires `Authorization: Bearer <token>` (PMAGENT_API_TOKEN)."),
    "bad-host": (400, "Host not allowed",
                 "The Host header is not one this server answers to. It listens on loopback "
                 "only (127.0.0.1 / localhost) — this protects against DNS-rebinding attacks."),
    "forbidden-origin": (403, "Cross-origin request refused",
                         "A state-changing request came from another origin. Only the page "
                         "served by this server may send it."),
    "method-not-allowed": (405, "Method not allowed", "This URL does not support that method."),
}


class ProblemError(Exception):
    """Raise anywhere in a route to return a problem response."""

    def __init__(self, slug: str, detail: str = "", headers: dict | None = None, **extra) -> None:
        self.slug = slug
        self.status, self.title, _ = CATALOG[slug]
        self.detail = detail
        self.headers = headers or {}
        self.extra = extra


def problem_body(slug: str, detail: str = "", instance: str = "", **extra) -> dict:
    status, title, _ = CATALOG[slug]
    body = {"type": f"/problems/{slug}", "title": title, "status": status}
    if detail:
        body["detail"] = detail
    if instance:
        body["instance"] = instance
    body.update(extra)
    return body


def problem_response(slug: str, detail: str = "", instance: str = "",
                     headers: dict | None = None, **extra) -> JSONResponse:
    body = problem_body(slug, detail, instance, **extra)
    return JSONResponse(body, status_code=body["status"], media_type=PROBLEM_JSON, headers=headers)


async def _problem_error(request: Request, exc: ProblemError) -> JSONResponse:
    return problem_response(exc.slug, exc.detail, request.url.path, exc.headers, **exc.extra)


async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = [
        {"loc": list(err.get("loc", [])), "msg": err.get("msg", ""), "type": err.get("type", "")}
        for err in exc.errors()
    ]
    return problem_response("validation", "The request did not validate.", request.url.path,
                            errors=errors)


_STATUS_SLUG = {404: "not-found", 405: "method-not-allowed", 401: "unauthorized"}


async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    slug = _STATUS_SLUG.get(exc.status_code)
    if slug is None:
        body = {"type": "about:blank", "title": str(exc.detail), "status": exc.status_code,
                "instance": request.url.path}
        return JSONResponse(body, status_code=exc.status_code, media_type=PROBLEM_JSON)
    detail = "" if exc.detail in (None, "Not Found", "Method Not Allowed") else str(exc.detail)
    return problem_response(slug, detail, request.url.path, headers=getattr(exc, "headers", None))


def install(app) -> None:
    """Register the handlers and the human-readable `/problems/<slug>` pages."""
    app.add_exception_handler(ProblemError, _problem_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)

    @app.get("/problems/{slug}", include_in_schema=False, response_class=HTMLResponse)
    def problem_page(slug: str) -> HTMLResponse:
        if slug not in CATALOG:
            return HTMLResponse("<h1>Unknown problem type</h1>", status_code=404)
        status, title, text = CATALOG[slug]
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8>"
            f"<title>{escape(title)}</title>"
            "<link rel=stylesheet href=/static/app.css>"
            "<main class=problem-page>"
            f"<p class=mono>HTTP {status} · /problems/{escape(slug)}</p>"
            f"<h1>{escape(title)}</h1><p>{escape(text)}</p>"
            "<p><a href=/>Back to PM Agent</a></p></main>"
        )
