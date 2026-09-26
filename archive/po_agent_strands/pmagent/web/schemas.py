"""
Request and response bodies of the HTTP API.

API BEST PRACTICE — every body is a typed Pydantic model, so FastAPI validates
input, documents output, and publishes an accurate OpenAPI document at
`/api/v1/openapi.json` (browse it at `/api/docs`). Conventions:

* snake_case fields; ISO-8601 UTC timestamps ending in `Z`;
* opaque, prefixed string ids (`conv_…`, `turn_…`, `apr_…`) — clients must not
  parse them;
* explicit `status` enums instead of booleans that multiply;
* collections are wrapped (`{"items": [...]}`) so fields can be added later
  without breaking clients;
* every resource carries the URLs a client needs next (`events_url`,
  `decision_url`) — clients follow links instead of building paths.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

MAX_MESSAGE_CHARS = 20_000


class Problem(BaseModel):
    """RFC 9457 problem details (served as application/problem+json)."""

    type: str = Field(examples=["/problems/turn-in-progress"])
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    model_calls: int = 0


class Health(BaseModel):
    status: Literal["ok"] = "ok"


class Meta(BaseModel):
    app: str
    version: str
    provider: str
    model: str
    jira_base_url: str | None
    jira_project: str
    lanes: list[str]
    reasoning_available: bool
    reasoning_note: str
    lucid_connected: bool
    auth_required: bool


class TurnCreate(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS,
                         description="What the user typed.")

    @field_validator("message")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value.strip()


class DecisionCreate(BaseModel):
    decision: Literal["approve", "reject"] = Field(
        description="Approve runs the paused writes; reject cancels them and the agent is told."
    )


class ToolCall(BaseModel):
    id: str
    name: str
    args: dict[str, Any]


class ApprovalOut(BaseModel):
    id: str
    turn_id: str
    status: Literal["pending", "approved", "rejected", "abandoned"]
    created_at: str
    decided_at: str | None
    calls: list[ToolCall]
    descriptions: list[str] = Field(description="What each write will do, as the CLI shows it.")
    warning: str | None = Field(description="Scope-check warning, when the CLI would print one.")
    decision_url: str


class Event(BaseModel):
    id: int
    event: str = Field(examples=["tool.started"])
    data: dict[str, Any]


class TurnOut(BaseModel):
    id: str
    conversation_id: str
    message: str
    status: Literal["running", "awaiting_approval", "completed", "failed"]
    created_at: str
    completed_at: str | None
    route: str
    route_reason: str
    reply: str
    error: Problem | None
    usage: Usage
    approval_ids: list[str]
    event_count: int
    url: str
    events_url: str


class TurnDetail(TurnOut):
    events: list[Event]


class TurnList(BaseModel):
    items: list[TurnDetail]


class TranscriptItem(BaseModel):
    role: Literal["user", "assistant"]
    text: str


class ConversationOut(BaseModel):
    id: str
    created_at: str
    status: Literal["idle", "running", "awaiting_approval"]
    route: str
    messages: list[TranscriptItem]
    pending_approval: ApprovalOut | None
    turns_url: str
