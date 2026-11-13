"""Grievance logic that must not depend on the model (Step 7).

  * classify_confirmation(): the rep's own words decide confirm / cancel / other. Rule based and
    conservative: anything unclear is "other", which never files.
  * build_payload(): the exact bytes sent to the API and the Idempotency-Key derived from them, so a
    retry resends byte-identical content (the mock compares raw bytes) and an edited description gets a
    new key.
  * pending-action helpers and the fixed texts shown to the rep.

The model has NO tool that files. It can only prepare a pending proposal; filing happens in the API
layer after the rep's next message is classified as "confirm".
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone

CATEGORIES = ("billing", "quality_of_care", "access", "customer_service", "other")
MAX_DESCRIPTION = 2000

# Cancel wins over everything: if in doubt, do not file.
CANCEL_RE = re.compile(
    r"\b(no|nope|don'?t|dont|do not|hold off|hold on|wait|cancel(?:l?ed)?|stop|not yet|not now|never ?mind|"
    r"abort|forget it|scrap)\b", re.I)
# Words that change or add to the request: the rep must restate it, so it is "other".
EDIT_RE = re.compile(
    r"\b(but|however|change[ds]?|instead|also|add|edit|update|modify|expedite[ds]?|urgent|different|except|"
    r"rather|fix|reword|replace)\b", re.I)
CONFIRM_RE = re.compile(
    r"\b(yes|yep|yeah|confirm(?:ed)?|go ahead|proceed|approved?|agrees?|agreed|file it|submit it|go for it)\b", re.I)
MAX_CONFIRM_WORDS = 60


def classify_confirmation(text: str) -> str:
    """Return "confirm", "cancel" or "other" for the rep's message about a pending proposal."""
    t = (text or "").strip()
    if not t:
        return "other"
    if CANCEL_RE.search(t):
        return "cancel"
    if "?" in t or EDIT_RE.search(t):
        return "other"
    if CONFIRM_RE.search(t) and len(t.split()) <= MAX_CONFIRM_WORDS:
        return "confirm"
    return "other"


def is_bare_confirmation(text: str) -> bool:
    """A short, pure "yes / confirmed / file it" (used when nothing is waiting for confirmation)."""
    return classify_confirmation(text) == "confirm" and len((text or "").split()) <= 15


def build_payload(tenant: str, member_id: str, category: str, description: str, expedite: bool) -> tuple[str, str]:
    """(body string, idempotency key). The body is serialised once and reused byte for byte on retries."""
    body = json.dumps(
        {"category": category, "description": description, "expedite_requested": bool(expedite)},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    key = "gv-" + hashlib.sha256(f"{tenant}|{member_id}|{body}".encode("utf-8")).hexdigest()[:40]
    return body, key


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def new_pending(tenant: str, member_id: str, category: str, description: str, expedite: bool,
                ttl_seconds: int, duplicates: list[str]) -> dict:
    body, key = build_payload(tenant, member_id, category, description, expedite)
    created = now()
    return {
        "id": uuid.uuid4().hex, "tenant": tenant, "member_id": member_id, "category": category,
        "description": description, "expedite_requested": bool(expedite), "body": body,
        "idempotency_key": key, "created_at": iso(created),
        "expires_at": iso(created + timedelta(seconds=ttl_seconds)),
        "other_turns": 0, "attempts": 0, "possible_duplicates": duplicates,
    }


def pending_expired(pending: dict) -> bool:
    try:
        return now() > datetime.fromisoformat(pending["expires_at"])
    except (KeyError, ValueError):
        return True  # unreadable = not usable


def pending_is_consistent(pending: dict, tenant: str, member_id: str) -> bool:
    """Defence in depth before any write: the stored proposal must match the session and its own hash."""
    try:
        body, key = build_payload(tenant, member_id, pending["category"], pending["description"],
                                  pending["expedite_requested"])
    except KeyError:
        return False
    return (pending.get("tenant") == tenant and pending.get("member_id") == member_id
            and pending.get("body") == body and pending.get("idempotency_key") == key
            and pending["category"] in CATEGORIES)


# ---- texts shown to the rep (fixed wording: the model does not write these) --------------------------

def readback_text(pending: dict) -> str:
    lines = [
        "Please read this grievance back to the member:",
        f"- Category: {pending['category']}",
        f"- Description: {pending['description']}",
    ]
    if pending["expedite_requested"]:
        lines.append("- Expedited handling: requested. The plan's clinical staff decide whether it is expedited.")
    else:
        lines.append("- Expedited handling: not requested")
    if pending.get("possible_duplicates"):
        lines.append(
            "- Note: the member already has an open grievance in this category ("
            + ", ".join(pending["possible_duplicates"])
            + "). File a new one only if this is a different issue (one grievance per issue)."
        )
    lines.append("")
    lines.append("If the member agrees, reply \"confirm\" to file it, or \"cancel\" to drop it. Nothing has been filed yet.")
    return "\n".join(lines)


def filed_text(grievance: dict, category: str, deadline: str | None, days: int | None, retried: bool) -> str:
    gid = grievance.get("grievance_id")
    text = (f"Grievance {gid} has been filed (category: {category}, status: {grievance.get('status')}, "
            f"received {grievance.get('received_date')}).")
    if retried:
        text += " The first attempt was not acknowledged; it was confirmed on retry and filed only once."
    if deadline and days:
        text += f" The plan's resolution deadline is {deadline} ({days} calendar days)."
    return text


def already_filed_text(grievance_id: str) -> str:
    return f"That grievance was already filed as {grievance_id}. It was not filed twice."


CANCELLED_TEXT = "Okay, I have not filed anything. The grievance request is cancelled."
NOTHING_PENDING_TEXT = ("No grievance is waiting for confirmation, so nothing was filed. "
                        "Tell me what the member wants to file and I will prepare it again.")
EXPIRED_TEXT = ("That grievance proposal is no longer active, so nothing was filed. "
                "Tell me what the member wants to file and I will prepare it again.")
UNCONFIRMED_TEXT = ("I could not confirm that the grievance was filed. Reply \"confirm\" to try again: "
                    "the same request key is reused, so it cannot be filed twice. Do not re-enter it by hand.")
FAILED_TEXT = ("The grievance could not be filed and nothing was recorded. "
               "Please try again later or file it through the usual manual process.")
