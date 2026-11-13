# Member Services Agent (POC)

Governed multi-tenant assistant for member-services reps. See `SPEC.md` for the full specification and the step plan.

Dev helper: `GET /dev/members` (optional `?tenant=plan_a`) lists the tenants, their products and every member id with plan, coverage status and the eval cases that use that member, so you know what to send to `/conversations`. It returns no names, DOB, SSN or other PHI. It is off unless `DEV_ENDPOINTS_ENABLED=true` in `.env` and it reads the pack files directly (the mock API has no list endpoint), so it is for development only.

Current status: Step 10 (evaluation harness and regression gate).

```
python mock_services/server.py --port 8090      # the supplied mock, in another terminal
python -m evaluation.run_eval                   # run all 40 cases, write reports/, apply the gate (exit 1 on failure)
python -m evaluation.run_eval --ids EV-16,EV-17 # a few cases (output goes to reports/subset/, never the committed report)
python -m evaluation.gate                       # re-apply the gate to reports/eval_results.json (no model, no network)
python -m evaluation.gate --update-baseline     # accept the last run as the new baseline
python -m pytest -q                             # all unit and integration tests
```

The committed report is `reports/eval_report.md` (results: `reports/eval_results.json`, baseline: `reports/baseline.json`). The gate's thresholds, each with its justification, are in `evaluation/thresholds.json`; four cases whose expected facts in the pack contradict its own data are scored separately (`evaluation/disputed.json`). Details in `SPEC.md` (Step 10).

Earlier: Step 9 (budgets and trace). Every conversation has limits on model calls, tool calls, time and cost (`BUDGET_*` and the `LLM_*_COST_PER_MILLION` prices in `.env`); when one is reached the rep gets a clear message instead of a hang. Every response includes a `budget` summary, and `GET /conversations/{id}/trace?tenant=plan_a&member_id=M123456` returns the step-by-step trace (model calls with tokens and cost, tool calls, gateway decisions, confirmations). Traces hold no message text and no raw member ids. Files: `state/<tenant>/<member>/<id>.trace.jsonl` and `state/<tenant>/audit/<date>.jsonl`.

Earlier: Step 8 (provider directory). Ask for an in-network dermatologist, cardiologist, etc. near the member: results come only from the member's own plan network, near their home ZIP area (or a ZIP the rep names), and no member data is sent to the vendor. Needs `DIRECTORY_API_KEY` in `.env`.

Earlier: Step 7 (grievances with human confirmation). Ask the assistant to file a grievance: it prepares one and shows a read-back (nothing is filed). Reply "confirm" in your next message to file it, or "cancel". Filing happens exactly once, even if the grievance system answers 503 or you confirm twice. Details in `SPEC.md` (Step 7).

Earlier: Step 6 (plan documents + calculators). The assistant now answers what a member pays (searching only that member's own plan documents, for the right plan year), how much deductible, out-of-pocket and physical therapy visits are left, appeal windows and grievance deadlines (calculated in code, not by the model), and flags terminated coverage. Needs the `kb/` folder (set `PACK_DIR` or `KB_DIR`). Some eval cases disagree with the data; see `SPEC.md` finding F-24.

Earlier: Step 5 (PHI filter). The model can check a caller's last-4 SSN or DOB (`verify_caller`, match/no-match only), refuses to share identifiers, sensitive replies are blocked, and saved conversation files are redacted. Switch with `PHI_FILTER_ENABLED` in `.env`. Postman collection with all APIs (ours and the mock): `docs/emid.postman_collection.json` (fill the token variables locally; do not commit them).

Earlier: Step 4 (tools through a gateway: member, benefits and claims). Start the mock API first (`python mock_services/server.py --port 8090`) and set `TENANT_TOKEN_PLAN_A` / `TENANT_TOKEN_PLAN_B` in `.env`. The model can look up the member on the call, their benefits accumulators and claims. It cannot choose the tenant or member. Plan rules (copays, deductible limits) arrive in Step 6.

Earlier: Step 3 (LangGraph chat endpoint, conversations saved as JSON files, last `HISTORY_TURNS` exchanges sent to the LLM for follow-ups, no filter yet).

Follow-ups: when you send an existing `conversation_id`, the last `HISTORY_TURNS` exchanges (default 8, set in `.env`; 0 turns it off) are sent to the model with your new message. The file always keeps the full conversation; the setting only limits what the model sees.

Conversations: leave `conversation_id` out (or send `null`, `""` or `0`) to start a new conversation. Every response returns a `conversation_id`; send it back to continue that conversation. An unknown id returns 404. Files are saved under `state/{tenant}/{member_id}/{conversation_id}.json` (git-ignored; set `STATE_DIR` to change). Until the PHI filter exists, use made-up text only.

## Setup (Windows PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env      # then edit .env: OPENAI_API_KEY and OPENAI_MODEL
```

## Run

```powershell
uvicorn app.main:app --reload --port 8000
```

## Try it

```powershell
curl.exe -s -X POST http://localhost:8000/conversations -H "Content-Type: application/json" `
  -d '{\"tenant\":\"plan_a\",\"member_id\":\"M749817\",\"message\":\"Hello\"}'
```

Interactive docs: http://localhost:8000/docs

Model used in development: set via `OPENAI_MODEL` in `.env` (to be recorded here).
