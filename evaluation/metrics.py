"""Numbers computed from the per-case results (shared by the gate and the report)."""
from __future__ import annotations

import math
from collections import Counter, defaultdict

DAILY_CONVERSATIONS = 20_000  # NFR-7


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile (no interpolation): honest for small samples."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return float(ordered[rank - 1])


def summarise(cases: list[dict], extra_hard_failures: list[dict] | None = None) -> dict:
    total = len(cases)
    hard = [{"case": c["id"], **h} for c in cases for h in c["hard_failures"]] + list(extra_hard_failures or [])
    by_behaviour: dict[str, dict] = defaultdict(lambda: {"total": 0, "passed": 0, "pack_passed": 0})
    for c in cases:
        b = by_behaviour[c["behaviour"]]
        b["total"] += 1
        b["passed"] += bool(c["data_pass"])
        b["pack_passed"] += bool(c["pack_pass"])
    turn_latencies = [ms for c in cases for ms in c["latency_ms"]]
    costs = [c["cost_usd"] for c in cases]
    avg_cost = sum(costs) / total if total else 0.0
    return {
        "cases": total,
        "pack_passed": sum(bool(c["pack_pass"]) for c in cases),
        "data_passed": sum(bool(c["data_pass"]) for c in cases),
        "disputed": [c["id"] for c in cases if c["disputed"]],
        "by_behaviour": dict(by_behaviour),
        "hard_failures": hard,
        "hard_failure_kinds": dict(Counter(h["kind"] for h in hard)),
        "latency_ms": {"turns": len(turn_latencies), "p50": percentile(turn_latencies, 50),
                       "p95": percentile(turn_latencies, 95), "max": float(max(turn_latencies, default=0))},
        "cost_usd": {"avg_per_conversation": round(avg_cost, 8), "max": round(max(costs, default=0.0), 8),
                     "projected_per_day_at_20k": round(avg_cost * DAILY_CONVERSATIONS, 2)},
        "avg_model_calls": round(sum(c["model_calls"] for c in cases) / total, 2) if total else 0,
        "avg_tool_calls": round(sum(c["tool_calls"] for c in cases) / total, 2) if total else 0,
        "gateway_denials": sum(c["gateway_denials"] for c in cases),
        "budget_stops": sum(c["budget_stops"] for c in cases),
        "http_errors": sum(1 for c in cases for s in c["http_statuses"] if s != 200) + sum(1 for c in cases if c["error"]),
    }
