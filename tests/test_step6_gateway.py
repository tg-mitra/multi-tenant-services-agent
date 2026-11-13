import asyncio
import dataclasses
import json

import httpx
from test_step6_kb_calc import kb_dir  # noqa: F401  (fixture: small fake KB with the real traps)

from app.config import get_settings
from app.gateway.gateway import SessionContext, ToolGateway
from app.rag.kb import load_kb

CTX_A = SessionContext("plan_a", "M749817", "c1")
CTX_B = SessionContext("plan_b", "M681234", "c2")


def _benefits(product="GPPO", status="ACTIVE", term=None, acc=None, plan_name="Gold PPO"):
    return {
        "member_id": "M749817", "product_code": product, "plan_name": plan_name, "network_id": "N",
        "group_number": "G", "coverage_status": status, "coverage_effective": "2026-01-01",
        "coverage_termination": term,
        "accumulators": acc if acc is not None else {
            "plan_year": 2026, "as_of": "2026-11-15", "deductible_met_individual": 154.48,
            "out_of_pocket_met_individual": 2408.57, "physical_therapy_visits_used": 12},
    }


def _claim(member="M749817", status="DENIED", processed="2026-06-01"):
    return {"claim_id": "C2675159407", "member_id": member, "status": status, "processed_date": processed,
            "service_date": "2026-05-11", "received_date": "2026-05-20", "provider": {"name": "Dr X"},
            "service": {"code": "1", "description": "MRI"}, "billed_amount": 1.0, "allowed_amount": 0.0,
            "plan_paid": 0.0, "member_responsibility": {"deductible": 0.0, "copay": 0.0, "coinsurance": 0.0},
            "denial": {"carc": "CO-50", "description": "Not medically necessary"}}


def _settings(**kw):
    base = dataclasses.replace(get_settings(), tenant_tokens={"plan_a": "ta", "plan_b": "tb"},
                               member_api_base="http://test", rag_top_k=5, as_of_date="2026-11-15")
    return dataclasses.replace(base, **kw)


def _run(routes, fn, kb_path, ctx=CTX_A, settings=None):
    """routes: {path suffix: (status, json)}. Returns (result, recorded requests)."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        for suffix, (status, body) in routes.items():
            if request.url.path.endswith(suffix):
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={})

    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")
        async with ToolGateway(ctx, settings or _settings(), client=client, kb=load_kb(kb_path)) as gw:
            return await fn(gw)

    return asyncio.run(go()), calls


# ---- search_plan_documents ----------------------------------------------------------------------

def test_search_uses_the_members_product_and_year(kb_dir):
    r = {"/benefits": (200, _benefits("SHMO", plan_name="Silver HMO"))}
    res, _ = _run(r, lambda gw: gw.search_plan_documents("specialist visit"), kb_dir)
    text = json.dumps(res)
    assert "$70 copay" in text and "$40 copay" not in text and res["plan_year"] == 2026
    res27, _ = _run({"/benefits": (200, _benefits("GPPO"))}, lambda gw: gw.search_plan_documents("specialist visit", 2027), kb_dir)
    assert "$50 copay" in json.dumps(res27) and res27["plan_year"] == 2027


def test_search_never_returns_the_other_tenants_documents(kb_dir):
    r = {"/benefits": (200, _benefits("GPPO"))}
    res, _ = _run(r, lambda gw: gw.search_plan_documents("virtual behavioral health session"), kb_dir)
    text = json.dumps(res)
    assert "$0 copay" not in text and "Prairie" not in text and "$25 copay" in text


def test_search_with_no_match_says_so(kb_dir):
    r = {"/benefits": (200, _benefits("GPPO"))}
    res, _ = _run(r, lambda gw: gw.search_plan_documents("zebra unicorn"), kb_dir)
    assert res["passages"] == [] and "not available" in res["note"]


def test_search_flags_terminated_coverage(kb_dir):
    r = {"/benefits": (200, _benefits("GPPO", status="TERMINATED", term="2026-08-31"))}
    res, _ = _run(r, lambda gw: gw.search_plan_documents("urgent care"), kb_dir)
    assert res["coverage_status"] == "TERMINATED" and "2026-08-31" in res["coverage_note"]


def test_search_validates_input_without_calling_the_api(kb_dir):
    for kwargs in [{"query": ""}, {"query": "x" * 201}, {"query": "ok", "plan_year": "2026"},
                   {"query": "ok", "plan_year": 1999}, {"query": "ok", "plan_year": True}]:
        res, calls = _run({}, lambda gw, k=kwargs: gw.search_plan_documents(**k), kb_dir)
        assert calls == [] and res["error"] == "invalid_argument", kwargs


def test_benefits_are_fetched_once_per_message(kb_dir):
    async def several(gw):
        await gw.search_plan_documents("specialist")
        await gw.get_plan_status()
        await gw.get_benefits()

    _, calls = _run({"/benefits": (200, _benefits())}, several, kb_dir)
    assert len(calls) == 1


# ---- get_plan_status ------------------------------------------------------------------------------

def test_plan_status_numbers_come_from_code(kb_dir):
    res, _ = _run({"/benefits": (200, _benefits())}, lambda gw: gw.get_plan_status(), kb_dir)
    assert res["deductible"] == {"limit": "$1,000.00", "met_so_far": "$154.48", "remaining": "$845.52"}
    assert res["out_of_pocket"] == {"limit": "$4,000.00", "met_so_far": "$2,408.57", "remaining": "$1,591.43"}
    assert res["physical_therapy_visits"] == {"limit": 30, "used": 12, "remaining": 18}
    assert res["family_limits_for_reference"] == {"deductible": "$2,000.00", "out_of_pocket": "$8,000.00"}
    assert res["product"] == "Gold PPO" and res["plan_year"] == 2026


def test_plan_status_uses_the_members_product(kb_dir):
    res, _ = _run({"/benefits": (200, _benefits("SHMO", plan_name="Silver HMO"))}, lambda gw: gw.get_plan_status(), kb_dir)
    assert res["deductible"]["limit"] == "$2,500.00" and res["physical_therapy_visits"]["limit"] == 20


def test_plan_status_never_negative_and_handles_missing_data(kb_dir):
    over = {"plan_year": 2026, "as_of": "x", "deductible_met_individual": 1500.0,
            "out_of_pocket_met_individual": 9000.0, "physical_therapy_visits_used": 40}
    res, _ = _run({"/benefits": (200, _benefits(acc=over))}, lambda gw: gw.get_plan_status(), kb_dir)
    assert res["deductible"]["remaining"] == "$0.00" and res["physical_therapy_visits"]["remaining"] == 0
    none, _ = _run({"/benefits": (200, _benefits(acc={}))}, lambda gw: gw.get_plan_status(), kb_dir)
    assert none["error"] == "not_available"
    no_doc, _ = _run({"/benefits": (200, _benefits(acc={**over, "plan_year": 2030}))}, lambda gw: gw.get_plan_status(), kb_dir)
    assert no_doc["error"] == "not_available"


def test_plan_status_terminated_note(kb_dir):
    r = {"/benefits": (200, _benefits(status="TERMINATED", term="2026-08-31"))}
    res, _ = _run(r, lambda gw: gw.get_plan_status(), kb_dir)
    assert "2026-08-31" in res["coverage_note"]


# ---- check_appeal_window ---------------------------------------------------------------------------

def test_appeal_window_within_and_past(kb_dir):
    routes = {"/benefits": (200, _benefits()), "/claims/C2675159407": (200, _claim(processed="2026-06-01"))}
    res, _ = _run(routes, lambda gw: gw.check_appeal_window("C2675159407"), kb_dir)
    assert res["appeal_deadline"] == "2026-11-28" and res["within_appeal_window"] is True and res["appeal_window_days"] == 180
    routes["/claims/C2675159407"] = (200, _claim(processed="2026-05-14"))
    res, _ = _run(routes, lambda gw: gw.check_appeal_window("C2675159407"), kb_dir)
    assert res["appeal_deadline"] == "2026-11-10" and res["within_appeal_window"] is False and res["days_overdue"] == 5


def test_appeal_window_only_for_denied_claims_and_own_claims(kb_dir):
    paid = {"/benefits": (200, _benefits()), "/claims/C2675159407": (200, _claim(status="PAID"))}
    res, _ = _run(paid, lambda gw: gw.check_appeal_window("C2675159407"), kb_dir)
    assert res["appeal_applicable"] is False
    other = {"/benefits": (200, _benefits()), "/claims/C2675159407": (200, _claim(member="M111111"))}
    res, _ = _run(other, lambda gw: gw.check_appeal_window("C2675159407"), kb_dir)
    assert res["error"] == "not_found" and "M111111" not in json.dumps(res)
    res, calls = _run({}, lambda gw: gw.check_appeal_window("../x"), kb_dir)
    assert res["error"] == "invalid_argument" and calls == []


# ---- grievances --------------------------------------------------------------------------------------

def _grievances(*items):
    return {"/grievances": (200, {"data": list(items)})}


def _g(gid, received, status="OPEN", member="M749817", resolved=None):
    g = {"grievance_id": gid, "member_id": member, "category": "access", "status": status,
         "received_date": received, "expedite_requested": False, "description": "PRIVATE free text"}
    if resolved:
        g["resolved_date"] = resolved
    return g


def test_grievance_deadline_is_tenant_specific(kb_dir):
    a = {**_grievances(_g("G26-600230", "2026-10-09")), "/benefits": (200, _benefits("GPPO"))}
    res, _ = _run(a, lambda gw: gw.check_grievance_deadline("G26-600230"), kb_dir)
    assert res["resolution_deadline"] == "2026-11-08" and res["past_resolution_deadline"] is True
    assert res["resolution_timeframe_days"] == 30
    b = {**_grievances(_g("G26-600260", "2026-10-20", member="M681234")), "/benefits": (200, _benefits("CPPO", plan_name="Choice PPO"))}
    res, _ = _run(b, lambda gw: gw.check_grievance_deadline("G26-600260"), kb_dir, ctx=CTX_B)
    assert res["resolution_deadline"] == "2026-12-04" and res["past_resolution_deadline"] is False
    assert res["resolution_timeframe_days"] == 45


def test_resolved_grievance_is_judged_by_its_resolved_date(kb_dir):
    on_time = {**_grievances(_g("G26-600366", "2026-08-06", "RESOLVED", resolved="2026-08-16")), "/benefits": (200, _benefits())}
    res, _ = _run(on_time, lambda gw: gw.check_grievance_deadline("G26-600366"), kb_dir)
    assert res["past_resolution_deadline"] is False
    late = {**_grievances(_g("G26-600367", "2026-07-01", "RESOLVED", resolved="2026-09-01")), "/benefits": (200, _benefits())}
    res, _ = _run(late, lambda gw: gw.check_grievance_deadline("G26-600367"), kb_dir)
    assert res["past_resolution_deadline"] is True


def test_other_members_grievance_is_not_found_and_ids_are_validated(kb_dir):
    other = {**_grievances(_g("G26-600500", "2026-10-09", member="M999999")), "/benefits": (200, _benefits())}
    res, _ = _run(other, lambda gw: gw.check_grievance_deadline("G26-600500"), kb_dir)
    assert res["error"] == "not_found"
    for bad in ["600230", "G26-60023", "g26-600230", "../x"]:
        res, calls = _run({}, lambda gw, b=bad: gw.check_grievance_deadline(b), kb_dir)
        assert res["error"] == "invalid_argument" and calls == [], bad


def test_list_grievances_omits_free_text_and_other_members(kb_dir):
    routes = _grievances(_g("G26-600230", "2026-10-09"), _g("G26-600500", "2026-10-09", member="M999999"))
    res, _ = _run(routes, lambda gw: gw.list_grievances(), kb_dir)
    assert [g["grievance_id"] for g in res["grievances"]] == ["G26-600230"]
    assert "PRIVATE" not in json.dumps(res) and "description" not in json.dumps(res)
