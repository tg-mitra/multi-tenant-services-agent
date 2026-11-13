"""FastAPI entry point.

Step 1: POST /conversations passes the message straight to the LLM.
No filter and no storage yet. tenant and member_id are validated but not used.
Do not use real member data until the PHI filter (Step 5) exists.
"""
from __future__ import annotations

import logging
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.agent.graph import LLMNotConfigured, run_turn

log = logging.getLogger("uvicorn.error")

app = FastAPI(title="Member Services Agent", version="0.1.0")


class ConversationRequest(BaseModel):
    tenant: Literal["plan_a", "plan_b"]
    member_id: str = Field(pattern=r"^M\d{6}$")
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: Optional[str] = Field(default=None, max_length=64)


class ConversationResponse(BaseModel):
    reply: str


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/conversations", response_model=ConversationResponse)
async def conversations(req: ConversationRequest) -> ConversationResponse:
    try:
        reply = await run_turn(req.message)
    except LLMNotConfigured:
        raise HTTPException(status_code=503, detail="LLM is not configured")
    except Exception as exc:
        # Log only the error class and provider status/code to the server console
        # (no message text, no prompt). Never echo upstream details to the client.
        body = getattr(exc, "body", None)
        code = body.get("code") if isinstance(body, dict) else None
        log.error(
            "LLM call failed: type=%s status=%s code=%s",
            type(exc).__name__,
            getattr(exc, "status_code", None),
            code,
        )
        raise HTTPException(status_code=502, detail="LLM request failed")
    return ConversationResponse(reply=reply)
