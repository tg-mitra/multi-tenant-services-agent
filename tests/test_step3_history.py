from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage

import app.main as main
import app.turns as turns
from app.agent.history import turns_to_messages

client = TestClient(main.app)

VALID = {"tenant": "plan_a", "member_id": "M749817", "message": "m1"}


def _turns(n):
    return [{"turn": i, "rep": f"rep{i}", "assistant": f"ai{i}"} for i in range(1, n + 1)]


# ---- history builder ------------------------------------------------------

def test_window_keeps_last_n_exchanges_oldest_first():
    msgs = turns_to_messages(_turns(10), 8)
    assert len(msgs) == 16
    assert msgs[0].content == "rep3" and msgs[-1].content == "ai10"
    assert isinstance(msgs[0], HumanMessage) and isinstance(msgs[1], AIMessage)


def test_fewer_turns_than_window_returns_all():
    assert len(turns_to_messages(_turns(3), 8)) == 6


def test_zero_or_negative_limit_returns_nothing():
    assert turns_to_messages(_turns(5), 0) == []
    assert turns_to_messages(_turns(5), -1) == []


def test_empty_turns():
    assert turns_to_messages([], 8) == []


# ---- through the API ------------------------------------------------------

def _capture(monkeypatch):
    seen = []

    async def fake(message: str, history=None, gateway=None, extra_system=None) -> str:
        seen.append((message, list(history or [])))
        return f"reply to {message}"

    monkeypatch.setattr(turns, "run_turn", fake)
    return seen


def test_new_conversation_sends_no_history(monkeypatch):
    seen = _capture(monkeypatch)
    client.post("/conversations", json=VALID)
    assert seen[0][1] == []


def test_follow_up_sends_earlier_exchanges(monkeypatch):
    seen = _capture(monkeypatch)
    cid = client.post("/conversations", json=VALID).json()["conversation_id"]
    client.post("/conversations", json={**VALID, "message": "m2", "conversation_id": cid})
    message, history = seen[1]
    assert message == "m2"
    assert [m.content for m in history] == ["m1", "reply to m1"]


def test_only_last_n_exchanges_are_sent(monkeypatch):
    monkeypatch.setenv("HISTORY_TURNS", "2")
    seen = _capture(monkeypatch)
    cid = client.post("/conversations", json={**VALID, "message": "m1"}).json()["conversation_id"]
    for i in range(2, 6):
        client.post("/conversations", json={**VALID, "message": f"m{i}", "conversation_id": cid})
    message, history = seen[-1]  # the 5th message sees m3 and m4 only
    assert message == "m5"
    assert [m.content for m in history] == ["m3", "reply to m3", "m4", "reply to m4"]


def test_history_turns_zero_disables_history(monkeypatch):
    monkeypatch.setenv("HISTORY_TURNS", "0")
    seen = _capture(monkeypatch)
    cid = client.post("/conversations", json=VALID).json()["conversation_id"]
    client.post("/conversations", json={**VALID, "message": "m2", "conversation_id": cid})
    assert seen[1][1] == []


def test_full_history_still_saved_in_file(monkeypatch, isolated_state_dir):
    import json

    monkeypatch.setenv("HISTORY_TURNS", "1")
    _capture(monkeypatch)
    cid = client.post("/conversations", json=VALID).json()["conversation_id"]
    for i in range(2, 5):
        client.post("/conversations", json={**VALID, "message": f"m{i}", "conversation_id": cid})
    rec = json.loads(
        (isolated_state_dir / "plan_a" / "M749817" / f"{cid}.json").read_text(encoding="utf-8")
    )
    assert len(rec["turns"]) == 4  # the window only limits what the LLM sees
