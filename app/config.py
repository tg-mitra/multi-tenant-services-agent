"""Environment-driven settings. Values come from the process environment or a .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    openai_model: str
    openai_base_url: str | None
    llm_temperature: float
    llm_timeout_seconds: float


def get_settings() -> Settings:
    return Settings(
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip(),
        openai_base_url=(os.getenv("OPENAI_BASE_URL", "").strip() or None),
        llm_temperature=float(os.getenv("LLM_TEMPERATURE", "0")),
        llm_timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", "30")),
    )
