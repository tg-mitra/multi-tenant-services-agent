"""FastAPI entry point.

Step 1: POST /conversations passes the message straight to the LLM (no filter).
Step 2: each conversation is saved as a JSON file under STATE_DIR after the LLM replies.

Do not use real member data until the PHI filter (Step 5) exists: the saved files
contain the raw text.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Literal, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from app.agent.graph import LLMNotConfigured, run_turn
from app.agent.history import turns_to_messages
from app.config import get_settings
from app.dev import PackNotFound, list_tenants_and_members
from app.gateway.gateway import SessionContext, ToolGateway
from app.gateway.phi import guard_output, redact_text
from app.store.jsonfile import CONVERSATION_ID_RE, JsonConversationStore

log = logging.getLogger("uvicorn.error")

app = FastAPI(title="Member Services Agent", version="0.2.0")


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

    is_new = req.conversation_id is None
    history = []
    if is_new:
        conversation_id = new_conversation_id()
    else:
        conversation_id = req.conversation_id
        # Never create a conversation under a client-chosen id. The path includes tenant and
        # member, so another tenant's or member's id is simply "not found" (no hint it exists).
        record = store.load(req.tenant, req.member_id, conversation_id)
        if record is None:
            return _error(404, "Conversation not found", None)
        history = turns_to_messages(record["turns"], settings.history_turns)

    # On failure a brand-new conversation has no file yet, so its id is not returned.
    known_id = None if is_new else conversation_id

    started = time.perf_counter()
    ctx = SessionContext(tenant=req.tenant, member_id=req.member_id, conversation_id=conversation_id)
    model_message = req.message
    if settings.phi_filter_enabled:
        # Minimum necessary: a full SSN, phone or email typed by the rep is not sent to the model.
        # The caller's last-4 / DOB stay, because verify_caller needs them.
        model_message, _ = redact_text(req.message, skip={"dob", "ssn_last4"})
    try:
        # Tenant and member come from the request/session only; the model cannot change them.
        async with ToolGateway(ctx, settings) as gateway:
            reply = await run_turn(model_message, history, gateway=gateway)
    except LLMNotConfigured:
        return _error(503, "LLM is not configured", known_id)
    except Exception as exc:
        # Log only the error class and provider status/code (no message text, no prompt).
        body = getattr(exc, "body", None)
        code = body.get("code") if isinstance(body, dict) else None
        log.error(
            "LLM call failed: type=%s status=%s code=%s",
            type(exc).__name__,
            getattr(exc, "status_code", None),
            code,
        )
        return _error(502, "LLM request failed", known_id)
    latency_ms = int((time.perf_counter() - started) * 1000)

    saved_rep_message = req.message
    if settings.phi_filter_enabled:
        # Output guard: never return an SSN/phone/email/DOB-shaped value to the rep.
        reply, blocked = guard_output(reply)
        if blocked:
            log.warning("phi output guard blocked reply conv=%s kinds=%s", conversation_id, ",".join(blocked))
        # What is written to disk is redacted (the rep may have typed identifiers).
        saved_rep_message, counts = redact_text(req.message)
        reply_to_save, reply_counts = redact_text(reply)
        if counts or reply_counts:
            log.info("phi redaction conv=%s in=%s out=%s", conversation_id, counts, reply_counts)
    else:
        reply_to_save = reply

    try:
        await store.append_turn(
            req.tenant,
            req.member_id,
            conversation_id,
            rep_message=saved_rep_message,
            assistant_reply=reply_to_save,
            model=settings.openai_model,
            latency_ms=latency_ms,
            is_new=is_new,
        )
    except Exception as exc:
        log.error("Saving conversation failed: type=%s", type(exc).__name__)
        return _error(500, "Could not save the conversation", known_id)

    return ConversationResponse(conversation_id=conversation_id, reply=reply)
