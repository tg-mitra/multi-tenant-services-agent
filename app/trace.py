"""Trace and audit log (Step 9).

One event per step of a conversation: model calls, tool calls, gateway decisions, guards and
confirmation changes. Events carry ids, hashes, timings, token counts, cost and counts of
redactions. They NEVER carry message text, prompts, tool payloads or answers.

Safety by construction: event() accepts only the field names in ALLOWED (anything else is dropped),
truncates strings, and passes strings through the PHI redactor, so a careless call cannot leak content.

Two append-only JSONL sinks, both tenant-prefixed:
  * state/<tenant>/<member>/<conversation>.trace.jsonl   the conversation's own trace (GET .../trace)
  * state/<tenant>/audit/<yyyy-mm-dd>.jsonl               tenant-wide audit log (retention: 6 years in production)
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings
from app.gateway.phi import redact_text
from app.store.jsonfile import CONVERSATION_ID_RE, MEMBER_ID_RE, TENANTS

ALLOWED = {
    "name", "status", "latency_ms", "tokens_in", "tokens_out", "estimated", "cost_usd", "reason", "intent",
    "http_status", "attempts", "count", "dropped", "redactions", "path", "stopped_by", "model", "budget",
    "model_calls", "tool_calls", "decision", "kinds", "state", "key_prefix", "category",
}
_write_lock = threading.Lock()


def member_hash(settings: Settings, tenant: str, member_id: str) -> str:
    return hashlib.sha256(f"{settings.trace_hash_salt}|{tenant}|{member_id}".encode()).hexdigest()[:12]


def _clean(value):
    if isinstance(value, str):
        return redact_text(value[:120])[0]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k)[:40]: _clean(v) for k, v in list(value.items())[:20]}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in list(value)[:20]]
    return str(value)[:40]


def trace_path(base: Path, tenant: str, member_id: str, conversation_id: str) -> Path:
    if tenant not in TENANTS or not MEMBER_ID_RE.fullmatch(member_id) or not CONVERSATION_ID_RE.fullmatch(conversation_id):
        raise ValueError("invalid trace location")
    base = Path(base).resolve()
    path = (base / tenant / member_id / f"{conversation_id}.trace.jsonl").resolve()
    if base not in path.parents:
        raise ValueError("path escapes state directory")
    return path


def read_trace(base: Path, tenant: str, member_id: str, conversation_id: str, turn: int | None = None) -> list[dict]:
    try:
        lines = trace_path(base, tenant, member_id, conversation_id).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    events = [json.loads(ln) for ln in lines if ln.strip()]
    return [e for e in events if turn is None or e.get("turn") == turn]


class Trace:
    """Writes the events of one rep message."""

    def __init__(self, settings: Settings, tenant: str, member_id: str, conversation_id: str, turn: int):
        self.settings, self.tenant, self.conversation_id, self.turn = settings, tenant, conversation_id, turn
        self.member_hash = member_hash(settings, tenant, member_id)
        self._paths = [
            trace_path(settings.state_dir, tenant, member_id, conversation_id),
            Path(settings.state_dir).resolve() / tenant / "audit" / f"{datetime.now(timezone.utc):%Y-%m-%d}.jsonl",
        ]
        self.seq = 0
        self.events: list[dict] = []
        self.budget = None  # set by process_turn so every event can show what is left

    def event(self, type_: str, **fields) -> dict:
        self.seq += 1
        ev = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "conversation_id": self.conversation_id, "tenant": self.tenant, "member_hash": self.member_hash,
            "turn": self.turn, "seq": self.seq, "type": type_,
        }
        ev.update({k: _clean(v) for k, v in fields.items() if k in ALLOWED and v is not None})
        if self.budget is not None and "budget" not in ev:
            ev["budget"] = self.budget.remaining()
        self.events.append(ev)
        line = json.dumps(ev, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            with _write_lock:
                for path in self._paths:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with open(path, "a", encoding="utf-8") as fh:  # append-only
                        fh.write(line)
        except OSError:  # tracing must never break a rep's answer
            pass
        return ev

    def decision(self, name: str, decision: str, **fields) -> dict:
        """A gateway decision: allowed / denied / filtered / retried / budget_stop ..."""
        return self.event("gateway_decision", name=name, decision=decision, **fields)
