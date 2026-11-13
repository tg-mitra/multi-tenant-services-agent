import json

import pytest
from fastapi.testclient import TestClient

import app.main as main

client = TestClient(main.app)


@pytest.fixture
def pack(tmp_path, monkeypatch):
    """A tiny fake pack with canary PHI and a canary token."""
    data = tmp_path / "mock_services" / "data"
    for tid, name, pc in [("plan_a", "Harborview", "GPPO"), ("plan_b", "Prairie Sky", "CPPO")]:
        (data / tid).mkdir(parents=True)
    (data / "tenants.json").write_text(json.dumps({"tenants": {
        "plan_a": {"name": "Harborview", "api_token": "SECRET_TOKEN_A",
                   "products": [{"product_code": "GPPO", "product": "Gold PPO", "network_id": "N"}]},
        "plan_b": {"name": "Prairie Sky", "api_token": "SECRET_TOKEN_B",
                   "products": [{"product_code": "CPPO", "product": "Choice PPO", "network_id": "M"}]},
    }}), encoding="utf-8")

    def member(mid, pc, plan, term=None):
        return {"member_id": mid, "first_name": "CanaryFirst", "last_name": "CanaryLast", "dob": "1980-03-02",
                "ssn": "123-45-2331", "phone": "555-014-2233", "email": "canary@example.org",
                "address": {"line1": "1 Elm St"}, "product_code": pc, "plan_name": plan,
                "coverage_termination": term}

    (data / "plan_a" / "members.json").write_text(json.dumps([
        member("M111111", "GPPO", "Gold PPO"), member("M222222", "GPPO", "Gold PPO", term="2026-08-31"),
    ]), encoding="utf-8")
    (data / "plan_b" / "members.json").write_text(json.dumps([member("M333333", "CPPO", "Choice PPO")]),
                                                  encoding="utf-8")
    (tmp_path / "eval").mkdir()
    (tmp_path / "eval" / "conversations.json").write_text(json.dumps({"cases": [
        {"id": "EV-01", "tenant": "plan_a", "member_id": "M111111", "turns": [], "expected": {"behaviour": "answer"}},
    ]}), encoding="utf-8")
    monkeypatch.setenv("PACK_DIR", str(tmp_path))
    monkeypatch.setenv("DEV_ENDPOINTS_ENABLED", "true")
    return tmp_path


def test_disabled_by_default_behaves_like_unknown_route(pack, monkeypatch):
    monkeypatch.setenv("DEV_ENDPOINTS_ENABLED", "false")
    r = client.get("/dev/members")
    assert r.status_code == 404 and "M111111" not in r.text


def test_lists_tenants_and_members(pack):
    body = client.get("/dev/members").json()
    assert body["as_of"] == "2026-11-15"
    assert [t["tenant"] for t in body["tenants"]] == ["plan_a", "plan_b"]
    a = body["tenants"][0]
    assert a["client"] == "Harborview" and a["member_count"] == 2
    assert a["products"] == [{"product_code": "GPPO", "product": "Gold PPO"}]
    by_id = {m["member_id"]: m for m in a["members"]}
    assert by_id["M111111"]["coverage_status"] == "ACTIVE"
    assert by_id["M222222"]["coverage_status"] == "TERMINATED"
    assert by_id["M111111"]["eval_cases"] == [{"id": "EV-01", "behaviour": "answer"}]
    assert by_id["M222222"]["eval_cases"] == []


def test_tenant_filter(pack):
    body = client.get("/dev/members", params={"tenant": "plan_b"}).json()
    assert [t["tenant"] for t in body["tenants"]] == ["plan_b"]
    assert [m["member_id"] for m in body["tenants"][0]["members"]] == ["M333333"]


def test_unknown_tenant_filter_is_rejected(pack):
    assert client.get("/dev/members", params={"tenant": "plan_c"}).status_code == 422


def test_response_has_no_phi_or_tokens(pack):
    text = client.get("/dev/members").text
    for secret in ["CanaryFirst", "CanaryLast", "1980-03-02", "123-45-2331", "555-014-2233",
                   "canary@example.org", "Elm St", "SECRET_TOKEN"]:
        assert secret not in text, secret


def test_missing_pack_gives_a_helpful_503(tmp_path, monkeypatch):
    monkeypatch.setenv("PACK_DIR", str(tmp_path / "nowhere"))
    monkeypatch.setenv("DEV_ENDPOINTS_ENABLED", "true")
    r = client.get("/dev/members")
    assert r.status_code == 503 and "PACK_DIR" in r.json()["detail"]


def test_works_without_the_eval_file(pack):
    (pack / "eval" / "conversations.json").unlink()
    body = client.get("/dev/members").json()
    assert body["tenants"][0]["members"][0]["eval_cases"] == []
