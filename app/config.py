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
    phi_filter_enabled: bool  # output guard, redaction of saved files, scrubbing of tool results
    max_verify_attempts: int  # verify_caller calls allowed per rep message
    dev_endpoints_enabled: bool  # GET /dev/members (reads the pack files directly; development only)
    pack_dir: Path  # root of the supplied pack (mock_services/, eval/, kb/ ...)
    kb_dir: Path  # plan documents (default PACK_DIR/kb)
    rag_top_k: int  # passages returned per plan-document search
    directory_api_base: str  # third-party provider directory (default: <MEMBER_API_BASE>/directory/v3)
    directory_api_key: str  # vendor key, sent as X-Api-Key; never given to the model
    pending_ttl_seconds: int  # how long a grievance proposal waits for the rep's confirmation
    max_grievance_retries: int  # retries of the grievance POST after a 503/timeout (same key, same bytes)
    grievance_retry_delay_seconds: float


def get_settings() -> Settings:
    pack_dir = Path(os.getenv("PACK_DIR", ".").strip() or ".")
    member_api_base = os.getenv("MEMBER_API_BASE", "http://localhost:8090").strip().rstrip("/")
    return Settings(
        directory_api_base=(os.getenv("DIRECTORY_API_BASE", "").strip().rstrip("/") or f"{member_api_base}/directory/v3"),
        directory_api_key=os.getenv("DIRECTORY_API_KEY", "").strip(),
        kb_dir=Path(os.getenv("KB_DIR", "").strip() or (pack_dir / "kb")),
        rag_top_k=max(1, int(os.getenv("RAG_TOP_K", "5"))),
        pending_ttl_seconds=max(30, int(os.getenv("PENDING_TTL_SECONDS", "900"))),
        max_grievance_retries=max(0, int(os.getenv("MAX_GRIEVANCE_RETRIES", "3"))),
        grievance_retry_delay_seconds=max(0.0, float(os.getenv("GRIEVANCE_RETRY_DELAY_SECONDS", "0.5"))),
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip(),
        openai_base_url=(os.getenv("OPENAI_BASE_URL", "").strip() or None),
        llm_temperature=(
            float(os.getenv("LLM_TEMPERATURE", "0")) if os.getenv("LLM_TEMPERATURE", "0").strip() else None
        ),
        llm_timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", "30")),
        member_api_base=member_api_base,
        tenant_tokens={
            "plan_a": os.getenv("TENANT_TOKEN_PLAN_A", "").strip(),
            "plan_b": os.getenv("TENANT_TOKEN_PLAN_B", "").strip(),
        },
        http_timeout_seconds=float(os.getenv("HTTP_TIMEOUT_SECONDS", "10")),
        retry_after_max_seconds=float(os.getenv("RETRY_AFTER_MAX_SECONDS", "2")),
        max_api_retries=int(os.getenv("MAX_API_RETRIES", "2")),
        max_graph_steps=int(os.getenv("MAX_GRAPH_STEPS", "12")),
        as_of_date=os.getenv("AS_OF_DATE", "2026-11-15").strip(),
        phi_filter_enabled=os.getenv("PHI_FILTER_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off"),
        max_verify_attempts=int(os.getenv("MAX_VERIFY_ATTEMPTS", "3")),
        dev_endpoints_enabled=os.getenv("DEV_ENDPOINTS_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on"),
        pack_dir=pack_dir,
        history_turns=max(0, int(os.getenv("HISTORY_TURNS", "8").strip() or "8")),
        state_dir=Path(os.getenv("STATE_DIR", "./state").strip() or "./state"),
    )
