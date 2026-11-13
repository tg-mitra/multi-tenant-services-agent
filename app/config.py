"""Environment-driven settings. Values come from the process environment or a .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    openai_model: str
    openai_base_url: str | None
    llm_temperature: float | None  # None = use the model's default
    llm_timeout_seconds: float
    state_dir: Path
    history_turns: int  # most recent exchanges (rep message + reply) sent to the LLM; 0 = none
    member_api_base: str
    tenant_tokens: dict[str, str]  # tenant id -> bearer token
    http_timeout_seconds: float
    retry_after_max_seconds: float  # longest Retry-After we are willing to wait on a 429
    max_api_retries: int  # retries after a 429
    max_graph_steps: int  # LangGraph recursion limit: model+tool hops per turn
    as_of_date: str


def get_settings() -> Settings:
    return Settings(
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip(),
        openai_base_url=(os.getenv("OPENAI_BASE_URL", "").strip() or None),
        llm_temperature=(
            float(os.getenv("LLM_TEMPERATURE", "0")) if os.getenv("LLM_TEMPERATURE", "0").strip() else None
        ),
        llm_timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", "30")),
        member_api_base=os.getenv("MEMBER_API_BASE", "http://localhost:8090").strip().rstrip("/"),
        tenant_tokens={
            "plan_a": os.getenv("TENANT_TOKEN_PLAN_A", "").strip(),
            "plan_b": os.getenv("TENANT_TOKEN_PLAN_B", "").strip(),
        },
        http_timeout_seconds=float(os.getenv("HTTP_TIMEOUT_SECONDS", "10")),
        retry_after_max_seconds=float(os.getenv("RETRY_AFTER_MAX_SECONDS", "2")),
        max_api_retries=int(os.getenv("MAX_API_RETRIES", "2")),
        max_graph_steps=int(os.getenv("MAX_GRAPH_STEPS", "12")),
        as_of_date=os.getenv("AS_OF_DATE", "2026-11-15").strip(),
        history_turns=max(0, int(os.getenv("HISTORY_TURNS", "8").strip() or "8")),
        state_dir=Path(os.getenv("STATE_DIR", "./state").strip() or "./state"),
    )
