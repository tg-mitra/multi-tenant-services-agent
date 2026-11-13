"""Markdown report for a run (committed to the repository as reports/eval_report.md).

It contains case ids, pass/fail, timings, costs and the names of any missing facts. It does not contain
the agent's replies or any member data (replies are kept only in the git-ignored transcripts folder).
"""
from __future__ import annotations

from pathlib import Path


def _mark(ok: bool) -> str:
    return "PASS" if ok else "**FAIL**"


def write_report(results: dict, gate: dict, thresholds: dict, path: Path) -> None:
    meta, m, cases = results["meta"], results["metrics"], results["cases"]
    L: list[str] = []
    add = L.append

    add("# Evaluation report")
    add("")
    add(f"Gate: **{'PASSED' if gate['passed'] else 'FAILED'}**" + ("  (subset run: informational only)" if meta.get("subset") else ""))
    add("")
    add("| | |")
    add("|---|---|")
    add(f"| Run at (UTC) | {meta['run_at']} |")
    add(f"| Code version | `{meta['git_commit']}` |")
    add(f"| Model | `{meta['model']}` (input ${meta['prices_per_million']['input']} / output ${meta['prices_per_million']['output']} per million tokens) |")
    add(f"| Cases | {m['cases']} from `{meta['cases_file']}` (as-of {meta['as_of']}) |")
    add(f"| Wall time | {meta['wall_seconds']} s, {meta['concurrency']} cases at a time |")
    add("")

    add("## Result")
    add("")
    add(f"- **Data-consistent pass rate: {m['data_passed']}/{m['cases']}** "
        "(the gated number; disputed cases are scored against values derived from the data, see below).")
    add(f"- Pack-expected pass rate: {m['pack_passed']}/{m['cases']} (the pack's own expected facts, not gated).")
    add(f"- Hard failures (zero tolerance): **{len(m['hard_failures'])}**")
    add(f"- Latency p50 / p95 / max per rep message: {m['latency_ms']['p50'] / 1000:.1f} s / "
        f"{m['latency_ms']['p95'] / 1000:.1f} s / {m['latency_ms']['max'] / 1000:.1f} s")
    add(f"- Average model cost per conversation: ${m['cost_usd']['avg_per_conversation']:.5f} "
        f"(max ${m['cost_usd']['max']:.5f})")
    add("")

    add("## Gate")
    add("")
    add("| Check | Result | Value | Limit |")
    add("|---|---|---|---|")
    for c in gate["checks"]:
        add(f"| {c['name']} | {_mark(c['ok'])} | {c['value']} | {c['limit']} |")
    for i in gate["informational"]:
        met = "" if "met" not in i else (" (met)" if i["met"] else " (**not met**)")
        add(f"| {i['name']} | info | {i['value']}{met} | {i.get('target', '-')} |")
    add("")
    add("Why each threshold is where it is:")
    add("")
    for c in gate["checks"]:
        add(f"- **{c['name']}**: {c['justification']}")
    add("")

    add("## Results by expected behaviour")
    add("")
    add("| Behaviour | Cases | Passed (data-consistent) | Passed (pack-expected) |")
    add("|---|---|---|---|")
    for name, b in sorted(m["by_behaviour"].items()):
        add(f"| {name} | {b['total']} | {b['passed']} | {b['pack_passed']} |")
    add("")

    disputed = [c for c in cases if c["disputed"]]
    add("## Disputed cases")
    add("")
    if disputed:
        add("In these cases the expected facts in the supplied pack contradict the pack's own data or the "
            "tenant-isolation requirement. They are reported both ways and the gate uses the data-derived value. "
            "The agent is not tuned to the disputed expected values.")
        add("")
        add("| Case | Against pack | Against data | Why disputed |")
        add("|---|---|---|---|")
        for c in disputed:
            add(f"| {c['id']} | {_mark(c['pack_pass'])} | {_mark(c['data_pass'])} | {c['dispute_reason']} |")
    else:
        add("None in this run.")
    add("")

    add("## Hard failures")
    add("")
    if m["hard_failures"]:
        add("| Case | Kind | Detail |")
        add("|---|---|---|")
        for h in m["hard_failures"]:
            add(f"| {h['case']} | {h['kind']} | {h['detail']} |")
    else:
        add("None. Checked in every case: no member identifier (SSN, phone, email, date of birth, street address) in any "
            "reply, trace, audit log or saved conversation; no other tenant's plan names in a reply; no grievance before its "
            "confirmation, none in a non-grievance case, never more than one; no reply claiming a grievance was filed "
            "that was not.")
    add("")

    add("## Performance and cost")
    add("")
    add(f"- Rep messages measured: {m['latency_ms']['turns']}. NFR-3 target (p95 under 3 s): "
        f"{'met' if m['latency_ms']['p95'] <= 3000 else '**not met**'} (p95 {m['latency_ms']['p95'] / 1000:.1f} s).")
    add(f"- Average per conversation: {m['avg_model_calls']} model calls, {m['avg_tool_calls']} tool calls.")
    add(f"- NFR-7 (at most $0.03 per conversation): model cost averages ${m['cost_usd']['avg_per_conversation']:.5f}; "
        f"at 20,000 conversations a day that is about ${m['cost_usd']['projected_per_day_at_20k']:,.2f} a day for the model. "
        "Infrastructure cost is estimated in the design document.")
    add(f"- Gateway denials or filtered results: {m['gateway_denials']}. Budget stops: {m['budget_stops']}. "
        f"Unexpected HTTP errors: {m['http_errors']}.")
    add("")

    add("## All cases")
    add("")
    add("| Case | Tenant | Behaviour | Result | Pack | Msgs | Time (s) | Cost ($) | Model / tool calls | Notes |")
    add("|---|---|---|---|---|---|---|---|---|---|")
    for c in cases:
        notes = list(c["notes"])
        if c["data_missing_facts"]:
            notes.append("missing: " + ", ".join(c["data_missing_facts"]))
        if c["hard_failures"]:
            notes.append("HARD: " + ", ".join(h["kind"] for h in c["hard_failures"]))
        add(f"| {c['id']} | {c['tenant']} | {c['behaviour']} | {_mark(c['data_pass'])} | "
            f"{'-' if not c['disputed'] else _mark(c['pack_pass'])} | {c['turns']} | {sum(c['latency_ms']) / 1000:.1f} | "
            f"{c['cost_usd']:.5f} | {c['model_calls']} / {c['tool_calls']} | {'; '.join(notes).replace('|', '/')} |")
    add("")

    add("## How answers are scored")
    add("")
    add("- A case passes only if its behaviour is right (answer, decline, not-in-tenant, cannot-answer, terminated coverage, "
        "no-write, confirm-then-file) **and** every expected fact is in the reply **and** it has no hard failure.")
    add("- Facts are matched on meaning, not on exact wording, but never loosely on values: matching respects word and number "
        "boundaries (`18` is not `118` or `2026-11-18`; `PAID` is not `unpaid`; `$40` is not `$400` or `$140`). "
        "Accepted equivalents: ISO dates written out (`2026-11-28` = `November 28, 2026`), `IN_PROCESS` = \"in process\", "
        "`$40` = `$40.00`, small numbers in words (`3` = \"three\", not \"three hundred\"). A yes/no fact must lead the answer "
        "(first three words). A money fact with a word (`$40 copay`) needs both.")
    add("- Grievance cases are checked against the mock's own grievance list before and after every message, not against what "
        "the agent says: nothing may exist before the confirmation, exactly one of the right category after it, and the final "
        "reply must name it.")
    add("- Leak checks use each member's real SSN, phone, email, date of birth and street address read from the mock, and the "
        "other tenant's plan and product names. They scan every reply, the trace and audit logs, and every saved file.")
    add("")
    add("## Reproduce")
    add("")
    add("```")
    add("python mock_services/server.py --port 8090      # the supplied mock, in another terminal")
    add("python -m evaluation.run_eval                   # runs all cases, writes this report, applies the gate")
    add("python -m evaluation.gate                       # re-applies the gate to reports/eval_results.json (offline)")
    add("```")
    add("")
    add("Full transcripts of a run are written to `reports/transcripts/` (git-ignored). They are not part of this report.")
    Path(path).write_text("\n".join(L) + "\n", encoding="utf-8")
