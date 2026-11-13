import asyncio
import dataclasses
import json

import httpx
from fastapi.testclient import TestClient

import app.main as main
import app.turns as turns
from app.config import get_settings
from app.gateway.gateway import SessionContext, ToolGateway
from app.gateway.phi import BLOCKED_REPLY, detect, guard_output, redact_text, sanitize_result

client = TestClient(main.app)

CANARY_SSN = "123-45-2331"
CANARY_DOB = "1980-03-02"
CANARY_PHONE = "555-014-2233"
CANARY_EMAIL = "canary@example.org"
MEMBER = {
    "member_id": "M749817", "first_name": "Ann", "last_name": "Canary", "dob": CANARY_DOB,
    "ssn": CANARY_SSN, "phone": CANARY_PHONE, "email": CANARY_EMAIL,
    "address": {"line1": "1 Elm St", "zip": "12345"},
    "product_code": "GPPO", "plan_name": "Gold PPO", "network_id": "HVH-PPO", "group_number": "G1",
    "coverage_effective": "2026-01-01", "coverage_termination": None,
}
CTX = SessionContext("plan_a", "M749817", "c1")


# ---- patterns ---------------------------------------------------------------------

def test_redacts_ssn_phone_email():
    text = "SSN 123-45-6789, also 123456789 and 123 45 6789; call (555) 014-2233 or 555.014.2233; a@b.com"
    out, counts = redact_text(text)
    for raw in ["123-45-6789", "123456789", "123 45 6789", "014-2233", "555.014.2233", "a@b.com"]:
        assert raw not in out, raw
    assert counts["ssn"] == 3 and counts["email"] == 1 and counts["phone"] == 2


def test_redacts_dob_only_with_a_birth_keyword():
    out, counts = redact_text("DOB 1980-03-02, born on 03/02/1980, date of birth: 1980-03-02")
    assert "1980" not in out and counts["dob"] == 3
    plain = "Service date 2026-08-14, processed 2026-09-01"
    assert redact_text(plain)[0] == plain


def test_redacts_last4_after_ssn_keyword():
    out, counts = redact_text("Caller gave the last four digits of their SSN as 2331.")
    assert "2331" not in out and "[SSN_LAST4]" in out and counts["ssn_last4"] == 1
    out, _ = redact_text("last 4 of the social security number is 2331")
    assert "2331" not in out


def test_no_false_positives_on_ids_dates_and_amounts():
    benign = (
        "Claim C2639555547 (NPI 1234567890) on 2026-08-14 paid $1,429.52; billed 12345.67; "
        "grievance G26-700101; member M749817; plan year 2026; limit 30 visits."
    )
    assert redact_text(benign) == (benign, {})
    assert detect(benign) == []


def test_skip_keeps_dob_and_last4_for_the_model_message():
    msg = "SSN 123-45-6789 phone 555-014-2233 last four 2331 DOB 1980-03-02"
    out, counts = redact_text(msg, skip={"dob", "ssn_last4"})
    assert "123-45-6789" not in out and "555-014-2233" not in out
    assert "2331" in out and "1980-03-02" in out
    assert "dob" not in counts and "ssn_last4" not in counts


# ---- output guard -------------------------------------------------------------------

def test_guard_blocks_sensitive_values():
    for bad in ["Their SSN is 123-45-6789", "Call 555-014-2233", "Email a@b.com", "DOB: 1980-03-02"]:
        reply, kinds = guard_output(bad)
        assert reply == BLOCKED_REPLY and kinds, bad


def test_guard_passes_normal_answers():
    ok = "Claim C2639555547 is PAID. The member owes $40.00 for plan year 2026."
    assert guard_output(ok) == (ok, [])


def test_sanitize_result_walks_nested_structures():
    result = {"a": ["call 555-014-2233"], "b": {"c": "ssn 123-45-6789", "n": 5}}
    clean = sanitize_result(result)
    assert "555-014-2233" not in json.dumps(clean) and "123-45-6789" not in json.dumps(clean)
    assert clean["b"]["n"] == 5


# ---- verify_caller ---------------------------------------------------------------------

def _settings(**overrides):
    base = dataclasses.replace(
        get_settings(), tenant_tokens={"plan_a": "tok_a", "plan_b": "tok_b"},
        member_api_base="http://test", max_verify_attempts=3,
    )
    return dataclasses.replace(base, **overrides)


def _run(handler, fn, settings=None):
    calls = []

    def wrapped(request):
        calls.append(request)
        return handler(request)

    async def go():
        c = httpx.AsyncClient(transport=httpx.MockTransport(wrapped), base_url="http://test")
        async with ToolGateway(CTX, settings or _settings(), client=c) as gw:
            return await fn(gw)

    return asyncio.run(go()), calls


def _member(r):
    return httpx.Response(200, json=MEMBER)


def test_verify_match_and_no_match_return_only_a_boolean():
    ok, _ = _run(_member, lambda gw: gw.verify_caller(ssn_last4="2331"))
    assert ok == {"match": True, "checked": ["ssn_last4"]}
    no, _ = _run(_member, lambda gw: gw.verify_caller(ssn_last4="9999"))
    assert no == {"match": False, "checked": ["ssn_last4"]}
    both_one_wrong, _ = _run(_member, lambda gw: gw.verify_caller(ssn_last4="2331", dob="1999-01-01"))
    assert both_one_wrong["match"] is False
    both, _ = _run(_member, lambda gw: gw.verify_caller(ssn_last4="2331", dob=CANARY_DOB))
    assert both["match"] is True and both["checked"] == ["ssn_last4", "dob"]


def test_verify_never_leaks_stored_values():
    for kwargs in [{"ssn_last4": "2331"}, {"ssn_last4": "0000"}, {"dob": CANARY_DOB}, {"dob": "2000-01-01"}]:
        result, _ = _run(_member, lambda gw, k=kwargs: gw.verify_caller(**k))
        text = json.dumps(result)
        assert set(result) == {"match", "checked"}
        for secret in [CANARY_SSN, "123-45", CANARY_DOB, CANARY_PHONE, CANARY_EMAIL, "Canary"]:
            assert secret not in text


def test_verify_validates_input_without_calling_the_api():
    for kwargs in [{}, {"ssn_last4": "12"}, {"ssn_last4": "abcd"}, {"dob": "03/02/1980"}, {"ssn_last4": "123456789"}]:
        result, calls = _run(_member, lambda gw, k=kwargs: gw.verify_caller(**k))
        assert calls == [] and result["error"] == "invalid_argument", kwargs


def test_verify_attempts_are_capped_per_turn():
    async def many(gw):
        return [await gw.verify_caller(ssn_last4="0000") for _ in range(5)]

    results, calls = _run(_member, many)
    assert [r.get("match") for r in results[:3]] == [False, False, False]
    assert results[3]["error"] == "too_many_attempts" and results[4]["error"] == "too_many_attempts"
    assert len(calls) == 3


def test_verify_member_not_found():
    result, _ = _run(lambda r: httpx.Response(404, json={}), lambda gw: gw.verify_caller(ssn_last4="2331"))
    assert result["error"] == "not_found"


def test_member_tools_never_return_the_canaries():
    async def all_tools(gw):
        return [await gw.get_member_summary()]

    results, _ = _run(_member, all_tools)
    text = json.dumps(results)
    for secret in [CANARY_SSN, CANARY_DOB, CANARY_PHONE, CANARY_EMAIL, "Canary", "Elm St", "Ann"]:
        assert secret not in text


def test_tool_result_free_text_is_scrubbed_and_switchable():
    claim = {
        "claim_id": "C2639555547", "member_id": "M749817", "status": "DENIED",
        "denial": {"carc": "CO-16", "description": f"Call {CANARY_PHONE} re SSN {CANARY_SSN}"},
        "service": {"code": "1", "description": "x"}, "provider": {"name": "Dr X"},
    }
    handler = lambda r: httpx.Response(200, json=claim)  # noqa: E731
    on, _ = _run(handler, lambda gw: gw.get_claim("C2639555547"))
    assert CANARY_PHONE not in json.dumps(on) and CANARY_SSN not in json.dumps(on)
    off, _ = _run(handler, lambda gw: gw.get_claim("C2639555547"), settings=_settings(phi_filter_enabled=False))
    assert CANARY_PHONE in json.dumps(off)


# ---- through the API -------------------------------------------------------------------

BODY = {"tenant": "plan_a", "member_id": "M749817", "message": "hi"}


def _state_text(state_dir) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in state_dir.rglob("*.json"))


def test_reply_with_sensitive_value_is_blocked_and_not_stored(monkeypatch, isolated_state_dir):
    async def fake(message, history=None, gateway=None, extra_system=None):
        return f"The SSN is {CANARY_SSN}"

    monkeypatch.setattr(turns, "run_turn", fake)
    r = client.post("/conversations", json=BODY)
    assert r.status_code == 200 and r.json()["reply"] == BLOCKED_REPLY
    assert CANARY_SSN not in r.text and CANARY_SSN not in _state_text(isolated_state_dir)


def test_rep_typed_identifiers_are_redacted_for_model_and_file(monkeypatch, isolated_state_dir):
    seen = {}

    async def fake(message, history=None, gateway=None, extra_system=None):
        seen["message"] = message
        return "ok"

    monkeypatch.setattr(turns, "run_turn", fake)
    msg = (f"Caller SSN {CANARY_SSN} phone {CANARY_PHONE} email {CANARY_EMAIL}; "
           f"last four digits of the SSN are 2331; DOB {CANARY_DOB}")
    client.post("/conversations", json={**BODY, "message": msg})
    # model: full SSN/phone/email removed, but last-4 and DOB stay so verify_caller can use them
    for gone in [CANARY_SSN, CANARY_PHONE, CANARY_EMAIL]:
        assert gone not in seen["message"]
    assert "2331" in seen["message"] and CANARY_DOB in seen["message"]
    # file: nothing sensitive at all
    stored = _state_text(isolated_state_dir)
    for gone in [CANARY_SSN, CANARY_PHONE, CANARY_EMAIL, CANARY_DOB, "2331"]:
        assert gone not in stored, gone
    assert "[SSN_LAST4]" in stored and "[DOB]" in stored


def test_filter_off_leaves_text_untouched(monkeypatch, isolated_state_dir):
    monkeypatch.setenv("PHI_FILTER_ENABLED", "false")

    async def fake(message, history=None, gateway=None, extra_system=None):
        return f"The SSN is {CANARY_SSN}"

    monkeypatch.setattr(turns, "run_turn", fake)
    r = client.post("/conversations", json={**BODY, "message": f"phone {CANARY_PHONE}"})
    assert CANARY_SSN in r.json()["reply"]
    assert CANARY_PHONE in _state_text(isolated_state_dir)


def test_follow_up_history_comes_from_the_redacted_file(monkeypatch):
    seen = []

    async def fake(message, history=None, gateway=None, extra_system=None):
        seen.append(list(history or []))
        return "ok"

    monkeypatch.setattr(turns, "run_turn", fake)
    cid = client.post("/conversations", json={**BODY, "message": f"SSN {CANARY_SSN}"}).json()["conversation_id"]
    client.post("/conversations", json={**BODY, "message": "next", "conversation_id": cid})
    assert CANARY_SSN not in " ".join(m.content for m in seen[1])
