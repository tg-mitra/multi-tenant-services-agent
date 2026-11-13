"""Regression gate: exits non-zero when quality drops below the thresholds (CI-ready).

    python -m evaluation.gate                       # check reports/eval_results.json (no model, no network)
    python -m evaluation.gate --results path.json --baseline reports/baseline.json

It reads a results file, so it runs anywhere (including CI without the pack or an API key) and the same
code is used right after a live run (python -m evaluation.run_eval).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_THRESHOLDS = HERE / "thresholds.json"
DEFAULT_RESULTS = HERE.parent / "reports" / "eval_results.json"
DEFAULT_BASELINE = HERE.parent / "reports" / "baseline.json"


def load_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def baseline_from(results: dict) -> dict:
    """The pass/fail of every case in a run, saved as the reference that later runs must not fall below."""
    meta = results["meta"]
    return {
        "_comment": "Pass/fail of each case in the last accepted run. The gate fails if a case that passed here fails later.",
        "generated": meta["run_at"], "model": meta["model"], "git_commit": meta["git_commit"],
        "data_pass": {c["id"]: bool(c["data_pass"]) for c in results["cases"]},
    }


def evaluate(results: dict, thresholds: dict, baseline: dict | None) -> dict:
    """Returns {"passed": bool, "checks": [...], "informational": [...]}; each check has name, ok, value,
    limit and justification."""
    m, cases = results["metrics"], results["cases"]
    safety = thresholds["safety_behaviours"]
    checks = []

    def add(name, ok, value, limit, key):
        checks.append({"name": name, "ok": bool(ok), "value": value, "limit": limit,
                       "justification": thresholds[key]["justification"] if key in thresholds else ""})

    add("hard failures (zero tolerance)", m["hard_failure_kinds"] == {} and not m["hard_failures"],
        len(m["hard_failures"]), f"<= {thresholds['hard_failures_max']['value']}", "hard_failures_max")

    s_total = sum(b["total"] for k, b in m["by_behaviour"].items() if k in safety)
    s_pass = sum(b["passed"] for k, b in m["by_behaviour"].items() if k in safety)
    s_rate = s_pass / s_total if s_total else 1.0
    add("safety behaviours pass rate", s_rate >= thresholds["safety_pass_rate_min"]["value"],
        f"{s_pass}/{s_total} ({s_rate:.0%})", f">= {thresholds['safety_pass_rate_min']['value']:.0%}", "safety_pass_rate_min")

    o_rate = m["data_passed"] / m["cases"] if m["cases"] else 0.0
    add("overall pass rate (data-consistent)", o_rate >= thresholds["overall_pass_rate_min"]["value"],
        f"{m['data_passed']}/{m['cases']} ({o_rate:.0%})", f">= {thresholds['overall_pass_rate_min']['value']:.0%}",
        "overall_pass_rate_min")

    regressions = []
    if baseline:
        was = baseline.get("data_pass", {})
        regressions = sorted(c["id"] for c in cases if was.get(c["id"]) is True and not c["data_pass"])
    add("regressions vs baseline", len(regressions) <= thresholds["max_new_regressions"]["value"],
        (", ".join(regressions) if regressions else ("none" if baseline else "no baseline file")),
        f"<= {thresholds['max_new_regressions']['value']}", "max_new_regressions")

    add("latency p95 (guard)", m["latency_ms"]["p95"] <= thresholds["latency_p95_ms_max"]["value"],
        f"{m['latency_ms']['p95'] / 1000:.1f} s", f"<= {thresholds['latency_p95_ms_max']['value'] / 1000:.0f} s",
        "latency_p95_ms_max")
    add("average cost per conversation", m["cost_usd"]["avg_per_conversation"] <= thresholds["avg_cost_usd_max"]["value"],
        f"${m['cost_usd']['avg_per_conversation']:.5f}", f"<= ${thresholds['avg_cost_usd_max']['value']}", "avg_cost_usd_max")
    add("unexpected HTTP errors", m["http_errors"] <= thresholds["http_errors_max"]["value"], m["http_errors"],
        f"<= {thresholds['http_errors_max']['value']}", "http_errors_max")

    target = thresholds["latency_target_ms"]["value"]
    info = [{"name": "NFR-3 latency target (p95)", "met": m["latency_ms"]["p95"] <= target,
             "value": f"{m['latency_ms']['p95'] / 1000:.1f} s", "target": f"{target / 1000:.0f} s"},
            {"name": "pack-expected pass rate (before data-consistent scoring)", "value": f"{m['pack_passed']}/{m['cases']}"}]
    return {"passed": all(c["ok"] for c in checks), "checks": checks, "informational": info}


def render(gate: dict) -> str:
    lines = []
    for c in gate["checks"]:
        lines.append(f"  [{'PASS' if c['ok'] else 'FAIL'}] {c['name']}: {c['value']} (limit {c['limit']})")
    for i in gate["informational"]:
        met = "" if "met" not in i else (" - met" if i["met"] else " - NOT met")
        lines.append(f"  [info] {i['name']}: {i['value']}{met}" + (f" (target {i['target']})" if "target" in i else ""))
    failed = [c for c in gate["checks"] if not c["ok"]]
    if failed:
        lines.append("")
        for c in failed:
            lines.append(f"  Why '{c['name']}' matters: {c['justification']}")
    lines.append("")
    lines.append("GATE: " + ("PASSED" if gate["passed"] else "FAILED"))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Apply the regression gate to an evaluation results file.")
    ap.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    ap.add_argument("--thresholds", type=Path, default=DEFAULT_THRESHOLDS)
    ap.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    ap.add_argument("--update-baseline", action="store_true",
                    help="save the pass/fail of the results file as the new baseline (use after an accepted run)")
    args = ap.parse_args(argv)
    if not args.results.exists():
        print(f"No results file at {args.results}. Run: python -m evaluation.run_eval", file=sys.stderr)
        return 2
    if args.update_baseline:
        results = load_json(args.results)
        args.baseline.write_text(json.dumps(baseline_from(results), indent=2), encoding="utf-8")
        print(f"Baseline written to {args.baseline} from {args.results.name} "
              f"({sum(baseline_from(results)['data_pass'].values())}/{len(results['cases'])} cases passing)")
        return 0
    baseline = load_json(args.baseline) if args.baseline.exists() else None
    gate = evaluate(load_json(args.results), load_json(args.thresholds), baseline)
    print(render(gate))
    return 0 if gate["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
