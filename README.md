# Member Services Agent (POC)

Governed multi-tenant assistant for member-services reps. See `SPEC.md` for the full specification and the step plan.

Current status: Step 3 (LangGraph chat endpoint, conversations saved as JSON files, last `HISTORY_TURNS` exchanges sent to the LLM for follow-ups, no filter yet).

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
