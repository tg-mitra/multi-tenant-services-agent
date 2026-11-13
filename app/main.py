"""FastAPI entry point.

Step 1: POST /conversations passes the message straight to the LLM.
No filter and no storage yet. tenant and member_id are validated but not used.
Do not use real member data until the PHI filter (Step 5) exists.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.agent.graph import LLMNotConfigured, run_turn

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
    except Exception:
        # Never echo upstream details or the user's text back.
        raise HTTPException(status_code=502, detail="LLM request failed")
    return ConversationResponse(reply=reply)
