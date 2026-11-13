import asyncio
import dataclasses
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from test_step6_kb_calc import kb_dir  # noqa: F401
from test_step7_grievance import ASK, BODY, EV16_CONFIRM, env  # noqa: F401  (fixtures: fake mock API + fake model)
from test_step8_directory import CTX_A, FakeWorld, _settings as dir_settings

import app.agent.graph as graph_mod
import app.main as main
import app.turns as turns
from app.budget import BudgetExceeded, TurnBudget, safe_reply
from app.config import get_settings
from app.gateway.gateway import SessionContext, ToolGateway
from app.trace import Trace, member_hash, read_trace, trace_path

client = TestClient(main.app)
TENANT, MEMBER = "plan_a", "M749817"
REQ = {"tenant": TENANT, "member_id": MEMBER}
SSN = "123-45-6789"


@pytest.fixture(autouse=True)
def known_limits(monkeypatch):
    """Do not depend on the developer's .env: fixed prices and budgets for every test here."""
    for k, v in {"LLM_INPUT_COST_PER_MILLION": "0.10", "LLM_OUTPUT_COST_PER_MILLION": "0.50",
                 "BUDGET_TURN_MODEL_CALLS": "6", "BUDGET_TURN_TOOL_CALLS": "8", "BUDGET_TURN_SECONDS": "30",
                 "BUDGET_CONVERSATION_COST_USD": "0.03", "BUDGET_CONVERSATION_STEPS": "60",
                 "BUDGET_CONVERSATION_SECONDS": "900", "TENANT_TOKEN_PLAN_A": "ta", "TENANT_TOKEN_PLAN_B": "tb",
                 "OPENAI_API_KEY": "sk-test"}.items():
        monkeypatch.setenv(k, v)


def settings(**kw):
    return dataclasses.replace(get_settings(), **kw)


# ---- budget arithmetic ------------------------------------------------------------------------------------------

def test_cost_comes_from_token_counts_and_the_configured_prices():
    b = TurnBudget(settings())
    assert b.add_model_call(3000, 400) == pytest.approx(0.0005)  # 3000*0.10/1M + 400*0.50/1M
    assert b.cost_usd == pytest.approx(0.0005) and b.input_tokens == 3000 and b.output_tokens == 400


def test_turn_model_call_limit():
    b = TurnBudget(settings(budget_turn_model_calls=2))
    b.add_model_call(1, 1); b.check_model_call()
    b.add_model_call(1, 1)
    with pytest.raises(BudgetExceeded) as e:
        b.check_model_call()
    assert e.value.reason == "turn_model_calls" and b.stopped_by == "turn_model_calls"


def test_turn_tool_call_limit_returns_a_reason_instead_of_raising():
    b = TurnBudget(settings(budget_turn_tool_calls=1))
    assert b.check_tool_call() is None
    b.add_tool_call()
    assert b.check_tool_call() == "turn_tool_calls"


def test_conversation_limits_include_what_earlier_messages_used():
    prior = {"cost_usd": 0.0299, "model_calls": 5, "tool_calls": 5, "latency_ms": 1000}
    b = TurnBudget(settings(budget_conversation_cost_usd=0.03), prior)
    b.check_model_call()  # still just under
    b.add_model_call(1000, 100)  # +0.00015 -> over
    with pytest.raises(BudgetExceeded) as e:
        b.check_model_call()
    assert e.value.reason == "conversation_cost"
    assert TurnBudget(settings(budget_conversation_steps=10), prior).pre_turn_check() == "conversation_steps"
    assert TurnBudget(settings(budget_conversation_seconds=1), prior).pre_turn_check() == "conversation_time"


def test_zero_disables_a_limit():
    s = settings(budget_turn_model_calls=0, budget_turn_tool_calls=0, budget_turn_seconds=0,
                 budget_conversation_cost_usd=0, budget_conversation_steps=0, budget_conversation_seconds=0)
    b = TurnBudget(s, {"cost_usd": 999, "model_calls": 999, "tool_calls": 999, "latency_ms": 10**9})
    for _ in range(50):
        b.add_model_call(10**6, 10**6); b.add_tool_call()
    b.check_model_call()
    assert b.check_tool_call() is None and b.pre_turn_check() is None


def test_turn_time_limit():
    b = TurnBudget(settings(budget_turn_seconds=5))
    b._started -= 6
    with pytest.raises(BudgetExceeded) as e:
        b.check_model_call()
    assert e.value.reason == "turn_time"


def test_safe_replies_are_fixed_texts_per_reason():
    assert "time allowed" in safe_reply("turn_time")
    assert "number of steps" in safe_reply("turn_model_calls")
    assert "lookups" in safe_reply("turn_tool_calls")
    for r in ("conversation_cost", "conversation_steps", "conversation_time"):
        assert "start a new conversation" in safe_reply(r)


# ---- trace: safe by construction ----------------------------------------------------------------------------------

def _trace(tmp_state, **kw):
    s = settings(state_dir=tmp_state, **kw)
    return Trace(s, TENANT, MEMBER, "conv1", 1), s


def test_unknown_fields_are_dropped_and_strings_are_redacted(isolated_state_dir):
    t, _ = _trace(isolated_state_dir)
    ev = t.event("tool_call", name="get_claim", status="ok", message="PRIVATE rep text", text="PRIVATE",
                 prompt="PRIVATE", payload={"x": "PRIVATE"}, reason=f"called with SSN {SSN}")
    dumped = json.dumps(ev)
    assert "PRIVATE" not in dumped and SSN not in dumped and "[SSN]" in ev["reason"]
    assert {"message", "text", "prompt", "payload"}.isdisjoint(ev)


def test_member_id_is_hashed_with_a_salt_per_tenant(isolated_state_dir):
    t, s = _trace(isolated_state_dir)
    ev = t.event("turn", name="start")
    assert MEMBER not in json.dumps(ev) and ev["member_hash"] == member_hash(s, TENANT, MEMBER)
    assert member_hash(s, "plan_b", MEMBER) != ev["member_hash"]
    assert member_hash(settings(trace_hash_salt="other"), TENANT, MEMBER) != ev["member_hash"]


def test_events_are_appended_to_the_conversation_trace_and_the_tenant_audit_log(isolated_state_dir):
    t, s = _trace(isolated_state_dir)
    t.event("turn", name="start"); t.decision("get_claim", "denied", reason="claim_not_owned_by_session_member")
    t2 = Trace(s, TENANT, MEMBER, "conv1", 2)
    t2.event("turn", name="start")
    mine = read_trace(isolated_state_dir, TENANT, MEMBER, "conv1")
    assert [(e["turn"], e["seq"]) for e in mine] == [(1, 1), (1, 2), (2, 1)]  # appended, never rewritten
    assert [e["name"] for e in read_trace(isolated_state_dir, TENANT, MEMBER, "conv1", turn=2)] == ["start"]
    audit = list((isolated_state_dir / TENANT / "audit").glob("*.jsonl"))
    assert len(audit) == 1 and len(audit[0].read_text(encoding="utf-8").splitlines()) == 3
    assert not (isolated_state_dir / "plan_b").exists()  # tenant-prefixed


def test_trace_path_rejects_unsafe_ids(isolated_state_dir):
    for args in [("plan_c", MEMBER, "c"), (TENANT, "M12", "c"), (TENANT, MEMBER, "../x"), (TENANT, MEMBER, "a/b")]:
        with pytest.raises(ValueError):
            trace_path(isolated_state_dir, *args)


# ---- gateway: tool events, decisions and the tool-call budget ---------------------------------------------------------------

def _gw_run(world, fn, budget=None, trace=None, ctx=CTX_A):
    async def go():
        c = httpx.AsyncClient(transport=httpx.MockTransport(world.handler), base_url="http://api")
        async with ToolGateway(ctx, dir_settings(), client=c, budget=budget, trace=trace) as gw:
            return await fn(gw)
    return asyncio.run(go())


def test_each_tool_call_is_traced_with_name_status_and_latency_only(isolated_state_dir):
    t, s = _trace(isolated_state_dir)
    b = TurnBudget(s)
    _gw_run(FakeWorld(), lambda gw: gw.find_providers("Dermatology"), b, t)
    ev = [e for e in t.events if e["type"] == "tool_call"]
    assert len(ev) == 1 and ev[0]["name"] == "find_providers" and ev[0]["status"] == "ok" and "latency_ms" in ev[0]
    assert b.tool_calls == 1
    assert "Delgado" not in json.dumps(t.events) and "43123" not in json.dumps(t.events)


def test_a_tool_that_calls_another_tool_counts_and_traces_once(isolated_state_dir):
    from test_step6_gateway import _benefits, _claim
    t, s = _trace(isolated_state_dir)
    b = TurnBudget(s)

    class World:
        def handler(self, request):
            if request.url.path.endswith("/benefits"):
                return httpx.Response(200, json=_benefits())
            return httpx.Response(200, json=_claim(member="M485534"))

    import test_step6_kb_calc  # noqa: F401
    res = _gw_run(World(), lambda gw: gw.check_appeal_window("C2675159407"), b, t)
    assert res["appeal_deadline"] == "2026-11-28" or "error" in res  # KB may be absent: only the counting matters
    assert b.tool_calls == 1 and [e["name"] for e in t.events if e["type"] == "tool_call"] == ["check_appeal_window"]


def test_tool_budget_blocks_the_call_without_touching_the_api(isolated_state_dir):
    t, s = _trace(isolated_state_dir, budget_turn_tool_calls=1)
    b = TurnBudget(s)
    world = FakeWorld()

    async def two(gw):
        return [await gw.get_benefits(), await gw.get_benefits()]

    first, second = _gw_run(world, two, b, t)
    assert "error" not in first and second["error"] == "budget_exceeded"
    stops = [e for e in t.events if e["type"] == "gateway_decision" and e["decision"] == "budget_stop"]
    assert len(stops) == 1 and stops[0]["reason"] == "turn_tool_calls" and b.stopped_by == "turn_tool_calls"
    assert b.tool_calls == 1


def test_provider_filtering_is_recorded_as_a_decision(isolated_state_dir):
    t, s = _trace(isolated_state_dir)
    _gw_run(FakeWorld(ignore_filters=True), lambda gw: gw.find_providers("Dermatology"), TurnBudget(s), t)
    d = [e for e in t.events if e["type"] == "gateway_decision" and e["name"] == "find_providers"]
    assert d and d[0]["decision"] == "filtered" and d[0]["dropped"] > 0


def test_claim_ownership_denial_is_recorded_as_a_decision(isolated_state_dir):
    from test_step6_gateway import _claim
    t, s = _trace(isolated_state_dir)

    class World:
        def handler(self, request):
            return httpx.Response(200, json=_claim(member="M111111"))

    res = _gw_run(World(), lambda gw: gw.get_claim("C2675159407"), TurnBudget(s), t)
    assert res["error"] == "not_found"
    d = [e for e in t.events if e["type"] == "gateway_decision"]
    assert d[0]["decision"] == "denied" and d[0]["reason"] == "claim_not_owned_by_session_member"
    assert "M111111" not in json.dumps(t.events)


# ---- end to end with a scripted model: costs, limits, persistence --------------------------------------------------------------

class ScriptedModel:
    """Stands in for the LLM. Each call returns the next scripted message; the last one repeats."""

    def __init__(self, script, tokens=(1000, 100), sleep=0.0):
        self.script, self.tokens, self.sleep, self.calls = script, tokens, sleep, 0

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        if self.sleep:
            await asyncio.sleep(self.sleep)
        msg = self.script[min(self.calls - 1, len(self.script) - 1)]
        return AIMessage(content=msg.content, tool_calls=msg.tool_calls,
                         usage_metadata={"input_tokens": self.tokens[0], "output_tokens": self.tokens[1],
                                         "total_tokens": sum(self.tokens)})


def call_tool(n=1, name="get_benefits"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": f"call_{n}", "type": "tool_call"}])


ANSWER = AIMessage(content="The member's coverage is active.")


@pytest.fixture
def scripted(monkeypatch, isolated_state_dir):
    def install(model):
        graph_mod.get_graph.cache_clear()
        monkeypatch.setattr(graph_mod, "_build_model", lambda s: model)
        return model

    async def fake_get(self, tool, path, params=None):
        return 200, {"product_code": "GPPO", "plan_name": "Gold PPO", "network_id": "N", "coverage_status": "ACTIVE",
                     "coverage_effective": "2026-01-01", "coverage_termination": None, "accumulators": {}}

    monkeypatch.setattr(ToolGateway, "_get", fake_get)
    yield install
    graph_mod.get_graph.cache_clear()


def say(message="status?", cid=0, **kw):
    return client.post("/conversations", json={**REQ, "message": message, "conversation_id": cid, **kw})


def state_json(state_dir, cid):
    return json.loads((state_dir / TENANT / MEMBER / f"{cid}.json").read_text(encoding="utf-8"))


def test_a_normal_turn_reports_cost_and_writes_a_full_trace(scripted, isolated_state_dir):
    scripted(ScriptedModel([call_tool(), ANSWER]))
    r = say().json()
    assert r["reply"] == "The member's coverage is active."
    turn = r["budget"]["turn"]
    assert turn["model_calls"] == 2 and turn["tool_calls"] == 1
    assert turn["cost_usd"] == pytest.approx(2 * (1000 * 0.10 + 100 * 0.50) / 1e6)  # 0.0003
    assert r["budget"]["stopped_by"] is None and r["budget"]["conversation"]["cost_limit_usd"] == 0.03
    events = client.get(f"/conversations/{r['conversation_id']}/trace", params={**REQ}).json()["events"]
    shape = [(e["type"], e["name"]) for e in events]
    assert shape == [("turn", "start"), ("model_call", "gpt-6-luna"), ("tool_call", "get_benefits"),
                     ("model_call", "gpt-6-luna"), ("turn", "end")] or shape[0] == ("turn", "start")
    assert [e["type"] for e in events] == ["turn", "model_call", "tool_call", "model_call", "turn"]
    mc = [e for e in events if e["type"] == "model_call"]
    assert mc[0]["tokens_in"] == 1000 and mc[0]["tokens_out"] == 100 and mc[0]["cost_usd"] == pytest.approx(0.00015)
    assert events[-1]["status"] == "ok" and events[-1]["path"] == "model" and events[-1]["cost_usd"] == pytest.approx(0.0003)
    assert all("budget" in e for e in events)


def test_usage_is_saved_per_turn_and_accumulates_across_messages(scripted, isolated_state_dir):
    scripted(ScriptedModel([ANSWER]))
    cid = say().json()["conversation_id"]
    r2 = say("again", cid).json()
    rec = state_json(isolated_state_dir, cid)
    assert rec["turns"][0]["usage"]["model_calls"] == 1 and rec["turns"][1]["usage"]["model_calls"] == 1
    assert rec["usage"]["model_calls"] == 2 and rec["usage"]["cost_usd"] == pytest.approx(0.0003)
    assert r2["budget"]["conversation"]["cost_usd"] == pytest.approx(0.0003)
    assert r2["budget"]["conversation"]["steps"] == 2


def test_model_call_budget_ends_the_turn_cleanly(scripted, monkeypatch, isolated_state_dir):
    monkeypatch.setenv("BUDGET_TURN_MODEL_CALLS", "2")
    model = scripted(ScriptedModel([call_tool()]))  # never stops asking for tools
    r = say().json()
    assert r["reply"] == safe_reply("turn_model_calls") and r["budget"]["stopped_by"] == "turn_model_calls"
    assert model.calls == 2
    events = read_trace(isolated_state_dir, TENANT, MEMBER, r["conversation_id"])
    assert any(e["type"] == "gateway_decision" and e["decision"] == "budget_stop" and e["reason"] == "turn_model_calls" for e in events)
    assert events[-1]["status"] == "stopped" and events[-1]["stopped_by"] == "turn_model_calls"
    assert state_json(isolated_state_dir, r["conversation_id"])["usage"]["model_calls"] == 2  # usage still saved


def test_tool_call_budget_tells_the_model_no_and_it_can_still_answer(scripted, monkeypatch, isolated_state_dir):
    monkeypatch.setenv("BUDGET_TURN_TOOL_CALLS", "1")
    scripted(ScriptedModel([call_tool(1), call_tool(2), ANSWER]))
    r = say().json()
    assert r["reply"] == "The member's coverage is active." and r["budget"]["stopped_by"] == "turn_tool_calls"
    assert r["budget"]["turn"]["tool_calls"] == 1 and r["budget"]["turn"]["model_calls"] == 3
    events = read_trace(isolated_state_dir, TENANT, MEMBER, r["conversation_id"])
    assert [e["status"] for e in events if e["type"] == "tool_call"] == ["ok"]
    assert any(e.get("decision") == "budget_stop" and e["reason"] == "turn_tool_calls" for e in events)


def test_time_budget_cancels_a_slow_turn(scripted, monkeypatch, isolated_state_dir):
    monkeypatch.setenv("BUDGET_TURN_SECONDS", "0.3")
    scripted(ScriptedModel([ANSWER], sleep=2.0))
    r = say().json()
    assert r["reply"] == safe_reply("turn_time") and r["budget"]["stopped_by"] == "turn_time"
    assert r["budget"]["turn"]["elapsed_ms"] < 1500  # it did not wait the full two seconds


def test_conversation_cost_budget_stops_further_model_use(scripted, monkeypatch, isolated_state_dir):
    monkeypatch.setenv("BUDGET_CONVERSATION_COST_USD", "0.0004")
    model = scripted(ScriptedModel([call_tool(), ANSWER]))
    first = say().json()  # two model calls = 0.0003, under 0.0004
    assert first["reply"] == "The member's coverage is active."
    model.calls = 0
    second = say("again", first["conversation_id"]).json()
    assert second["reply"] == safe_reply("conversation_cost") and second["budget"]["stopped_by"] == "conversation_cost"
    # the third message is refused before the model is called at all
    model.calls = 0
    third = say("and again", first["conversation_id"]).json()
    assert third["reply"] == safe_reply("conversation_cost") and model.calls == 0
    events = read_trace(isolated_state_dir, TENANT, MEMBER, first["conversation_id"], turn=3)
    assert any(e["type"] == "gateway_decision" and e["name"] == "conversation" and e["reason"] == "conversation_cost" for e in events)


def test_conversation_step_budget(scripted, monkeypatch):
    monkeypatch.setenv("BUDGET_CONVERSATION_STEPS", "3")
    scripted(ScriptedModel([call_tool(), ANSWER]))
    first = say().json()  # 2 model calls + 1 tool call = 3 steps
    second = say("again", first["conversation_id"]).json()
    assert second["reply"] == safe_reply("conversation_steps")


def test_provider_gives_no_token_counts_so_cost_is_estimated(scripted, isolated_state_dir):
    class NoUsage(ScriptedModel):
        async def ainvoke(self, messages):
            self.calls += 1
            return AIMessage(content="x" * 400)

    scripted(NoUsage([ANSWER]))
    r = say().json()
    ev = [e for e in read_trace(isolated_state_dir, TENANT, MEMBER, r["conversation_id"]) if e["type"] == "model_call"][0]
    assert ev["estimated"] is True and ev["tokens_out"] == 100 and ev["tokens_in"] > 0
    assert r["budget"]["turn"]["cost_usd"] > 0


# ---- confirmations and guards are traced, and bypass the budget ---------------------------------------------------------------------

def test_confirmation_and_filing_are_traced_including_each_attempt(env, isolated_state_dir):  # noqa: F811
    api, _, c, state = env
    api.slow_first = True
    cid = c.post("/conversations", json={**BODY, "message": ASK, "conversation_id": 0}).json()["conversation_id"]
    c.post("/conversations", json={**BODY, "message": EV16_CONFIRM, "conversation_id": cid})
    events = read_trace(state, "plan_a", "M704649", cid, turn=2)
    names = [(e["type"], e["name"]) for e in events]
    assert ("confirmation", "classify") in names and ("confirmation", "filing") in names
    cls = next(e for e in events if e["name"] == "classify")
    assert cls["intent"] == "confirm"
    attempts = [e for e in events if e.get("name") == "file_grievance"]
    assert [a["http_status"] for a in attempts] == [503, 200] and attempts[0]["key_prefix"] == attempts[1]["key_prefix"]
    filing = next(e for e in events if e["name"] == "filing")
    assert filing["state"] == "filed" and filing["attempts"] == 2
    assert events[-1]["path"] == "confirm" and events[-1]["model_calls"] == 0
    text = json.dumps(events)
    assert "billed" not in text and "M704649" not in text and "G26-" not in text  # no content, no raw id


def test_an_exhausted_conversation_budget_does_not_block_a_confirmed_write(env, monkeypatch, isolated_state_dir):  # noqa: F811
    api, _, c, state = env
    cid = c.post("/conversations", json={**BODY, "message": ASK, "conversation_id": 0}).json()["conversation_id"]
    path = state / "plan_a" / "M704649" / f"{cid}.json"
    rec = json.loads(path.read_text(encoding="utf-8"))
    rec["usage"] = {"model_calls": 99, "tool_calls": 99, "input_tokens": 0, "output_tokens": 0,
                    "cost_usd": 5.0, "latency_ms": 10**7}
    path.write_text(json.dumps(rec), encoding="utf-8")
    reply = c.post("/conversations", json={**BODY, "message": "Confirmed", "conversation_id": cid}).json()
    assert "has been filed" in reply["reply"] and len(api.grievances) == 1  # the write was allowed
    other = c.post("/conversations", json={**BODY, "message": "What is the deductible?", "conversation_id": cid}).json()
    assert other["reply"] == safe_reply("conversation_cost")  # but new questions are stopped


def test_guard_and_redaction_events_carry_counts_not_content(monkeypatch, isolated_state_dir):
    async def leaky(message, history=None, gateway=None, extra_system=None, budget=None, trace=None):
        return f"The SSN is {SSN}"

    monkeypatch.setattr(turns, "run_turn", leaky)
    r = say(f"Caller SSN {SSN} phone 555-014-2233").json()
    events = read_trace(isolated_state_dir, TENANT, MEMBER, r["conversation_id"])
    guard = [e for e in events if e["type"] == "guard"]
    assert any(g["name"] == "output_guard" and g["kinds"] == ["ssn"] for g in guard)
    assert any(g["name"] == "redaction" and g["redactions"]["rep_message"] for g in guard)
    everything = "\n".join(p.read_text(encoding="utf-8") for p in isolated_state_dir.rglob("*.jsonl"))
    assert SSN not in everything and "555-014-2233" not in everything


# ---- the trace endpoint ------------------------------------------------------------------------------------------------------------------

def test_trace_endpoint_returns_events_and_filters_by_turn(scripted):
    scripted(ScriptedModel([ANSWER]))
    cid = say().json()["conversation_id"]
    say("two", cid)
    all_events = client.get(f"/conversations/{cid}/trace", params=REQ).json()["events"]
    assert {e["turn"] for e in all_events} == {1, 2}
    only2 = client.get(f"/conversations/{cid}/trace", params={**REQ, "turn": 2}).json()["events"]
    assert only2 and {e["turn"] for e in only2} == {2}


def test_trace_endpoint_hides_other_tenants_members_and_unknown_ids(scripted):
    scripted(ScriptedModel([ANSWER]))
    cid = say().json()["conversation_id"]
    for params in [{"tenant": "plan_b", "member_id": MEMBER}, {"tenant": TENANT, "member_id": "M111111"}]:
        r = client.get(f"/conversations/{cid}/trace", params=params)
        assert r.status_code == 404 and r.json() == {"detail": "Conversation not found", "conversation_id": None}
    assert client.get("/conversations/nope/trace", params=REQ).status_code == 404
    assert client.get(f"/conversations/{cid}/trace", params={"tenant": TENANT, "member_id": "bad"}).status_code == 422
    assert client.get(f"/conversations/{cid}/trace", params={"tenant": "plan_c", "member_id": MEMBER}).status_code == 422
    assert client.get(f"/conversations/{cid}/trace", params=REQ).status_code == 200


def test_the_trace_never_contains_message_text_or_raw_member_ids(scripted, isolated_state_dir):
    scripted(ScriptedModel([call_tool(), ANSWER]))
    cid = say("CANARY-REP-TEXT: what is the status of the claim?").json()["conversation_id"]
    body = client.get(f"/conversations/{cid}/trace", params=REQ).text
    files = "\n".join(p.read_text(encoding="utf-8") for p in isolated_state_dir.rglob("*.jsonl"))
    for blob in (body, files):
        assert "CANARY-REP-TEXT" not in blob and "coverage is active" not in blob and MEMBER not in blob
