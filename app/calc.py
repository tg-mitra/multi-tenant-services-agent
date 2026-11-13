"""Deterministic calculations (Step 6). The model never does this arithmetic: it calls a tool and
reports the result. All money uses Decimal."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def to_money(value) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def fmt_money(value: Decimal) -> str:
    return f"${to_money(value):,.2f}"


def remaining(limit: Decimal, used) -> Decimal:
    """What is left under a limit; never negative."""
    return max(Decimal("0.00"), to_money(limit) - to_money(used))


def money_line(limit: Decimal, met) -> dict:
    return {
        "limit": fmt_money(limit),
        "met_so_far": fmt_money(to_money(met)),
        "remaining": fmt_money(remaining(limit, met)),
    }


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def deadline_status(start: str, days: int, as_of: str) -> dict:
    """Deadline = start + `days` calendar days. The deadline day itself still counts as within."""
    deadline = parse_date(start) + timedelta(days=days)
    today = parse_date(as_of)
    left = (deadline - today).days
    return {
        "start_date": start,
        "days_allowed": days,
        "deadline": deadline.isoformat(),
        "as_of": as_of,
        "deadline_passed": today > deadline,
        "days_remaining": max(left, 0),
        "days_overdue": max(-left, 0),
    }
