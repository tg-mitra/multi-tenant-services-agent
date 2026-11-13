"""JSON-file conversation store (POC).

Layout: <STATE_DIR>/<tenant>/<member_id>/<conversation_id>.json

One file per conversation, written atomically (temp file + os.replace).
Single-process only: the per-conversation lock is in memory. Production would
use a managed database behind this same interface.

NOTE: until the PHI filter exists (Step 5) these files hold exactly what the rep
typed and what the model answered. Keep STATE_DIR out of git.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

TENANTS = {"plan_a", "plan_b"}
MEMBER_ID_RE = re.compile(r"^M\d{6}$")
CONVERSATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

IDEMPOTENCY_KEY_RE = re.compile(r"^gv-[0-9a-f]{40}$")
_UNSET = object()  # "leave this field as it is" (None means "clear it")
_locks: dict[str, asyncio.Lock] = {}
_turn_locks: dict[str, asyncio.Lock] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JsonConversationStore:
    def __init__(self, base_dir: Path):
        self.base = Path(base_dir).resolve()

    # ---- paths ---------------------------------------------------------
    def _path(self, tenant: str, member_id: str, conversation_id: str) -> Path:
        # Defence in depth: the API validates these too, but they become path parts.
        if tenant not in TENANTS:
            raise ValueError("invalid tenant")
        if not MEMBER_ID_RE.fullmatch(member_id):
            raise ValueError("invalid member_id")
        if not CONVERSATION_ID_RE.fullmatch(conversation_id):
            raise ValueError("invalid conversation_id")
        path = (self.base / tenant / member_id / f"{conversation_id}.json").resolve()
        if self.base not in path.parents:
            raise ValueError("path escapes state directory")
        return path

    # ---- read ----------------------------------------------------------
    def load(self, tenant: str, member_id: str, conversation_id: str) -> dict | None:
        path = self._path(tenant, member_id, conversation_id)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    # ---- write ---------------------------------------------------------
    def _write_atomic(self, path: Path, record: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    async def append_turn(
        self,
        tenant: str,
        member_id: str,
        conversation_id: str,
        *,
        rep_message: str,
        assistant_reply: str,
        model: str,
        latency_ms: int,
        is_new: bool,
        pending=_UNSET,
        last_event=_UNSET,
    ) -> dict:
        """Add one exchange. Creates the file only when is_new is True.

        `pending` sets (dict) or clears (None) the grievance proposal waiting for the rep's confirmation;
        `last_event` records the latest grievance event (proposed/filed/cancelled/expired). Omit to keep.
        """
        path = self._path(tenant, member_id, conversation_id)
        lock = _locks.setdefault(str(path), asyncio.Lock())
        async with lock:
            record = self.load(tenant, member_id, conversation_id)
            if record is None:
                if not is_new:
                    raise FileNotFoundError("conversation does not exist")
                now = _now()
                record = {
                    "conversation_id": conversation_id,
                    "tenant": tenant,
                    "member_id": member_id,
                    "created_at": now,
                    "updated_at": now,
                    "turns": [],
                }
            record["turns"].append(
                {
                    "turn": len(record["turns"]) + 1,
                    "ts": _now(),
                    "rep": rep_message,
                    "assistant": assistant_reply,
                    "model": model,
                    "latency_ms": latency_ms,
                }
            )
            record["updated_at"] = record["turns"][-1]["ts"]
            if pending is not _UNSET:
                record["pending_action"] = pending
            if last_event is not _UNSET:
                record["last_grievance_event"] = last_event
            self._write_atomic(path, record)
            return record

    def turn_lock(self, tenant: str, member_id: str, conversation_id: str) -> asyncio.Lock:
        """One lock per conversation, held for a whole rep message so two requests (for example a
        double-clicked "confirm") are handled one after the other, never at the same time."""
        return _turn_locks.setdefault(str(self._path(tenant, member_id, conversation_id)), asyncio.Lock())


class IdempotencyStore:
    """One small JSON file per grievance request key: state/<tenant>/idempotency/<key>.json.

    status: "in_flight" (written BEFORE the POST) | "done" (with the grievance) | "failed".
    """

    def __init__(self, base_dir: Path):
        self.base = Path(base_dir).resolve()

    def _path(self, tenant: str, key: str) -> Path:
        if tenant not in TENANTS or not IDEMPOTENCY_KEY_RE.fullmatch(key):
            raise ValueError("invalid tenant or idempotency key")
        path = (self.base / tenant / "idempotency" / f"{key}.json").resolve()
        if self.base not in path.parents:
            raise ValueError("path escapes state directory")
        return path

    def get(self, tenant: str, key: str) -> dict | None:
        try:
            return json.loads(self._path(tenant, key).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def put(self, tenant: str, key: str, record: dict) -> None:
        path = self._path(tenant, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({**record, "updated_at": _now()}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
