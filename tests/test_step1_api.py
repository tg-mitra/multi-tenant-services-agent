from fastapi.testclient import TestClient

import app.main as main
from app.agent.graph import LLMNotConfigured

client = TestClient(main.app)

VALID = {"tenant": "plan_a", "member_id": "M749817", "message": "Hello"}


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_conversation_returns_reply(monkeypatch):
    async def fake(message: str) -> str:
        return f"echo: {message}"

    monkeypatch.setattr(main, "run_turn", fake)
    r = client.post("/conversations", json=VALID)
    assert r.status_code == 200
    assert r.json() == {"reply": "echo: Hello"}


def test_rejects_unknown_tenant():
    r = client.post("/conversations", json={**VALID, "tenant": "plan_c"})
    assert r.status_code == 422


def test_rejects_bad_member_id():
    for bad in ["749817", "M12", "../etc", "M749817/../x"]:
        r = client.post("/conversations", json={**VALID, "member_id": bad})
        assert r.status_code == 422, bad


def test_rejects_empty_message():
    r = client.post("/conversations", json={**VALID, "message": ""})
    assert r.status_code == 422


def test_missing_key_gives_503(monkeypatch):
    async def boom(message: str) -> str:
        raise LLMNotConfigured("x")

    monkeypatch.setattr(main, "run_turn", boom)
    r = client.post("/conversations", json=VALID)
    assert r.status_code == 503


def test_llm_failure_does_not_leak_details(monkeypatch):
    async def boom(message: str) -> str:
        raise RuntimeError("secret upstream detail")

    monkeypatch.setattr(main, "run_turn", boom)
    r = client.post("/conversations", json=VALID)
    assert r.status_code == 502
    assert "secret" not in r.text
