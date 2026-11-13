import json

from fastapi.testclient import TestClient

import app.main as main

client = TestClient(main.app)

VALID = {"tenant": "plan_a", "member_id": "M749817", "message": "Hello"}


def _fake_llm(monkeypatch, reply="ok"):
    async def fake(message: str, history=None) -> str:
        return f"{reply}: {message}"

    monkeypatch.setattr(main, "run_turn", fake)


def _post(**overrides):
    return client.post("/conversations", json={**VALID, **overrides})


# ---- conversation_id rules ------------------------------------------------

def test_new_conversation_when_id_missing_null_blank_or_zero(monkeypatch):
    _fake_llm(monkeypatch)
    ids = set()
    for value in [None, "", "  ", "0", 0]:
        r = _post(conversation_id=value)
        assert r.status_code == 200, value
        cid = r.json()["conversation_id"]
        assert len(cid) == 32
        ids.add(cid)
    r = client.post("/conversations", json=VALID)  # field omitted
    ids.add(r.json()["conversation_id"])
    assert len(ids) == 6  # every call started its own conversation


def test_supplied_id_is_returned_unchanged(monkeypatch):
    _fake_llm(monkeypatch)
    cid = _post().json()["conversation_id"]
    r = _post(conversation_id=cid, message="again")
    assert r.status_code == 200
    assert r.json()["conversation_id"] == cid


def test_unsafe_conversation_id_rejected(monkeypatch):
    _fake_llm(monkeypatch)
    for bad in ["../x", "a/b", "a b", "x" * 65, "..\\y"]:
        assert _post(conversation_id=bad).status_code == 422, bad


def test_unknown_conversation_id_is_404_and_creates_nothing(monkeypatch, isolated_state_dir):
    _fake_llm(monkeypatch)
    r = _post(conversation_id="1234")
    assert r.status_code == 404
    assert r.json() == {"detail": "Conversation not found", "conversation_id": None}
    assert not isolated_state_dir.exists()


# ---- file storage ---------------------------------------------------------

def test_file_created_after_first_reply(monkeypatch, isolated_state_dir):
    _fake_llm(monkeypatch, "answer")
    cid = _post().json()["conversation_id"]
    path = isolated_state_dir / "plan_a" / "M749817" / f"{cid}.json"
    assert path.is_file()
    rec = json.loads(path.read_text(encoding="utf-8"))
    assert rec["conversation_id"] == cid
    assert rec["tenant"] == "plan_a" and rec["member_id"] == "M749817"
    assert len(rec["turns"]) == 1
    turn = rec["turns"][0]
    assert turn["rep"] == "Hello" and turn["assistant"] == "answer: Hello"
    assert turn["turn"] == 1 and "latency_ms" in turn and "ts" in turn


def test_follow_up_appends_to_same_file(monkeypatch, isolated_state_dir):
    _fake_llm(monkeypatch)
    cid = _post().json()["conversation_id"]
    _post(conversation_id=cid, message="second")
    files = list((isolated_state_dir / "plan_a" / "M749817").glob("*.json"))
    assert len(files) == 1
    rec = json.loads(files[0].read_text(encoding="utf-8"))
    assert [t["rep"] for t in rec["turns"]] == ["Hello", "second"]
    assert [t["turn"] for t in rec["turns"]] == [1, 2]


def test_two_new_conversations_make_two_files(monkeypatch, isolated_state_dir):
    _fake_llm(monkeypatch)
    _post()
    _post()
    assert len(list((isolated_state_dir / "plan_a" / "M749817").glob("*.json"))) == 2


def test_no_leftover_temp_files(monkeypatch, isolated_state_dir):
    _fake_llm(monkeypatch)
    _post()
    assert not list(isolated_state_dir.rglob(".tmp-*"))


# ---- isolation ------------------------------------------------------------

def test_other_member_or_tenant_cannot_use_the_id(monkeypatch):
    _fake_llm(monkeypatch)
    cid = _post().json()["conversation_id"]
    assert _post(conversation_id=cid, member_id="M111111").status_code == 404
    assert _post(conversation_id=cid, tenant="plan_b").status_code == 404
    assert _post(conversation_id=cid).status_code == 200


# ---- failures -------------------------------------------------------------

def test_llm_failure_on_new_conversation_creates_no_file(monkeypatch, isolated_state_dir):
    async def boom(message: str, history=None) -> str:
        raise RuntimeError("secret upstream detail")

    monkeypatch.setattr(main, "run_turn", boom)
    r = _post()
    assert r.status_code == 502
    assert r.json() == {"detail": "LLM request failed", "conversation_id": None}
    assert "secret" not in r.text
    assert not isolated_state_dir.exists()


def test_llm_failure_on_existing_conversation_keeps_id_and_file(monkeypatch, isolated_state_dir):
    _fake_llm(monkeypatch)
    cid = _post().json()["conversation_id"]

    async def boom(message: str, history=None) -> str:
        raise RuntimeError("x")

    monkeypatch.setattr(main, "run_turn", boom)
    r = _post(conversation_id=cid, message="second")
    assert r.status_code == 502
    assert r.json()["conversation_id"] == cid
    rec = json.loads(
        (isolated_state_dir / "plan_a" / "M749817" / f"{cid}.json").read_text(encoding="utf-8")
    )
    assert len(rec["turns"]) == 1  # failed turn not recorded
