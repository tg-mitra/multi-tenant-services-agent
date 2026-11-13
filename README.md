# Member Services Agent

Governed, multi-tenant AI assistant for health-plan member-services reps. Built for the Emids take-home brief (`BRIEF.md`). Full specification, findings and the step-by-step build log: [SPEC.md](SPEC.md). Production design, cloud mapping, threat model and ADRs: [DESIGN.md](DESIGN.md) (diagram: [docs/architecture.svg](docs/architecture.svg)). Decisions, surprises and AI-tool usage: [NOTES.md](NOTES.md).

**Model developed with:** OpenAI-compatible Chat Completions API, model `gpt-6-luna` (configurable — set `OPENAI_MODEL` and, for a non-OpenAI-compatible provider, `OPENAI_BASE_URL`; any model can be used, see `.env.example`).

## What it does

- Answers a rep's questions about one member's benefits, claims, plan rules, deductible/out-of-pocket/physical-therapy status, appeal and grievance deadlines, and in-network providers — scoped to the rep's own tenant and that one member for the whole conversation.
- Files a grievance only after the rep explicitly confirms, exactly once even under retries (Steps 7).
- Minimises PHI to the model and in everything saved to disk (Step 5).
- Enforces per-conversation budgets (steps, cost, time) and records a full trace of every step (Step 9).
- Comes with an evaluation harness over the 40 supplied cases and a regression gate suitable for CI (Step 10).

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Edit `.env`:
- `OPENAI_API_KEY`, `OPENAI_MODEL` (and `OPENAI_BASE_URL` for another provider).
- `TENANT_TOKEN_PLAN_A`, `TENANT_TOKEN_PLAN_B`, `DIRECTORY_API_KEY` — from the pack's `data/README.md`. Never commit real values.
- `PACK_DIR` if the supplied pack (`mock_services/`, `kb/`, `eval/`) lives somewhere other than this repository (the brief asks that it not be committed here — keep it out with `.gitignore` and point `PACK_DIR` at your own copy).

## Run

```powershell
python mock_services/server.py --port 8090      # the supplied mock, in its own terminal
uvicorn app.main:app --reload --port 8000        # the agent
```

Interactive API docs: http://localhost:8000/docs. Helper endpoint to see which member IDs exist (no PHI): set `DEV_ENDPOINTS_ENABLED=true` and open http://localhost:8000/dev/members.

### Try it

```powershell
curl.exe -s -X POST http://localhost:8000/conversations -H "Content-Type: application/json" `
  -d '{\"tenant\":\"plan_a\",\"member_id\":\"M749817\",\"message\":\"What does an in-network specialist visit cost?\",\"conversation_id\":0}'
```

Leave `conversation_id` out (or send `0`/`null`/`""`) to start a new conversation; every response returns a `conversation_id` to send back for follow-ups, including the two-turn grievance confirmation ("file a grievance…" then, in a second message, "confirm"). A Postman collection covering our API and the mock's endpoints can be generated locally (see `SPEC.md`); it is not committed, since it would need real tokens filled in.

## Evaluate

```powershell
python -m evaluation.run_eval                   # all 40 cases; writes reports/, applies the gate (exit 1 on failure)
python -m evaluation.run_eval --ids EV-16,EV-17  # a subset (writes to reports/subset/, never the committed report)
python -m evaluation.gate                        # re-apply the gate to reports/eval_results.json — no model, no network
python -m evaluation.gate --update-baseline      # accept the last run as the new regression baseline
```

Committed results of the last full run: [reports/eval_report.md](reports/eval_report.md) (data: `reports/eval_results.json`, baseline: `reports/baseline.json`). Gate thresholds and the reasoning behind each: `evaluation/thresholds.json`. Four cases whose expected facts in the pack disagree with the pack's own data are scored separately, never tuned to: `evaluation/disputed.json`.

## Test

```powershell
python -m pytest -q
```

No API key, pack, or network needed — all 300+ tests run against fakes and fixtures. CI (`.github/workflows/ci.yml`) runs the tests and the gate on every push; a live run against the real model and pack is opt-in (`RUN_LIVE_EVAL=true`) since the pack and the API key are not in this repository.

## Project layout

```
app/            FastAPI app, LangGraph agent, tool gateway, PHI filter, RAG, calculators, grievance logic
evaluation/     Evaluation harness, scoring, regression gate, report generator
tests/          Unit and integration tests (one file per build step)
docs/           Architecture diagram
reports/        Committed evaluation report, results and baseline
SPEC.md         Full specification: requirements, findings, risks, per-step implementation notes
DESIGN.md       Production design: AWS deployment, cloud mapping, threat model, ADRs
NOTES.md        Decisions, assumptions, surprises, and AI-tool usage
```

`data/`, `kb/`, `eval/`, `tools/`, `mock_services/` are the supplied pack and are git-ignored, per the brief; `state/` (conversation/trace/audit files) is also git-ignored.
