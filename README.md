# Member Services Agent (POC)

Governed multi-tenant assistant for member-services reps. See `SPEC.md` for the full specification and the step plan.

Current status: Step 1 (basic LangGraph chat endpoint, no filter, no storage).

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
