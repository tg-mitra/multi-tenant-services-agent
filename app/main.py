"""FastAPI entry point.

POST /conversations takes one rep message for one tenant and member. The work of a turn (model, gateway,
grievance confirmation, redaction, saving) is in app.turns; this module only validates the request,
serialises messages of the same conversation and maps errors to HTTP.

Use made-up data only unless the PHI filter is on (PHI_FILTER_ENABLED): saved files hold the conversation.
"""
from __future__ import annotations

import logging
import uuid
from typing import Literal, Optional

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from app.config import get_settings
from app.dev import PackNotFound, list_tenants_and_members
from app.store.jsonfile import CONVERSATION_ID_RE, IdempotencyStore, JsonConversationStore
from app.trace import read_trace
from app.turns import TurnError, process_turn

log = logging.getLogger("uvicorn.error")

app = FastAPI(title="Member Services Agent", version="0.3.0")


def new_conversation_id() -> str:
    return uuid.uuid4().hex


class ConversationRequest(BaseModel):
    tenant: Literal["plan_a", "plan_b"]
    member_id: str = Field(pattern=r"^M\d{6}$")
    message: str = Field(min_length=1, max_length=4000)
    # null, missing, blank or "0" (also the number 0) all mean "start a new conversation".
    conversation_id: Optional[str] = None

    @field_validator("conversation_id", mode="before")
    @classmethod
    def normalise_conversation_id(cls, v):
        if v is None:
            return None
        v = str(v).strip()
        if v in ("", "0"):
            return None
        if not CONVERSATION_ID_RE.fullmatch(v):
            raise ValueError("conversation_id may only contain letters, digits, '_' and '-' (max 64)")
        return v


class ConversationResponse(BaseModel):
    conversation_id: str
    reply: str
    # What this message and the conversation have used so far, and which limit stopped it (if any).
    budget: Optional[dict] = None


def _error(status: int, detail: str, conversation_id: str | None) -> JSONResponse:
    """Error body always carries conversation_id (null when no conversation exists)."""
    return JSONResponse(
        status_code=status, content={"detail": detail, "conversation_id": conversation_id}
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/dev/members")
async def dev_members(tenant: Optional[Literal["plan_a", "plan_b"]] = None):
    """Development helper: tenants with their member ids (no names or other PHI).

    Off unless DEV_ENDPOINTS_ENABLED=true; when off it behaves like any unknown route (404).
    """
    settings = get_settings()
    if not settings.dev_endpoints_enabled:
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    try:
        return list_tenants_and_members(settings, tenant)
    except PackNotFound:
        return JSONResponse(
            status_code=503,
            content={"detail": "Pack files not found. Set PACK_DIR to the folder that contains mock_services/."},
        )


@app.post("/conversations", response_model=ConversationResponse)
async def conversations(req: ConversationRequest):
    settings = get_settings()
    store = JsonConversationStore(settings.state_dir)
    idem = IdempotencyStore(settings.state_dir)

    is_new = req.conversation_id is None
    conversation_id = new_conversation_id() if is_new else req.conversation_id
    try:
        if is_new:
            result = await process_turn(req, settings, store, idem, conversation_id, True)
        else:
            # One message at a time per conversation: a double-clicked "confirm" is handled twice in
            # order, never at the same moment. A client-chosen id is never created (404 if unknown).
            async with store.turn_lock(req.tenant, req.member_id, conversation_id):
                result = await process_turn(req, settings, store, idem, conversation_id, False)
    except TurnError as err:
        return _error(err.status, err.detail, err.conversation_id)
    return ConversationResponse(conversation_id=result.conversation_id, reply=result.reply, budget=result.budget)


@app.get("/conversations/{conversation_id}/trace")
async def conversation_trace(
    conversation_id: str,
    tenant: Literal["plan_a", "plan_b"],
    member_id: str = Query(pattern=r"^M\d{6}$"),
    turn: Optional[int] = Query(default=None, ge=1),
):
    """The redacted step-by-step trace of a conversation: ids, timings, tokens, cost, gateway decisions.

    Never contains message text. Like /conversations, an unknown id, or one that belongs to another
    tenant or member, is simply 404 (no hint that it exists elsewhere)."""
    settings = get_settings()
    if not CONVERSATION_ID_RE.fullmatch(conversation_id) or JsonConversationStore(settings.state_dir).load(
            tenant, member_id, conversation_id) is None:
        return _error(404, "Conversation not found", None)
    return {"conversation_id": conversation_id,
            "events": read_trace(settings.state_dir, tenant, member_id, conversation_id, turn)}
