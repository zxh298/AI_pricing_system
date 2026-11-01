"""Step 10: the weekly scan, the investigation agent and the decisions around them.

The agent's tests need nothing but a generated warehouse. The scan and API tests need the Postgres container and use
the throwaway schema `diagnostic_test`. SAP is a fake a test can change ("the promotion ended").
"""
import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

os.environ["SAP_SCHEMA"], os.environ["DIAGNOSTIC_SCHEMA"] = "sap_test", "diagnostic_test"

from evals.triage import fake_sap, flagged_cases, score
from jobs.data_gen.generate import WEEK, write
from jobs.data_gen.outcome_scenarios import SCENARIOS
from jobs.pricing_engine.engine import price_week, record_send_results, save_recommendations
from jobs.weekly_triage.scan import Scan, run_sweep, run_triage
from services.diagnostic_api.app import create_app
from services.diagnostic_api.findings import FindingsStore
from services.diagnostic_api.investigate import AREAS, CAUSES, SUMMARY, SUBMIT, brief, investigate, summarize
from services.diagnostic_api.investigation_tools import INVESTIGATION_SCHEMAS, INVESTIGATION_TOOLS
from services.diagnostic_api.knowledge import KnowledgeMissing
from services.diagnostic_api.llm import AnthropicLLM, ScriptedLLM, text, tool_use
from services.diagnostic_api.sessions import SessionStore
from services.diagnostic_api.tools import ToolContext, execute_tool
from services.diagnostic_api.triage_store import CaseConflict, CaseStore
from shared import db
from shared.config import Config, load_config
from shared.diagnostic_schema import schema
from shared.warehouse import DuckDBWarehouse

try:
    db.connect().close()
    HAVE_PG = True
except Exception:
    HAVE_PG = False
pg = pytest.mark.skipif(not HAVE_PG, reason="Postgres not reachable (docker compose up -d postgres)")

SOLD = "2026-W38"
ANA = {"X-User": "ana", "X-Role": "analyst"}
SAM = {"X-User": "sam", "X-Role": "stakeholder"}
VIEWER = {"X-User": "vic"}


def done(causes=(), ruled_out=(), not_verified=()):
    return tool_use("submit_investigation", {"causes": list(causes), "ruled_out": list(ruled_out), "not_verified": list(not_verified)})


def cause(name, skus, explanation="It happened.", evidence=("a fact",), action="Ask the team."):
    return {"cause": name, "skus": list(skus), "explanation": explanation, "evidence": list(evidence), "proposed_action": action}


@pytest.fixture(scope="module")
def clean_copy(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("w") / "w.duckdb")
    write(path, seed=42, with_sap_tables=True)
    return path


def planted(clean_copy, tmp_path, name):
    """One scenario in its own copy: (case, context, truth)."""
    path = str(tmp_path / f"{name}.duckdb")
    shutil.copy(clean_copy, path)
    truth = SCENARIOS[name](path, SOLD)
    wh = DuckDBWarehouse(path)
    cases = flagged_cases(path, truth["promotions"], load_config())
    return cases[0], ToolContext("test", wh, fake_sap(wh, SOLD, truth["promotions"])), truth


# ======================= the agent's boundary =======================
def test_a_clean_week_opens_no_case(clean_copy):
    assert flagged_cases(clean_copy, [], load_config()) == []


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_scenario_is_flagged_exactly(clean_copy, tmp_path, name):
    path = str(tmp_path / "s.duckdb")
    shutil.copy(clean_copy, path)
    truth = SCENARIOS[name](path, SOLD)
    cases = flagged_cases(path, truth["promotions"], load_config())
    got, want = {(r["sku"], r["pack_qty"], r["region"]) for c in cases for r in c["records"]}, {tuple(r) for r in truth["records"]}
    if name == "pos":       # a fall in some stores can vanish in the regional total: never a false alarm, but it can miss
        assert got and got <= want
    else:
        assert got == want


def test_the_agent_cannot_read_the_planted_answer(clean_copy, tmp_path):
    case, _, truth = planted(clean_copy, tmp_path, "cannibal")
    assert truth["cause"] not in brief(case) and "promotion" not in brief(case).lower()


def test_only_read_only_tools_are_on_the_list():
    assert set(INVESTIGATION_TOOLS) == {s["name"] for s in INVESTIGATION_SCHEMAS}
    assert not {"diagnose_batch", "create_finding", "resolve_finding"} & set(INVESTIGATION_TOOLS)
    assert not {n for n in INVESTIGATION_TOOLS if n.startswith(("post", "send", "write", "set", "update", "delete"))}


def test_chat_cannot_call_the_agents_tools(clean_copy, tmp_path):
    _, ctx, _ = planted(clean_copy, tmp_path, "stock")
    out, is_error = execute_tool("compare_sell_through", {"week": SOLD, "skus": ["100000"]}, ctx)
    assert is_error and "unknown tool" in out["error"]


def test_the_agent_cannot_reach_a_tool_off_its_list(clean_copy, tmp_path):
    case, ctx, truth = planted(clean_copy, tmp_path, "stock")
    llm = ScriptedLLM([[tool_use("diagnose_batch", {"week": SOLD})], [done()]])
    result = investigate(case, ctx, llm, 5)
    assert result["trace"][0]["is_error"] and result["causes"] == []


def test_a_cause_with_its_signal_stands(clean_copy, tmp_path):
    case, ctx, truth = planted(clean_copy, tmp_path, "stock")
    skus = [r[0] for r in truth["records"]]
    llm = ScriptedLLM([[tool_use("get_stock_trend", {"week": SOLD, "skus": skus, "regions": ["NSW"]})],
                       [done([cause("STOCK_SHORTAGE", skus)], ["POS: tills match"], ["why stock is low"])]])
    result = investigate(case, ctx, llm, 5)
    assert result["status"] == "submitted" and [c["cause"] for c in result["causes"]] == ["STOCK_SHORTAGE"]
    assert score(truth, result)["skus_right"] and result["ruled_out"] == ["POS: tills match"]
    assert result["trace"][0]["tool"] == "get_stock_trend"


def test_a_cause_with_no_signal_behind_it_is_dropped(clean_copy, tmp_path):
    case, ctx, truth = planted(clean_copy, tmp_path, "stock")
    skus = [r[0] for r in truth["records"]]
    result = investigate(case, ctx, ScriptedLLM([[done([cause("POS_PRICE_GAP", skus)])]]), 5)
    assert result["causes"] == [] and result["dropped"][0]["cause"] == "POS_PRICE_GAP"


def test_substitution_needs_the_rival_and_its_promotion(clean_copy, tmp_path):
    case, ctx, truth = planted(clean_copy, tmp_path, "substitute")
    skus = [r[0] for r in truth["records"]]
    rival = truth["rival"]
    assert rival[0] not in brief(case)                                                 # the case does not name it
    llm = ScriptedLLM([[tool_use("find_rising_products", {"week": SOLD, "region": "NSW"})],
                       lambda msgs: [tool_use("get_sap_conditions", {"week": SOLD, "skus": [json.loads(msgs[-1]["content"][0]["content"])["rows"][0]["sku"]]})],
                       [done([cause("SUBSTITUTION", skus)])]])
    result = investigate(case, ctx, llm, 5)
    assert score(truth, result)["skus_right"] and score(truth, result)["followed_evidence"]
    bare = ToolContext("test", ctx.wh, fake_sap(ctx.wh, SOLD))                         # same sales, no promotion in SAP
    assert not investigate(case, bare, ScriptedLLM([[done([cause("SUBSTITUTION", skus)])]]), 5)["causes"]


def test_a_rise_too_small_to_explain_the_fall_does_not_support_substitution(clean_copy, tmp_path, monkeypatch):
    case, ctx, truth = planted(clean_copy, tmp_path, "substitute")
    skus = [r[0] for r in truth["records"]]
    monkeypatch.setattr("services.diagnostic_api.outcome.MIN_SHIFT_SHARE", 1000.0)
    result = investigate(case, ctx, ScriptedLLM([[done([cause("SUBSTITUTION", skus)])]]), 5)
    assert result["causes"] == [] and result["dropped"][0]["cause"] == "SUBSTITUTION"


def test_what_each_tool_returned_is_kept_for_audit(clean_copy, tmp_path):
    case, ctx, truth = planted(clean_copy, tmp_path, "stock")
    skus = [r[0] for r in truth["records"]]
    llm = ScriptedLLM([[tool_use("get_stock_trend", {"week": SOLD, "skus": skus, "regions": ["NSW"]})], [done()]])
    step = investigate(case, ctx, llm, 5)["trace"][0]
    assert step["result"]["sold_out"] == 3 and step["result"]["rows"][0]["sku"] in skus
    assert json.dumps(step, default=str)


def test_a_cause_is_kept_only_for_the_skus_that_show_it(clean_copy, tmp_path):
    case, ctx, truth = planted(clean_copy, tmp_path, "mixed")
    both = [r[0] for r in truth["records"]]
    result = investigate(case, ctx, ScriptedLLM([[done([cause("POS_PRICE_GAP", both), cause("STOCK_SHORTAGE", both)])]]), 5)
    assert {c["cause"]: c["skus"] for c in result["causes"]} == {k: sorted(v) for k, v in truth["causes"].items()}


def test_a_sku_outside_the_case_cannot_be_claimed(clean_copy, tmp_path):
    case, ctx, truth = planted(clean_copy, tmp_path, "stock")
    skus = [r[0] for r in truth["records"]]
    result = investigate(case, ctx, ScriptedLLM([[done([cause("STOCK_SHORTAGE", skus + ["999999"])])]]), 5)
    assert "999999" not in result["causes"][0]["skus"]


def test_other_never_stands_as_a_cause(clean_copy, tmp_path):
    case, ctx, truth = planted(clean_copy, tmp_path, "stock")
    result = investigate(case, ctx, ScriptedLLM([[done([cause("OTHER", [truth["records"][0][0]])])]]), 5)
    assert result["causes"] == [] and result["dropped"][0]["cause"] == "OTHER"


def test_the_cannibalization_signal_needs_the_override(clean_copy, tmp_path):
    case, ctx, truth = planted(clean_copy, tmp_path, "cannibal")
    sku = truth["records"][0][0]
    assert investigate(case, ctx, ScriptedLLM([[done([cause("CANNIBALIZATION", [sku])])]]), 5)["causes"]
    bare = ToolContext("test", ctx.wh, fake_sap(ctx.wh, SOLD))                         # same sales, no promotion in SAP
    assert not investigate(case, bare, ScriptedLLM([[done([cause("CANNIBALIZATION", [sku])])]]), 5)["causes"]


def test_a_number_the_tools_never_gave_is_flagged(clean_copy, tmp_path):
    case, ctx, truth = planted(clean_copy, tmp_path, "stock")
    skus = [r[0] for r in truth["records"]]
    result = investigate(case, ctx, ScriptedLLM([[done([cause("STOCK_SHORTAGE", skus, explanation="Price was $123.45.")])]]), 5)
    assert "123.45" in result["unverified"]


def test_the_step_limit_ends_with_nothing_claimed(clean_copy, tmp_path):
    case, ctx, truth = planted(clean_copy, tmp_path, "stock")
    call = [tool_use("get_stock_trend", {"week": SOLD, "skus": [truth["records"][0][0]]})]
    llm = ScriptedLLM([call, call, call])
    result = investigate(case, ctx, llm, 3)
    assert result["status"] == "step_limit" and result["causes"] == [] and len(llm.calls) == 3
    assert any("last tool round" in json.dumps(c["messages"][-1]) for c in llm.calls)


def test_the_tool_list_never_changes_during_a_run(clean_copy, tmp_path):
    case, ctx, truth = planted(clean_copy, tmp_path, "stock")
    call = [tool_use("get_stock_trend", {"week": SOLD, "skus": [truth["records"][0][0]]})]
    llm = ScriptedLLM([call, [text("thinking aloud")], [done()]])
    investigate(case, ctx, llm, 5)
    assert all(c["tools"] == llm.calls[0]["tools"] for c in llm.calls) and all(c["tool_choice"] is None for c in llm.calls)


def test_a_refusal_ends_the_run(clean_copy, tmp_path):
    case, ctx, _ = planted(clean_copy, tmp_path, "stock")
    llm = SimpleNamespace(create=lambda *a: {"content": [], "stop_reason": "refusal"})
    assert investigate(case, ctx, llm, 5)["status"] == "refused"


def test_the_summary_gets_no_data_tools_and_names_no_cause():
    case = {"error_code": "API_REJECTED", "reason": "X", "evidence": {"details": []}, "records": []}
    args = {"likely_area": "integration", "summary": "SAP returned a new code.", "next_checks": ["Read the SAP release notes"]}
    llm = ScriptedLLM([[tool_use("submit_summary", args)]])
    out = summarize(case, [], llm)
    assert out["status"] == "summarized" and [t["name"] for t in llm.calls[0]["tools"]] == ["submit_summary"]
    assert set(SUMMARY["input_schema"]["properties"]) == {"likely_area", "summary", "next_checks"}


def test_no_summary_when_the_model_does_not_answer():
    case = {"error_code": "E", "reason": "", "evidence": {}, "records": []}
    assert summarize(case, [], ScriptedLLM([[text("no")]]))["status"] == "no_summary"


def test_the_submit_schema_offers_exactly_the_known_causes():
    assert SUBMIT["input_schema"]["properties"]["causes"]["items"]["properties"]["cause"]["enum"] == CAUSES
    assert "OTHER" in CAUSES and "unknown" in AREAS


def test_the_triage_llm_keeps_thinking_and_uses_fallbacks():
    block = SimpleNamespace(type="thinking", thinking="hm", signature="sig")
    seen = {}

    def create(**kw):
        seen.update(kw)
        return SimpleNamespace(content=[block], stop_reason="end_turn")

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    out = AnthropicLLM(client=client, model="m", effort="medium", fallback=True, keep_thinking=True).create("s", [], [])
    assert out["content"] == [{"type": "thinking", "thinking": "hm", "signature": "sig"}]
    assert seen["fallbacks"] == "default" and seen["output_config"] == {"effort": "medium"}
    assert "temperature" not in seen and "tool_choice" not in seen


# ======================= a week with problems, for the scan =======================
class FakeRuns:
    def __init__(self):
        self.run, self.status = {"run_id": "run-1", "data_version": "run-1@t"}, "COMPLETED_WITH_ISSUES"

    def latest(self, week):
        return self.run

    def newest_status(self, week):
        return self.status


BOOKS = {t: {"owner": o, "severity": s, "doc_id": d, "why": f"Why {d}", "what_to_do": f"Do {d}", "what_not_to_do": f"Not {d}"}
         for t, o, s, d in (("NOT_EFFECTIVE:PROMOTION", "Promotions team", "warning", "PB-PROMO"),
                            ("API_REJECTED:VALIDITY_OVERLAP", "SAP Pricing team", "blocking", "PB-OVERLAP"),
                            ("MISSING_PRICE:MISSING_RULE", "Merchandising team", "blocking", "PB-RULE"),
                            ("MISSING_PRICE:FLOOR_ABOVE_PRICE", "Merchandising team", "blocking", "PB-FLOOR"),
                            ("MISSING_PRICE:NOT_IN_PRODUCTS", "Product Data team", "blocking", "PB-PRODUCTS"),
                            ("MISSING_IN_SAP", "SAP Pricing team", "warning", "PB-MISSING"))}


class FakeKb:
    def __init__(self):
        self.down = False

    def playbook_sections(self, tags):
        if self.down:
            raise KnowledgeMissing("not built")
        return {t: BOOKS[t] for t in tags if t in BOOKS}

    def similar(self, query):
        return []


def cfg(**over):
    return Config(**{"warehouse": "duckdb", "duckdb_path": "", "bigquery_dataset": "", "database_url": load_config().database_url,
                     "sap_base_url": "", "auth_mode": "none", "llm_provider": "scripted", "agent_mode": "router", "sap_read_key": "r",
                     "sap_write_key": "w", **over})


@pytest.fixture(scope="module")
def priced(clean_copy, tmp_path_factory):
    """W39 priced: three VIC promotions, two rejected records, everything else accepted."""
    path = str(tmp_path_factory.mktemp("p") / "p.duckdb")
    shutil.copy(clean_copy, path)
    wh = DuckDBWarehouse(path)
    rows = price_week(WEEK, wh)
    save_recommendations(path, "run-1", WEEK, rows)
    vic = [r for r in rows if r["status"] == "PRICED" and r["region"] == "VIC" and r["pack_qty"] == 1]
    promo, rejected = {r["sku"] for r in vic[:3]}, {r["sku"] for r in vic[3:5]}

    def bad(r):
        return r["sku"] in rejected and r["region"] == "VIC" and r["pack_qty"] == 1

    results = [{"sku": r["sku"], "pack_qty": r["pack_qty"], "region": r["region"], "status": "REJECTED" if bad(r) else "ACCEPTED",
                "code": "VALIDITY_OVERLAP" if bad(r) else "OK", "message": "validity overlaps run LEGACY" if bad(r) else "accepted"}
               for r in rows if r["status"] == "PRICED"]
    record_send_results(path, "run-1", WEEK, results)
    held = [{"sku": r["sku"], "pack_qty": r["pack_qty"], "region": r["region"], "price": r["recommended_price"],
             "effective_price": round(r["recommended_price"] * 0.7, 2) if r["sku"] in promo and r["region"] == "VIC" and r["pack_qty"] == 1
             else r["recommended_price"],
             "effective_source": "PROMOTION" if r["sku"] in promo and r["region"] == "VIC" and r["pack_qty"] == 1 else "MARKDOWN"}
            for r in rows if r["status"] == "PRICED" and not bad(r)]
    return SimpleNamespace(path=path, wh=wh, held=held, promo=promo, rejected=rejected, rows=rows)


@pytest.fixture
def world(priced):
    if not HAVE_PG:
        pytest.skip("Postgres not reachable")
    SessionStore().ensure_schema()
    with db.connect() as con:
        for t in ("triage_cases", "triage_events", "triage_notifications", "triage_runs", "findings"):
            con.execute(f"TRUNCATE {schema()}.{t}")
    clock = SimpleNamespace(now=datetime(2026, 9, 21, tzinfo=timezone.utc))
    store = CaseStore(clock=lambda: clock.now, response_hours=24, decision_owner="Lead")
    held = [dict(h) for h in priced.held]
    runs, kb, findings = FakeRuns(), FakeKb(), FindingsStore()
    ctx = ToolContext("weekly-triage", priced.wh, fake_sap(priced.wh, SOLD, extra=held), runs=runs, kb=kb)
    return SimpleNamespace(ctx=ctx, store=store, findings=findings, held=held, runs=runs, kb=kb, clock=clock, priced=priced,
                           scan=lambda llm=None, **over: run_triage(WEEK, Scan(ctx, store, findings, llm, cfg(**over))),
                           case=lambda code, reason="": next(c for c in store.list(WEEK) if c["error_code"] == code and c["reason"] == reason))


def end_promotions(w):
    for h in w.held:
        if h["effective_source"] == "PROMOTION":
            h["effective_price"], h["effective_source"] = h["price"], "MARKDOWN"


# ======================= the scan =======================
@pg
def test_known_errors_need_no_llm(world):
    out = world.scan(llm=None)
    assert out["status"] == "OK" and out["llm_steps"] == 0 and out["records_failing"] > 0
    promo = world.case("NOT_EFFECTIVE", "PROMOTION")
    assert (promo["confidence"], promo["owner"], promo["status"], promo["source"]) == ("CONFIRMED", "Promotions team", "open", "playbook")
    assert promo["record_count"] == 3 and promo["proposed_action"] == "Do PB-PROMO" and promo["deadline_at"] is not None


@pg
def test_each_team_gets_one_digest(world):
    world.scan()
    teams = [n["team"] for n in world.store.notifications(WEEK)]
    assert len(teams) == len(set(teams)) and {"Promotions team", "SAP Pricing team", "Merchandising team"} <= set(teams)


@pg
def test_a_repeat_scan_is_quiet_and_keeps_the_deadline(world):
    world.scan()
    before, deadline = len(world.store.notifications(WEEK)), world.case("NOT_EFFECTIVE", "PROMOTION")["deadline_at"]
    world.clock.now += timedelta(hours=2)
    out = world.scan()
    assert out["new"] == 0 and out["teams_notified"] == 0 and len(world.store.notifications(WEEK)) == before
    assert world.case("NOT_EFFECTIVE", "PROMOTION")["deadline_at"] == deadline


@pg
def test_accept_does_not_close_a_case_but_a_clean_rescan_does(world):
    world.scan()
    case = world.case("NOT_EFFECTIVE", "PROMOTION")
    world.store.accept(case["case_id"], "sam", world.findings)
    assert world.case("NOT_EFFECTIVE", "PROMOTION")["status"] == "accepted"
    world.scan()
    assert world.case("NOT_EFFECTIVE", "PROMOTION")["status"] == "accepted"            # still failing: stays open work
    end_promotions(world)
    out = world.scan()
    closed = world.case("NOT_EFFECTIVE", "PROMOTION")
    assert closed["status"] == "resolved" and out["resolved"] == 1 and out["findings_closed"]
    assert world.findings.list(week=WEEK, status="open") == []
    assert [e["event"] for e in world.store.events(closed["case_id"])] == ["opened", "accept", "resolved"]


@pg
def test_a_problem_that_returns_reopens_with_a_new_deadline(world):
    world.scan()
    saved = [dict(h) for h in world.held]
    end_promotions(world)
    world.scan()
    world.held[:] = saved
    world.clock.now += timedelta(days=2)
    world.scan()
    case = world.case("NOT_EFFECTIVE", "PROMOTION")
    assert (case["status"], case["change"]) == ("open", "reopened") and case["deadline_at"] > world.clock.now


@pg
def test_a_waiver_covers_the_records_it_named_only(world):
    world.scan()
    case = world.case("NOT_EFFECTIVE", "PROMOTION")
    world.store.waive(case["case_id"], "sam", "kept on purpose")
    assert world.scan()["teams_notified"] == 0 and world.case("NOT_EFFECTIVE", "PROMOTION")["status"] == "waived"
    extra = next(h for h in world.held if h["effective_source"] == "MARKDOWN" and h["region"] == "VIC" and h["pack_qty"] == 1)
    extra["effective_price"], extra["effective_source"] = 1.0, "PROMOTION"
    world.scan()
    assert world.case("NOT_EFFECTIVE", "PROMOTION")["status"] == "open"


@pg
def test_an_issue_with_an_open_finding_stays_quiet(world):
    skus = sorted(world.priced.promo)
    world.findings.create(week=WEEK, region="VIC", error_code="NOT_EFFECTIVE", reason="PROMOTION", root_cause="known",
                          confirmed_by="ana", skus=skus, owner="Promotions team")
    world.scan()
    case = world.case("NOT_EFFECTIVE", "PROMOTION")
    assert (case["status"], case["decision"], len(case["finding_ids"])) == ("accepted", "known_finding", 1)
    assert "Promotions team" not in [n["team"] for n in world.store.notifications(WEEK)]


@pg
def test_rejections_go_where_the_reason_says(world):
    world.scan()
    case = world.case("NOT_EFFECTIVE", "PROMOTION")
    wrong = world.store.reject(case["case_id"], "sam", "wrong_owner", new_owner="Data Platform team")
    assert (wrong["status"], wrong["owner"], wrong["route"]) == ("open", "Data Platform team", "development")
    refused = world.store.reject(case["case_id"], "sam", "action_refused")
    assert (refused["status"], refused["owner"], refused["route"]) == ("escalated", "Lead", "business")
    cause_wrong = world.store.reject(case["case_id"], "lee", "wrong_cause", note="not a promotion")
    assert (cause_wrong["status"], cause_wrong["owner"], cause_wrong["route"]) == ("escalated", "Development team", "development")


@pg
def test_wrong_owner_needs_a_name_and_a_closed_case_takes_no_decision(world):
    world.scan()
    case = world.case("NOT_EFFECTIVE", "PROMOTION")
    with pytest.raises(ValueError):
        world.store.reject(case["case_id"], "sam", "wrong_owner")
    world.store.waive(case["case_id"], "sam", "fine")
    with pytest.raises(CaseConflict):
        world.store.accept(case["case_id"], "sam", world.findings)


@pg
def test_a_missed_deadline_escalates_once(world):
    world.scan()
    world.clock.now += timedelta(hours=25)
    first = run_sweep(world.store)
    assert first["escalated"] > 0 and run_sweep(world.store)["escalated"] == 0
    case = world.case("NOT_EFFECTIVE", "PROMOTION")
    assert (case["status"], case["owner"]) == ("escalated", "Lead")
    assert "Lead" in [n["team"] for n in world.store.notifications(WEEK)]


@pg
def test_mass_failure_is_one_incident(world):
    world.held.clear()                                                  # SAP holds nothing: every accepted record is missing
    calls = []
    llm = SimpleNamespace(create=lambda *a: calls.append(a))
    out = world.scan(llm=llm)
    assert out["status"] == "BREAKER" and out["breaker"] and out["llm_steps"] == 0 and calls == []
    sent = world.store.notifications(WEEK)
    assert [(n["team"], n["kind"]) for n in sent] == [("Data Platform team", "incident")]
    assert all(c["deadline_at"] is None for c in world.store.list(WEEK))


@pg
def test_the_incident_ends_and_owners_hear_about_what_is_left(world):
    saved = [dict(h) for h in world.held]
    world.held.clear()
    world.scan()
    world.held[:] = saved
    out = world.scan()
    assert out["status"] == "OK" and out["teams_notified"] > 0
    assert world.case("NOT_EFFECTIVE", "PROMOTION")["deadline_at"] is not None
    assert world.case("MISSING_IN_SAP")["status"] == "resolved"


@pg
def test_no_run_is_an_incident_and_a_good_run_closes_it(world):
    latest, world.runs.run = world.runs.run, None
    out = world.scan()
    assert out["status"] == "INCIDENT" and world.store.notifications(WEEK)[0]["team"] == "Data Platform team"
    world.runs.run = latest
    world.scan()
    assert world.case("NO_USABLE_RUN", "NO_FINISHED_RUN")["status"] == "resolved"


@pg
def test_a_failed_newest_run_is_not_scanned_as_if_it_were_fine(world):
    world.runs.status = "FAILED"
    out = world.scan()
    assert out["status"] == "INCIDENT" and out["incident"] == "NO_USABLE_RUN"
    assert [c["error_code"] for c in world.store.list(WEEK)] == ["NO_USABLE_RUN"]


@pg
def test_a_knowledge_base_outage_sends_everything_to_development(world):
    world.kb.down = True
    out = world.scan()
    assert not out["knowledge_ok"]
    assert {c["route"] for c in world.store.list(WEEK)} == {"development"}


# ======================= the LLM steps inside a scan =======================
def plant_flag(world, monkeypatch, cases):
    """Two brands flagged, without rewriting the warehouse."""
    flags = [{"brand": b, "region": "VIC", "sku": s, "pack_qty": 1, "actual_units": 1, "baseline_units": base, "ratio": 0.02,
              "lost_units": int(base)} for b, s, base in cases]
    monkeypatch.setattr("jobs.weekly_triage.scan._outcome_flags", lambda scan, week: (flags, 100, SOLD, True))


@pg
def test_an_unknown_error_gets_one_summary_for_developers(world, monkeypatch):
    original = world.kb.playbook_sections
    world.kb.playbook_sections = lambda tags: {k: v for k, v in original(tags).items() if k != "API_REJECTED:VALIDITY_OVERLAP"}
    args = {"likely_area": "sap_configuration", "summary": "SAP rejects by overlap.", "next_checks": ["Check the condition table"]}
    llm = ScriptedLLM([[tool_use("submit_summary", args)]])
    out = world.scan(llm=llm)
    case = world.case("API_REJECTED", "VALIDITY_OVERLAP")
    assert out["llm_steps"] == 1 and len(llm.calls) == 1
    assert (case["confidence"], case["route"], case["owner"], case["source"]) == ("UNCLASSIFIED", "development", "Development team", "llm_summary")
    assert case["proposed_reason"] == "SAP rejects by overlap."


@pg
def test_the_budget_goes_to_the_largest_case(world, monkeypatch):
    plant_flag(world, monkeypatch, [("Small", "1", 30.0), ("Large", "2", 90.0)])
    llm = ScriptedLLM([[done()]])
    out = world.scan(llm=llm, triage_max_investigations=1)
    assert out["llm_steps"] == 1
    assert world.store.list(WEEK, None) and "Large" in brief_of(llm)
    small = next(c for c in world.store.list(WEEK) if c["brand"] == "Small")
    assert small["investigation"]["status"] == "over_budget" and small["route"] == "development"


def brief_of(llm):
    return llm.calls[0]["messages"][0]["content"]


@pg
def test_a_supported_cause_routes_by_code_not_by_the_model(world, monkeypatch):
    plant_flag(world, monkeypatch, [("B", "1", 50.0)])
    monkeypatch.setattr("services.diagnostic_api.investigate.supported_signals", lambda *a: {"STOCK_SHORTAGE": {("1", 1, "VIC")}})
    out = world.scan(llm=ScriptedLLM([[done([cause("STOCK_SHORTAGE", ["1"])])]]))
    case = next(c for c in world.store.list(WEEK) if c["kind"] == "outcome")
    assert (case["confidence"], case["owner"], case["route"], case["reason"]) == ("LIKELY", "Merchandising team", "business", "STOCK_SHORTAGE")
    assert out["llm_steps"] == 1


@pg
def test_an_unusable_action_is_replaced_by_codes_own(world, monkeypatch):
    plant_flag(world, monkeypatch, [("B", "1", 50.0)])
    monkeypatch.setattr("services.diagnostic_api.investigate.supported_signals", lambda *a: {"STOCK_SHORTAGE": {("1", 1, "VIC")}})
    world.scan(llm=ScriptedLLM([[done([cause("STOCK_SHORTAGE", ["1"], action="x")])]]))
    case = next(c for c in world.store.list(WEEK) if c["kind"] == "outcome")
    assert case["proposed_action"].startswith("Merchandising team: Check the replenishment")


@pg
def test_an_unsupported_cause_goes_to_development(world, monkeypatch):
    plant_flag(world, monkeypatch, [("B", "1", 50.0)])
    monkeypatch.setattr("services.diagnostic_api.investigate.supported_signals", lambda *a: {})
    world.scan(llm=ScriptedLLM([[done([cause("STOCK_SHORTAGE", ["1"])])]]))
    case = next(c for c in world.store.list(WEEK) if c["kind"] == "outcome")
    assert (case["confidence"], case["route"], case["proposed_reason"]) == ("UNCLASSIFIED", "development", "")
    assert case["investigation"]["dropped"][0]["cause"] == "STOCK_SHORTAGE"


@pg
def test_an_llm_failure_leaves_the_scan_and_the_case_intact(world, monkeypatch):
    plant_flag(world, monkeypatch, [("B", "1", 50.0)])

    def boom(*a):
        raise RuntimeError("api down")

    out = world.scan(llm=SimpleNamespace(create=boom))
    case = next(c for c in world.store.list(WEEK) if c["kind"] == "outcome")
    assert out["status"] == "OK" and case["investigation"]["status"] == "failed" and case["route"] == "development"
    assert case["records"]


@pg
def test_a_repeat_scan_does_not_pay_for_the_same_investigation_twice(world, monkeypatch):
    plant_flag(world, monkeypatch, [("B", "1", 50.0)])
    llm = ScriptedLLM([[done()]])
    world.scan(llm=llm)
    assert world.scan(llm=llm)["llm_steps"] == 0 and len(llm.calls) == 1


@pg
def test_no_sales_for_the_week_does_not_close_an_outcome_case(world, monkeypatch):
    plant_flag(world, monkeypatch, [("B", "1", 50.0)])
    world.scan()
    monkeypatch.setattr("jobs.weekly_triage.scan._outcome_flags", lambda scan, week: ([], 0, SOLD, False))
    world.scan()
    assert next(c for c in world.store.list(WEEK) if c["kind"] == "outcome")["status"] == "open"


@pg
def test_a_rerun_of_the_same_week_does_not_clear_final_sales_but_a_later_week_does(world):
    plant = {"brand": "B", "region": "VIC", "sku": "1", "pack_qty": 1, "actual_units": 1, "baseline_units": 50.0, "ratio": 0.02, "lost_units": 49}
    from services.diagnostic_api.triage_rules import outcome_cases
    world.store.sync("2026-W38", "r", outcome_cases("2026-W38", "2026-W37", [plant]), {"outcome"})
    assert world.store.resolve_earlier_outcomes("2026-W39", {("B", "VIC")}) == []
    assert len(world.store.resolve_earlier_outcomes("2026-W39", set())) == 1


@pg
def test_a_record_check_cannot_close_an_outcome_finding(world):
    world.findings.create(week=WEEK, region="VIC", error_code="LOW_SELL_THROUGH", reason="STOCK_SHORTAGE", root_cause="low stock",
                          confirmed_by="sam", skus=["1"])
    assert world.findings.auto_resolve(WEEK, ["VIC"], ["1"], {}) == []


# ======================= the API =======================
@pytest.fixture
def api(world, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("USER_REGIONS", '{"nsw-user": ["NSW"], "vic-user": ["VIC"]}')
    world.scan()
    with TestClient(create_app(SessionStore(), ctx_factory=lambda u, r: world.ctx, runs=world.runs, findings=world.findings, kb=world.kb,
                               cases=world.store)) as client:
        yield SimpleNamespace(http=client, world=world)


def overlap_in_two_regions(w):
    row = w.case("NOT_EFFECTIVE", "PROMOTION")
    records = row["records"] + [{**row["records"][0], "region": "NSW"}]
    with db.connect() as con:
        con.execute(f"UPDATE {schema()}.triage_cases SET records = %s::jsonb, regions = %s::jsonb WHERE case_id = %s",
                    (json.dumps(records), json.dumps(["NSW", "VIC"]), row["case_id"]))
    return row["case_id"]


@pg
def test_anyone_may_read_only_deciders_may_decide(api):
    c = api.world.case("NOT_EFFECTIVE", "PROMOTION")["case_id"]
    assert len(api.http.get(f"/triage/cases?week={WEEK}", headers=VIEWER).json()) > 0
    assert api.http.post(f"/triage/cases/{c}/accept", json={}, headers=VIEWER).status_code == 403
    assert api.http.post(f"/triage/cases/{c}/accept", json={}).status_code == 401


@pg
def test_a_stakeholder_accepts_and_the_finding_carries_their_name(api):
    c = api.world.case("NOT_EFFECTIVE", "PROMOTION")["case_id"]
    r = api.http.post(f"/triage/cases/{c}/accept", json={"ticket": "T-1"}, headers=SAM)
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    f = api.world.findings.list(week=WEEK, status="open")[0]
    assert (f["confirmed_by"], f["ticket"], f["owner"]) == ("sam", "T-1", "Promotions team") and f["evidence"]["case_id"] == c
    assert api.http.post(f"/triage/cases/{c}/accept", json={}, headers=SAM).status_code == 409


@pg
def test_a_multi_region_case_writes_one_finding_per_region(api):
    c = overlap_in_two_regions(api.world)
    assert api.http.post(f"/triage/cases/{c}/accept", json={}, headers=ANA).status_code == 200
    assert sorted(f["region"] for f in api.world.findings.list(week=WEEK)) == ["NSW", "VIC"]


@pg
def test_a_stakeholder_still_cannot_write_findings_directly(api):
    body = {"week": WEEK, "region": "VIC", "error_code": "X", "skus": ["1"], "root_cause": "because"}
    assert api.http.post("/findings", json=body, headers=SAM).status_code == 403


@pg
def test_a_user_sees_only_their_part_of_a_case_and_cannot_decide_it(api):
    c = overlap_in_two_regions(api.world)
    who = {"X-User": "nsw-user", "X-Role": "stakeholder"}
    seen = api.http.get(f"/triage/cases/{c}", headers=who).json()
    assert seen["partial"] and seen["regions"] == ["NSW"] and {r["region"] for r in seen["records"]} == {"NSW"}
    assert seen["evidence"] == {} and seen["investigation"] is None
    assert api.http.post(f"/triage/cases/{c}/accept", json={}, headers=who).status_code == 403


@pg
def test_a_case_outside_a_users_regions_is_not_found(api):
    c = api.world.case("NOT_EFFECTIVE", "PROMOTION")["case_id"]
    who = {"X-User": "nsw-user", "X-Role": "stakeholder"}
    assert api.http.get(f"/triage/cases/{c}", headers=who).status_code == 404
    assert api.http.post(f"/triage/cases/{c}/waive", json={"note": "x"}, headers=who).status_code == 404
    assert c not in [x["case_id"] for x in api.http.get(f"/triage/cases?week={WEEK}", headers=who).json()]


@pg
def test_rejection_reasons_are_validated(api):
    c = api.world.case("NOT_EFFECTIVE", "PROMOTION")["case_id"]
    assert api.http.post(f"/triage/cases/{c}/reject", json={"reason": "unhappy"}, headers=SAM).status_code == 422
    assert api.http.post(f"/triage/cases/{c}/reject", json={"reason": "wrong_owner"}, headers=SAM).status_code == 422
    r = api.http.post(f"/triage/cases/{c}/reject", json={"reason": "wrong_cause", "note": "not it"}, headers=SAM)
    assert r.json()["owner"] == "Development team"


@pg
def test_a_waiver_needs_a_reason(api):
    c = api.world.case("NOT_EFFECTIVE", "PROMOTION")["case_id"]
    assert api.http.post(f"/triage/cases/{c}/waive", json={}, headers=SAM).status_code == 422


@pg
def test_a_case_shows_its_history_and_the_scans_are_listed(api):
    c = api.world.case("NOT_EFFECTIVE", "PROMOTION")["case_id"]
    assert api.http.get(f"/triage/cases/{c}", headers=VIEWER).json()["events"][0]["event"] == "opened"
    assert api.http.get(f"/triage/scans?week={WEEK}", headers=VIEWER).json()[0]["status"] == "OK"
    assert api.http.get(f"/triage/notifications?week={WEEK}", headers=VIEWER).json()
