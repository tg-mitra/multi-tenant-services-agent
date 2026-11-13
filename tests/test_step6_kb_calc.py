from decimal import Decimal

import pytest

from app.calc import deadline_status, fmt_money, money_line, remaining, to_money
from app.rag.kb import KbLookupError, load_kb


def _doc(tenant, year, product_code, product, body):
    return (f"---\ntenant: {tenant}\nplan_year: {year}\neffective_date: {year}-01-01\n"
            f"product_code: {product_code}\nproduct: {product}\n---\n\n{body}\n")


def _benefits(title, ded, oop, pt, specialist, behavioral="$25 copay"):
    return (f"# {title}\n\n| Benefit | In-network member cost | Out-of-network member cost |\n|---|---|---|\n"
            f"| Deductible (individual / family) | {ded} | $2,000 / $4,000 |\n"
            f"| Out-of-pocket maximum (individual / family) | {oop} | $8,000 / $16,000 |\n"
            f"| Specialist visit | {specialist} | 40% coinsurance after deductible |\n"
            f"| Behavioral health outpatient visit (in person or virtual) | {behavioral} | 40% coinsurance |\n"
            f"| Physical therapy visit limit | {pt} visits per plan year | Combined with in-network visits |\n\n"
            "## Limits\n\n- Advanced imaging requires prior authorization.\n")


def _guide(name, grievance_days):
    return (f"# {name}: Member services guide\n\n| Topic | Rule |\n|---|---|\n"
            f"| Grievance resolution timeframe | {grievance_days} calendar days from the date the grievance is received |\n"
            "| Claim appeal filing window | 180 calendar days from the date of the denial notice. |\n")


@pytest.fixture
def kb_dir(tmp_path):
    a, b = tmp_path / "plan_a", tmp_path / "plan_b"
    a.mkdir(); b.mkdir()
    (a / "gold_2026.md").write_text(_doc("plan_a", 2026, "GPPO", "Gold PPO",
        _benefits("Gold PPO 2026", "$1,000 / $2,000", "$4,000 / $8,000", 30, "$40 copay")), encoding="utf-8")
    (a / "gold_2027.md").write_text(_doc("plan_a", 2027, "GPPO", "Gold PPO",
        _benefits("Gold PPO 2027", "$1,500 / $3,000", "$4,500 / $9,000", 24, "$50 copay")), encoding="utf-8")
    (a / "silver_2026.md").write_text(_doc("plan_a", 2026, "SHMO", "Silver HMO",
        _benefits("Silver HMO 2026", "$2,500 / $5,000", "$8,000 / $16,000", 20, "$70 copay")), encoding="utf-8")
    (a / "member_services_guide.md").write_text(_doc("plan_a", 2026, "ALL", "All medical plans",
        _guide("Harborview", 30)), encoding="utf-8")
    # MISFILED: sits in plan_a/ but belongs to plan_b
    (a / "virtual_care_2026.md").write_text(_doc("plan_b", 2026, "ALL", "All medical plans",
        "# Prairie Sky Virtual Care Benefit\n\n| Service | Member cost |\n|---|---|\n"
        "| Virtual behavioral health session | $0 copay, up to 20 sessions per plan year |\n"), encoding="utf-8")
    (b / "choice_2026.md").write_text(_doc("plan_b", 2026, "CPPO", "Choice PPO",
        _benefits("Choice PPO 2026", "$750 / $1,500", "$3,500 / $7,000", 24, "$40 copay", "see Virtual Care Benefit")),
        encoding="utf-8")
    (b / "member_services_guide.md").write_text(_doc("plan_b", 2026, "ALL", "All medical plans",
        _guide("Prairie Sky", 45)), encoding="utf-8")
    (b / "bad.md").write_text("# no front matter\n", encoding="utf-8")
    return tmp_path


# ---- loading / chunking --------------------------------------------------------------

def test_table_rows_become_precise_chunks(kb_dir):
    kb = load_kb(kb_dir)
    rows = [c for c in kb.chunks if c.doc_id == "gold_2026.md" and c.label == "Specialist visit"]
    assert len(rows) == 1 and rows[0].kind == "row"
    assert "In-network member cost: $40 copay" in rows[0].text and "Gold PPO" in rows[0].text


def test_document_without_front_matter_is_skipped_with_a_warning(kb_dir):
    kb = load_kb(kb_dir)
    assert not any(c.doc_id == "bad.md" for c in kb.chunks)
    assert any("bad.md" in w for w in kb.warnings)


def test_misfiled_document_is_flagged_and_used_by_front_matter_tenant(kb_dir):
    kb = load_kb(kb_dir)
    assert any("virtual_care_2026.md" in w and "plan_b" in w for w in kb.warnings)
    assert {c.tenant for c in kb.chunks if c.doc_id == "virtual_care_2026.md"} == {"plan_b"}


# ---- isolation: tenant, product, year ------------------------------------------------------

def test_plan_a_never_sees_plan_b_content(kb_dir):
    kb = load_kb(kb_dir)
    got = kb.search("plan_a", "GPPO", 2026, "virtual behavioral health session copay", k=20)
    assert got and all(c.tenant == "plan_a" for c in got)
    assert not any("$0 copay" in c.text or "Prairie" in c.text for c in got)
    assert any("$25 copay" in c.text for c in got)  # the real plan_a answer


def test_plan_b_does_see_its_own_misfiled_document(kb_dir):
    kb = load_kb(kb_dir)
    got = kb.search("plan_b", "CPPO", 2026, "virtual behavioral health session", k=5)
    assert any(c.doc_id == "virtual_care_2026.md" for c in got)


def test_candidates_never_cross_tenants(kb_dir):
    kb = load_kb(kb_dir)
    for tenant, product in [("plan_a", "GPPO"), ("plan_a", "SHMO"), ("plan_b", "CPPO")]:
        for year in (2026, 2027):
            assert all(kb.chunks[i].tenant == tenant for i in kb.candidates(tenant, product, year))


def test_product_filter(kb_dir):
    kb = load_kb(kb_dir)
    got = kb.search("plan_a", "SHMO", 2026, "specialist visit", k=10)
    assert any("$70 copay" in c.text for c in got) and not any("$40 copay" in c.text for c in got)


def test_year_filter_uses_the_right_document(kb_dir):
    kb = load_kb(kb_dir)
    y26 = kb.search("plan_a", "GPPO", 2026, "specialist visit", k=1)[0]
    y27 = kb.search("plan_a", "GPPO", 2027, "specialist visit", k=1)[0]
    assert "$40 copay" in y26.text and y26.plan_year == 2026
    assert "$50 copay" in y27.text and y27.plan_year == 2027


def test_all_product_docs_fall_back_to_the_latest_earlier_year(kb_dir):
    kb = load_kb(kb_dir)
    got = kb.search("plan_a", "GPPO", 2027, "grievance resolution timeframe", k=3)
    assert got and got[0].doc_id == "member_services_guide.md" and got[0].plan_year == 2026


def test_product_docs_need_the_exact_year(kb_dir):
    kb = load_kb(kb_dir)
    assert kb.search("plan_a", "GPPO", 2028, "specialist visit", k=5) == [] or \
        all(c.product_code == "ALL" for c in kb.search("plan_a", "GPPO", 2028, "specialist visit", k=5))


def test_synonyms_and_empty_query(kb_dir):
    kb = load_kb(kb_dir)
    assert kb.search("plan_a", "GPPO", 2026, "   ") == []
    got = kb.search("plan_a", "GPPO", 2026, "PT limit", k=2)
    assert any("Physical therapy visit limit" in c.text for c in got)


# ---- parsed numbers ---------------------------------------------------------------------------------

def test_plan_limits(kb_dir):
    kb = load_kb(kb_dir)
    lim = kb.plan_limits("plan_a", "GPPO", 2026)
    assert lim["deductible_individual"] == Decimal("1000") and lim["deductible_family"] == Decimal("2000")
    assert lim["oop_individual"] == Decimal("4000") and lim["pt_visit_limit"] == 30
    assert kb.plan_limits("plan_a", "GPPO", 2027)["pt_visit_limit"] == 24
    with pytest.raises(KbLookupError):
        kb.plan_limits("plan_a", "GPPO", 2030)


def test_rule_days_differ_per_tenant(kb_dir):
    kb = load_kb(kb_dir)
    assert kb.rule_days("plan_a", "GPPO", 2026, "Grievance resolution")[0] == 30
    assert kb.rule_days("plan_b", "CPPO", 2026, "Grievance resolution")[0] == 45
    assert kb.rule_days("plan_a", "GPPO", 2026, "Claim appeal filing window")[0] == 180


# ---- calculators -----------------------------------------------------------------------------------------

def test_money_helpers():
    assert fmt_money(Decimal("5591.43")) == "$5,591.43"
    assert remaining(Decimal("1000"), 154.48) == Decimal("845.52")
    assert remaining(Decimal("1000"), 1200) == Decimal("0.00")  # never negative
    assert to_money(0.1 + 0.2) == Decimal("0.30")  # no float drift
    assert money_line(Decimal("4000"), 2408.57)["remaining"] == "$1,591.43"


def test_deadline_status_boundaries():
    assert deadline_status("2026-06-01", 180, "2026-11-15")["deadline"] == "2026-11-28"
    on_day = deadline_status("2026-06-01", 180, "2026-11-28")
    assert on_day["deadline_passed"] is False and on_day["days_remaining"] == 0
    after = deadline_status("2026-06-01", 180, "2026-11-29")
    assert after["deadline_passed"] is True and after["days_overdue"] == 1
    assert deadline_status("2026-10-09", 30, "2026-11-15")["deadline"] == "2026-11-08"
    assert deadline_status("2026-12-20", 30, "2027-01-02")["deadline"] == "2027-01-19"  # crosses the year
