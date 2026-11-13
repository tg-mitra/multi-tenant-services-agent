"""Run the evaluation cases against the agent, score them, write the results and the report, apply the gate.

    python -m evaluation.run_eval                          # all cases, in-process, then the gate
    python -m evaluation.run_eval --ids EV-16,EV-17        # a few cases
    python -m evaluation.run_eval --base-url http://localhost:8000   # against a running server
    python -m evaluation.run_eval --update-baseline        # save this run's pass/fail as the new baseline

Needs the mock service running (MEMBER_API_BASE), the model key in .env, and the pack (PACK_DIR or --cases).
The mock is only READ by the harness (member records for leak detection, grievance lists to count writes).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.config import Settings, get_settings
from evaluation import gate as gate_mod
from evaluation.metrics import summarise
from evaluation.report import write_report
from evaluation.scoring import CaseRun, forbidden_strings, leaks_in, other_tenant_terms, score_case

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"


# ---- reading the mock (the harness only reads) ---------------------------------------------------------------

class Mock:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.AsyncClient(base_url=settings.member_api_base, timeout=20)

    async def get(self, tenant: str, path: str) -> tuple[int, dict | None]:
        token = self.settings.tenant_tokens.get(tenant, "")
        for attempt in range(4):
            try:
                r = await self.client.get(path, headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError:
                return 0, None
            if r.status_code == 429:
                await asyncio.sleep(1.2)
                continue
            return r.status_code, (r.json() if r.status_code == 200 else None)
        return 429, None

    async def member(self, tenant: str, member_id: str) -> dict | None:
        status, data = await self.get(tenant, f"/tenants/{tenant}/members/{member_id}")
        return data if status == 200 else None

    async def grievances(self, tenant: str, member_id: str) -> dict:
        status, data = await self.get(tenant, f"/tenants/{tenant}/members/{member_id}/grievances")
        return {g["grievance_id"]: g["category"] for g in (data or {}).get("data", [])}

    async def close(self):
        await self.client.aclose()


# ---- one case ----------------------------------------------------------------------------------------------------------

async def run_case(case: dict, app_client: httpx.AsyncClient, mock: Mock, tenants: dict, extra_terms: dict) -> CaseRun:
    tenant, member = case["tenant"], case["member_id"]
    run = CaseRun(case=case)
    # PHI of this member id in EVERY tenant that has it (a member id can exist in two tenants)
    for tid in tenants:
        run.forbidden += forbidden_strings(await mock.member(tid, member))
    run.other_terms = other_tenant_terms(tenant, tenants, extra_terms)
    run.grievances_before = await mock.grievances(tenant, member)

    conv: str | int = 0
    budget: dict = {}
    for turn in case["turns"]:
        started = time.perf_counter()
        try:
            r = await app_client.post("/conversations", json={"tenant": tenant, "member_id": member,
                                                              "message": turn["text"], "conversation_id": conv})
            body = r.json()
        except Exception as exc:  # network failure talking to our own API
            run.error = f"request failed: {type(exc).__name__}"
            run.statuses.append(0)
            run.latencies_ms.append(int((time.perf_counter() - started) * 1000))
            break
        run.latencies_ms.append(int((time.perf_counter() - started) * 1000))
        run.statuses.append(r.status_code)
        run.replies.append(body.get("reply") or body.get("detail") or "")
        run.grievances_after_turn.append(await mock.grievances(tenant, member))
        if r.status_code != 200:
            run.error = f"HTTP {r.status_code}"
            break
        conv = body["conversation_id"]
        run.conversation_id = conv
        budget = body.get("budget") or {}
    run.cost_usd = (budget.get("conversation") or {}).get("cost_usd", 0.0)
    if run.conversation_id:
        t = await app_client.get(f"/conversations/{run.conversation_id}/trace", params={"tenant": tenant, "member_id": member})
        if t.status_code == 200:
            run.trace = t.json()["events"]
            ends = [e for e in run.trace if e["type"] == "turn" and e["name"] == "end"]
            run.model_calls = sum(e.get("model_calls", 0) for e in ends)
            run.tool_calls = sum(e.get("tool_calls", 0) for e in ends)
    return run


# ---- the whole run -------------------------------------------------------------------------------------------------------

def git_commit() -> str:
    """Short hash of HEAD, with '+uncommitted' when the working tree has changes (the run is not exactly that commit)."""
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                              timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True,
                               timeout=10).stdout.strip()
        return (head + ("+uncommitted" if dirty else "")) if head else "unknown"
    except Exception:
        return "unknown"


def scan_state_files(state_dir: Path, forbidden: list[str]) -> list[dict]:
    """Sensitive values anywhere in what the agent saved (conversations, traces, audit, idempotency)."""
    bad = 0
    for p in Path(state_dir).rglob("*"):
        if p.is_file() and p.suffix in (".json", ".jsonl"):
            bad += len(leaks_in(p.read_text(encoding="utf-8", errors="replace"), forbidden))
    return [{"case": "(all)", "kind": "phi_leak", "detail": f"{bad} sensitive value(s) found in saved state files"}] if bad else []


async def run_all(args) -> int:
    settings = get_settings()
    cases_path = Path(args.cases or (settings.pack_dir / "eval" / "conversations.json"))
    if not cases_path.exists():
        print(f"Cannot find the evaluation cases at {cases_path}. Set PACK_DIR or pass --cases.", file=sys.stderr)
        return 2
    pack = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = pack["cases"]
    if args.ids:
        wanted = {i.strip() for i in args.ids.split(",")}
        cases = [c for c in cases if c["id"] in wanted]
    thresholds = gate_mod.load_json(Path(args.thresholds))
    disputed = {k: v for k, v in gate_mod.load_json(Path(args.disputed)).items() if not k.startswith("_")}

    tenants_file = settings.pack_dir / "mock_services" / "data" / "tenants.json"
    tenants = json.loads(tenants_file.read_text(encoding="utf-8"))["tenants"] if tenants_file.exists() else {}
    for t in tenants.values():
        t.pop("api_token", None)  # never keep tokens around

    mock = Mock(settings)
    status, _ = await mock.get("plan_a", "/tenants/plan_a/members/M000000")
    if status in (0, 401):
        print(f"Cannot reach the mock service at {settings.member_api_base} (or the token is wrong). Start it first.",
              file=sys.stderr)
        return 2

    state_dir = Path(args.state_dir) if args.state_dir else Path(tempfile.mkdtemp(prefix="vtask_eval_state_"))
    os.environ["STATE_DIR"] = str(state_dir)
    if args.base_url:
        app_client = httpx.AsyncClient(base_url=args.base_url, timeout=300)
    else:
        from app.main import app  # imported late so STATE_DIR above is the one in use
        app_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://eval", timeout=300)

    sem = asyncio.Semaphore(args.concurrency)
    started_at = datetime.now(timezone.utc)
    t0 = time.perf_counter()

    async def one(case):
        async with sem:
            run = await run_case(case, app_client, mock, tenants, thresholds.get("cross_tenant_terms_extra", {}))
            res = score_case(run, disputed)
            print(f"  {case['id']:6} {'PASS' if res['data_pass'] else 'FAIL'}"
                  f"{'  (pack: ' + ('pass' if res['pack_pass'] else 'fail') + ')' if res['disputed'] else ''}"
                  f"  {sum(run.latencies_ms) / 1000:5.1f}s  hard={len(res['hard_failures'])}"
                  f"{'  ' + '; '.join(res['notes']) if res['notes'] else ''}", flush=True)
            return run, res

    print(f"Running {len(cases)} cases (concurrency {args.concurrency}) against "
          f"{'the app in-process' if not args.base_url else args.base_url}; state in {state_dir}")
    pairs = await asyncio.gather(*[one(c) for c in cases])
    runs, results = [p[0] for p in pairs], [p[1] for p in pairs]
    wall = time.perf_counter() - t0

    all_forbidden = sorted({s for r in runs for s in r.forbidden})
    extra = scan_state_files(state_dir, all_forbidden)
    metrics = summarise(results, extra)
    out = {
        "meta": {
            "run_at": started_at.isoformat(timespec="seconds"), "wall_seconds": round(wall, 1),
            "git_commit": git_commit(), "model": settings.openai_model, "as_of": pack.get("as_of"),
            "cases_file": cases_path.name, "concurrency": args.concurrency,
            "prices_per_million": {"input": settings.llm_input_cost_per_million, "output": settings.llm_output_cost_per_million},
            "subset": bool(args.ids),
        },
        "metrics": metrics, "cases": results,
    }
    out_dir = Path(args.out)
    if args.ids and out_dir == REPORTS:  # a subset run must never overwrite the committed full report
        out_dir = REPORTS / "subset"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "eval_results.json").write_text(json.dumps(out, indent=2), encoding="utf-8")

    # full transcripts for debugging only: git-ignored, never part of the committed report
    tdir = out_dir / "transcripts"
    tdir.mkdir(exist_ok=True)
    with open(tdir / f"run_{started_at:%Y%m%d_%H%M%S}.jsonl", "w", encoding="utf-8") as fh:
        for run in runs:
            fh.write(json.dumps({"id": run.case["id"], "turns": [t["text"] for t in run.case["turns"]],
                                 "replies": run.replies, "created": run.created(), "error": run.error}) + "\n")

    baseline_path = Path(args.baseline)
    if args.update_baseline:
        baseline_path.write_text(json.dumps(gate_mod.baseline_from(out), indent=2), encoding="utf-8")
        print(f"Baseline updated: {baseline_path}")
    baseline = gate_mod.load_json(baseline_path) if baseline_path.exists() else None
    gate = gate_mod.evaluate(out, thresholds, baseline)
    write_report(out, gate, thresholds, out_dir / "eval_report.md")
    await app_client.aclose()
    await mock.close()
    if not args.keep_state and not args.state_dir:
        shutil.rmtree(state_dir, ignore_errors=True)

    print(f"\n{metrics['data_passed']}/{metrics['cases']} passed (data-consistent), "
          f"{metrics['pack_passed']}/{metrics['cases']} against the pack's own expected values; "
          f"hard failures: {len(metrics['hard_failures'])}; p95 {metrics['latency_ms']['p95'] / 1000:.1f}s; "
          f"avg cost ${metrics['cost_usd']['avg_per_conversation']:.5f}")
    print(f"Report: {out_dir / 'eval_report.md'}\n")
    if args.ids:
        print("(subset run: gate result is informational)")
    print(gate_mod.render(gate))
    if args.no_gate or args.ids:
        return 0
    return 0 if gate["passed"] else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the evaluation cases and apply the regression gate.")
    ap.add_argument("--cases", help="path to conversations.json (default PACK_DIR/eval/conversations.json)")
    ap.add_argument("--ids", help="comma separated case ids to run (a subset run never fails the build)")
    ap.add_argument("--base-url", help="test a running server instead of the in-process app")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--out", default=str(REPORTS))
    ap.add_argument("--state-dir", help="keep agent state here (default: a temporary folder, deleted afterwards)")
    ap.add_argument("--keep-state", action="store_true")
    ap.add_argument("--thresholds", default=str(gate_mod.DEFAULT_THRESHOLDS))
    ap.add_argument("--disputed", default=str(Path(__file__).resolve().parent / "disputed.json"))
    ap.add_argument("--baseline", default=str(gate_mod.DEFAULT_BASELINE))
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--no-gate", action="store_true", help="always exit 0 (still prints the gate)")
    return asyncio.run(run_all(ap.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
