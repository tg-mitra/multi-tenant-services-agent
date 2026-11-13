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
from app.config import get_settings
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


@app.post("/conversations", response_model=ConversationResponse)
async def conversations(req: ConversationRequest):
    settings = get_settings()
    store = JsonConversationStore(settings.state_dir)

    is_new = req.conversation_id is None
    if is_new:
        conversation_id = new_conversation_id()
    else:
        conversation_id = req.conversation_id
        # Never create a conversation under a client-chosen id. The path includes tenant and
        # member, so another tenant's or member's id is simply "not found" (no hint it exists).
        if store.load(req.tenant, req.member_id, conversation_id) is None:
            return _error(404, "Conversation not found", None)

    # On failure a brand-new conversation has no file yet, so its id is not returned.
    known_id = None if is_new else conversation_id

    started = time.perf_counter()
    try:
        reply = await run_turn(req.message)
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

    try:
        await store.append_turn(
            req.tenant,
            req.member_id,
            conversation_id,
            rep_message=req.message,
            assistant_reply=reply,
            model=settings.openai_model,
            latency_ms=latency_ms,
            is_new=is_new,
        )
    except Exception as exc:
        log.error("Saving conversation failed: type=%s", type(exc).__name__)
        return _error(500, "Could not save the conversation", known_id)

    return ConversationResponse(conversation_id=conversation_id, reply=reply)
