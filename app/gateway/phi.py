"""PHI filter helpers (Step 5): detection, redaction and the output guard.

Layers (see SPEC 6.2):
  1. Field allow-list projection in the gateway (always on, Step 4): the model never receives
     SSN, DOB, contact details, address, last name or notes.
  2. Free-text scrubbing of tool results and redaction of what is written to disk (this module,
     switchable with PHI_FILTER_ENABLED).
  3. Output guard: a reply that contains an SSN-, phone- or email-shaped value is replaced.

Honest limits: these are pattern-based backstops. They miss unusual formats and cannot find names.
The allow-list is the real control. A production system would add a DLP/NER detector.
"""
from __future__ import annotations

import re
from typing import Any

# 9 digits with optional separators, not part of a longer digit run (so 10-digit NPIs and
# claim ids such as C2639555547 do not match).
SSN_RE = re.compile(r"(?<!\d)\d{3}[- ]?\d{2}[- ]?\d{4}(?!\d)")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# A date written right after a birth-date keyword (plain claim/service dates are NOT touched).
DOB_RE = re.compile(
    r"(?i)(\b(?:dob|d\.o\.b\.?|date of birth|birth ?date|born(?: on)?)\b[^\d\n]{0,20})"
    r"(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})"
)
# Four digits right after an SSN keyword ("last four digits of their SSN as 2331").
SSN_LAST4_RE = re.compile(
    r"(?i)(\b(?:ssn|social security(?: number)?|last (?:four|4))\b[^\d\n]{0,40})(\d{4})(?!\d)"
)

BLOCKED_REPLY = (
    "I can't share that. Sensitive identifiers such as a Social Security number, date of birth, "
    "phone, email or address are not available through this assistant."
)


def redact_text(text: str, skip: frozenset[str] | set[str] = frozenset()) -> tuple[str, dict[str, int]]:
    """Return (text with sensitive values replaced by placeholders, count per kind).

    `skip` lists kinds to leave in place, e.g. {"dob", "ssn_last4"} for the message that goes to the
    model: the verify tool needs those two values from the rep, nothing else.
    """
    counts: dict[str, int] = {}

    def sub(pattern: re.Pattern, repl, kind: str, s: str) -> str:
        if kind in skip:
            return s
        new, n = pattern.subn(repl, s)
        if n:
            counts[kind] = counts.get(kind, 0) + n
        return new

    text = sub(DOB_RE, r"\1[DOB]", "dob", text)
    text = sub(SSN_LAST4_RE, r"\1[SSN_LAST4]", "ssn_last4", text)
    text = sub(EMAIL_RE, "[EMAIL]", "email", text)
    text = sub(SSN_RE, "[SSN]", "ssn", text)
    text = sub(PHONE_RE, "[PHONE]", "phone", text)
    return text, counts


def detect(text: str) -> list[str]:
    """Kinds of sensitive values present (used by the output guard)."""
    kinds = []
    if SSN_RE.search(text):
        kinds.append("ssn")
    if PHONE_RE.search(text):
        kinds.append("phone")
    if EMAIL_RE.search(text):
        kinds.append("email")
    if DOB_RE.search(text):
        kinds.append("dob")
    return kinds


def guard_output(text: str) -> tuple[str, list[str]]:
    """Output guard: replace the whole reply if it contains a sensitive value."""
    kinds = detect(text)
    if kinds:
        return BLOCKED_REPLY, kinds
    return text, []


def sanitize_result(value: Any) -> Any:
    """Redact sensitive patterns in every string of a tool result (keys are left alone)."""
    if isinstance(value, str):
        return redact_text(value)[0]
    if isinstance(value, list):
        return [sanitize_result(v) for v in value]
    if isinstance(value, dict):
        return {k: sanitize_result(v) for k, v in value.items()}
    return value
