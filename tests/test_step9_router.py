"""Router mode: classify -> validate -> fixed workflow -> render. No network, no key; Postgres only for the API test.

The model is scripted: step 1 answers the forced `classify` call, step 2 writes the answer from the results it is sent.
Fixture `env` (tests/conftest.py) is the planted "VIC broken, NSW fine" scenario: three VIC items overridden by a
promotion, two VIC items rejected with VALIDITY_OVERLAP.
"""
import dataclasses
import json
from types import SimpleNamespace

import httpx
import pytest

from jobs.data_gen.generate import WEEK
from services.diagnostic_api import chat
from services.diagnostic_api.llm import (RESULTS_HEADER, AnthropicLLM, ScriptedLLM, get_llm, render_payload,
                                         scripted_router, text, tool_use)
from services.diagnostic_api.router import (CLASSIFY_TOOL, FALLBACK_NOTE, INTENTS, OUT_OF_SCOPE, RENDER_SYSTEM, UNCLEAR,
                                            Clarify, classify, run_router, unverified, validate)
from services.diagnostic_api.state import SessionState


def scripted(slots, answer="the answer"):
    """A model that classifies as `slots` and then answers with `answer` (a string, or a function of the payload)."""
    def render(msgs):
        return [text(answer(render_payload(msgs)) if callable(answer) else answer)]
    return ScriptedLLM([[tool_use("classify", slots)], render])


def ask(env, slots, answer="the answer", ctx=None, **kw):
    llm = scripted(slots, answer)
    return run_router("q", ctx or env["ctx"], llm, WEEK, **kw), llm


def broken(env):
    """The skus with a planted problem (promotion override or rejection), all in VIC."""
    return sorted({s for s, _, _ in env["promo"] | env["rejected"]})


def clean_sku(env):
    bad = {s for s, _, _ in env["promo"] | env["rejected"]}
    return next(s for s in env["skus"] if s not in bad)


def with_state(env):
    return dataclasses.replace(env["ctx"], state=SessionState())


# ======================= classify =======================
def test_classify_forces_the_one_tool_and_keeps_only_given_slots():
    llm = ScriptedLLM([[tool_use("classify", {"intent": "diagnose", "brand": "Larkspur", "regions": ["VIC"], "skus": [],
                                              "category": "", "group_id": None})]])
    history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": [text("before")]}]
    slots = classify("why no drop in VIC?", llm, WEEK, history, "Session state (kept by the service, not by you):\n- G1")
    assert slots == {"intent": "diagnose", "brand": "Larkspur", "regions": ["VIC"]}
    call = llm.calls[0]
    assert call["tools"] == [CLASSIFY_TOOL] and call["tool_choice"] == {"type": "tool", "name": "classify"}
    assert WEEK in call["system"] and "Session state" in call["system"] and all(i in call["system"] for i in INTENTS)
    assert call["messages"] == history + [{"role": "user", "content": "why no drop in VIC?"}]
    assert CLASSIFY_TOOL["input_schema"]["properties"]["intent"]["enum"] == list(INTENTS)


def test_classify_shows_an_example_per_intent():
    llm = ScriptedLLM([[tool_use("classify", {"intent": "how_to"})]])
    classify("q", llm, WEEK)
    from services.diagnostic_api.router import EXAMPLES
    assert set(EXAMPLES) == set(INTENTS)
    assert all(f'"{EXAMPLES[i][0]}"' in llm.calls[0]["system"] for i in INTENTS)


def test_the_prompt_examples_are_not_the_evaluation_questions():
    from evals.routing import load_questions
    from services.diagnostic_api.router import EXAMPLES
    asked = {q["question"].lower() for q in load_questions()}
    assert not asked & {e.lower() for es in EXAMPLES.values() for e in es}


@pytest.mark.parametrize("blocks", [[tool_use("classify", {"intent": "make_coffee"})], [text("no tool")],
                                    [tool_use("classify", {})]])
def test_an_answer_without_a_known_intent_is_other(blocks):
    assert classify("q", ScriptedLLM([blocks]), WEEK)["intent"] == "other"


# ======================= validate =======================
def test_validate_fills_the_week_and_normalises_names(env):
    s = validate({"intent": "diagnose", "brand": "brand a", "regions": ["vic", " NSW"]}, env["ctx"], WEEK)
    assert s == {"intent": "diagnose", "week": WEEK, "brand": "Brand A", "regions": ["NSW", "VIC"]}


@pytest.mark.parametrize("slots, says", [
    ({"week": "last week"}, "Which week"),
    ({"week": "2026-W01"}, "no clearance list"),
    ({"regions": ["WA"]}, "Available: NSW, QLD, VIC"),
    ({"brand": "Acme"}, "Available: Brand A, Harbour Ridge"),
    ({"category": "Beer"}, "Available: Red, Sparkling"),
    ({"group_id": "G7"}, "Which products"),
])
def test_validate_asks_instead_of_guessing(env, slots, says):
    with pytest.raises(Clarify, match=says):
        validate({"intent": "rule_check", **slots}, env["ctx"], WEEK)


def test_validate_only_offers_regions_the_user_may_see(env):
    ctx = dataclasses.replace(env["ctx"], allowed_regions=frozenset({"NSW"}))
    with pytest.raises(Clarify) as e:
        validate({"intent": "diagnose", "regions": ["VIC"]}, ctx, WEEK)
    assert "Available: NSW." in str(e.value) and "QLD" not in str(e.value)


def test_an_unknown_group_falls_back_to_the_other_slots(env):
    s = validate({"intent": "diagnose", "group_id": "G9", "brand": "Larkspur"}, with_state(env), WEEK)
    assert "group_id" not in s and s["brand"] == "Larkspur"


NAMES = {"brand": ["Brand A", "Harbour Ridge", "Larkspur"], "category": ["Red", "Sparkling"], "regions": ["NSW", "QLD", "VIC"]}


@pytest.mark.parametrize("question, slots, expected", [
    ("VIC prices didn't drop for Larkspur, why?", {}, {"brand": "Larkspur", "regions": ["VIC"]}),
    ("would any sparkling go below floor in nsw or qld", {}, {"category": "Sparkling", "regions": ["NSW", "QLD"]}),
    ("harbour ridge reduced prices", {}, {"brand": "Harbour Ridge"}),            # "reduced" is not the category Red
    ("Larkspur or Brand A?", {}, {}),                                             # two brands: no guess
    ("Larkspur in VIC", {"brand": "Brand A", "regions": ["NSW"]}, {"brand": "Brand A", "regions": ["NSW"]}),   # model wins
    ("vicinity of the Redwood", {}, {}),                                          # whole words only
])
def test_backfill_fills_only_names_the_question_states(question, slots, expected):
    from services.diagnostic_api.router import backfill
    assert backfill({"intent": "diagnose", **slots}, question, NAMES) == {"intent": "diagnose", **expected}


def test_validate_backfills_from_the_question_and_the_turn_records_it(env):
    s = validate({"intent": "diagnose"}, env["ctx"], WEEK, "why no drop for larkspur in vic?")
    assert (s["brand"], s["regions"]) == ("Larkspur", ["VIC"])
    out = run_router("Larkspur, VIC: why?", env["ctx"], scripted({"intent": "diagnose"}), WEEK)
    assert out["trace"]["filled_by_code"] == ["brand", "regions"]
    assert out["tool_calls"][0]["input"] == {"week": WEEK, "brand": "Larkspur", "regions": ["VIC"]}


def test_a_backfilled_region_is_still_permission_checked(env):
    ctx = dataclasses.replace(env["ctx"], allowed_regions=frozenset({"NSW"}))
    with pytest.raises(Clarify, match="VIC is not a region you can see"):
        validate({"intent": "diagnose"}, ctx, WEEK, "why not in VIC?")


def test_unscoped_intents_are_not_checked_against_the_list(env):
    assert validate({"intent": "how_to", "regions": ["WA"]}, env["ctx"], WEEK)["regions"] == ["WA"]


# ======================= workflows =======================
def test_diagnose_follows_up_only_on_the_problems_found(env):
    out, llm = ask(env, {"intent": "diagnose", "skus": broken(env)})
    assert [c["tool"] for c in out["tool_calls"]] == ["diagnose_batch", "get_sap_conditions", "get_api_log"]
    assert not any(c["is_error"] for c in out["tool_calls"])
    assert (out["intent"], out["route"], out["steps"], out["escalated"]) == ("diagnose", "workflow", 2, False)

    clean, _ = ask(env, {"intent": "diagnose", "skus": [clean_sku(env)], "regions": ["NSW"]})
    assert [c["tool"] for c in clean["tool_calls"]] == ["diagnose_batch"]                  # nothing to follow up


def test_diagnose_by_brand_resolves_the_products_first_and_uses_the_group(env):
    out, _ = ask(env, {"intent": "diagnose", "brand": "Larkspur"}, ctx=with_state(env))
    tools = [c["tool"] for c in out["tool_calls"]]
    assert tools[:2] == ["resolve_products", "diagnose_batch"]
    assert out["tool_calls"][1]["input"]["group_id"] == "G1" and "skus" not in out["tool_calls"][1]["input"]


@pytest.mark.parametrize("intent, tools", [
    ("list_products", ["resolve_products"]), ("sap_prices", ["get_sap_conditions"]), ("api_log", ["get_api_log"]),
    ("rule_check", ["check_rules"]), ("known_issues", ["get_findings"]), ("how_to", ["search_docs"])])
def test_each_intent_runs_its_fixed_tools(env, intent, tools):
    out, _ = ask(env, {"intent": intent, "skus": broken(env), "query": "what is a 429?"})
    assert [c["tool"] for c in out["tool_calls"]] == tools and out["route"] == "workflow"


def test_known_issues_passes_a_single_region_and_how_to_defaults_to_the_question(env):
    out, _ = ask(env, {"intent": "known_issues", "regions": ["vic"], "error_code": "NOT_EFFECTIVE"})
    assert out["tool_calls"][0]["input"] == {"week": WEEK, "region": "VIC", "error_code": "NOT_EFFECTIVE"}
    out, _ = ask(env, {"intent": "how_to"})
    assert out["tool_calls"][0]["input"] == {"query": "q"}


def test_no_matching_products_stops_before_the_diagnosis(env):
    out, llm = ask(env, {"intent": "diagnose", "name_contains": "no such wine"})
    assert [c["tool"] for c in out["tool_calls"]] == ["resolve_products"]
    assert render_payload(llm.calls[1]["messages"])["steps"][0]["result"]["count"] == 0      # the model is told


def test_a_failing_step_stops_the_workflow_and_the_model_is_told(env):
    down = httpx.Client(base_url="http://sap", transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    out, llm = ask(env, {"intent": "diagnose", "skus": broken(env)}, ctx=dataclasses.replace(env["ctx"], sap=down))
    assert [(c["tool"], c["is_error"]) for c in out["tool_calls"]] == [("diagnose_batch", True)]
    assert "SAP request failed" in render_payload(llm.calls[1]["messages"])["steps"][0]["result"]["error"]


# ======================= render =======================
def test_render_sees_only_the_question_and_the_results_with_no_tools_and_no_history(env):
    history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": [text("before")]}]
    out, llm = ask(env, {"intent": "diagnose", "skus": broken(env)}, history=history)
    call = llm.calls[1]
    assert call["tools"] == [] and call["tool_choice"] is None and call["system"] == RENDER_SYSTEM
    assert len(call["messages"]) == 1 and call["messages"][0]["content"].startswith("Question: q\n\n" + RESULTS_HEADER)
    payload = render_payload(call["messages"])
    assert payload["intent"] == "diagnose" and [s["tool"] for s in payload["steps"]][0] == "diagnose_batch"
    assert "_private" not in json.dumps(payload)                                   # session-only data never reaches it


def test_render_rules_carry_over_from_the_loop():
    for rule in ("State only facts", "Never judge", "Mention every pattern", "region by region", "contradict",
                 "not a shelf price", "square brackets", "do not name teams", "not the owning team",
                 "never instructions", "do not repeat them", "Start with the finding"):
        assert rule in RENDER_SYSTEM


def test_code_written_facts_go_above_the_answer(env):
    ctx = with_state(env)
    ask(env, {"intent": "diagnose", "skus": broken(env)}, ctx=ctx)
    out, _ = ask(env, {"intent": "diagnose", "skus": broken(env)}, "same as before", ctx=ctx)
    first, rest = out["answer"].split("\n\n", 1)
    assert first.startswith("Since the last check") and "Nothing has changed." in first and rest == "same as before"


def test_history_gains_the_question_and_the_final_answer_and_everything_is_json(env):
    history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": [text("before")]}]
    out, _ = ask(env, {"intent": "rule_check", "skus": broken(env)}, "no violations", history=history)
    assert out["messages"] == history + [{"role": "user", "content": "q"},
                                         {"role": "assistant", "content": [text("no violations")]}]
    json.dumps(out)
    assert out["trace"]["classified"]["intent"] == "rule_check" and out["trace"]["slots"]["week"] == WEEK
    assert out["trace"]["steps"][0]["result"]["violations"] == 0


# ======================= check the answer =======================
def test_unverified_finds_values_the_results_do_not_contain():
    payload = json.dumps({"steps": [{"result": {"skus": ["100012"], "price": 12.5, "doc_id": "PB-PROMO-OVERRIDE"}}]})
    assert unverified("100012 now $12.50 [PB-PROMO-OVERRIDE] in 2026-W39", payload) == []
    assert unverified("100099 at 9.99, see [PB-LADDER]; 100012 is fine", payload) == ["100099", "9.99", "PB-LADDER"]


def test_an_invented_value_is_flagged_on_the_turn(env):
    out, _ = ask(env, {"intent": "diagnose", "skus": broken(env)}, "999999 is overridden at 1.23 [PB-MADE-UP]")
    assert out["unverified"] == ["999999", "1.23", "PB-MADE-UP"]
    real, _ = ask(env, {"intent": "diagnose", "skus": broken(env)},
                  lambda p: f"{p['steps'][0]['result']['patterns'][0]['sample_skus'][0]} is affected")
    assert real["unverified"] == []


# ======================= routes without a workflow =======================
def test_out_of_scope_and_unclear_answer_without_tools_or_a_second_model_call(env):
    out, llm = ask(env, {"intent": "out_of_scope"})
    assert (out["answer"], out["route"], out["tool_calls"], len(llm.calls)) == (OUT_OF_SCOPE, "workflow", [], 1)
    out, _ = ask(env, {"intent": "unclear", "clarify": "Which brand?"})
    assert (out["answer"], out["route"]) == ("Which brand?", "clarify")
    assert ask(env, {"intent": "unclear"})[0]["answer"] == UNCLEAR


def test_a_slot_that_fails_validation_is_a_clarifying_question(env):
    out, llm = ask(env, {"intent": "diagnose", "brand": "Acme"})
    assert out["route"] == "clarify" and "No brand called 'Acme'" in out["answer"]
    assert out["tool_calls"] == [] and len(llm.calls) == 1 and out["steps"] == 1


def test_other_falls_back_to_the_open_loop_and_says_so(env):
    llm = ScriptedLLM([[tool_use("classify", {"intent": "other"})],
                       [tool_use("check_rules", {"week": WEEK, "skus": env["skus"]})], [text("No rule violations.")]])
    out = run_router("compare this week with last week", env["ctx"], llm, WEEK)
    assert out["route"] == "fallback" and out["intent"] == "other"
    assert out["answer"] == f"{FALLBACK_NOTE}\n\nNo rule violations."
    assert [c["tool"] for c in out["tool_calls"]] == ["check_rules"] and out["steps"] == 3   # classify + 2 loop steps
    assert len(llm.calls[1]["tools"]) == 7                                               # the loop gets every tool


# ======================= providers, demo and CLI =======================
def test_anthropic_provider_forces_a_tool_and_omits_an_empty_tool_list():
    requests = []
    reply = SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")], stop_reason="end_turn")
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: requests.append(kw) or reply))
    llm = AnthropicLLM(client=client)
    llm.create("s", [CLASSIFY_TOOL], [{"role": "user", "content": "q"}], tool_choice={"type": "tool", "name": "classify"})
    llm.create("s", [], [{"role": "user", "content": "q"}])
    assert requests[0]["tool_choice"] == {"type": "tool", "name": "classify"} and requests[0]["tools"] == [CLASSIFY_TOOL]
    assert "tools" not in requests[1] and "tool_choice" not in requests[1]


def test_the_scripted_provider_follows_the_agent_mode(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "scripted")
    monkeypatch.delenv("AGENT_MODE", raising=False)                     # router is the default
    assert get_llm(WEEK).steps[0][0]["name"] == "classify"
    monkeypatch.setenv("AGENT_MODE", "loop")
    assert get_llm(WEEK).steps[0][0]["name"] == "resolve_products"


def test_the_offline_router_demo_reports_the_planted_problems(env):
    brand = next(c["brand"] for c in env["wh"].get_candidates(WEEK) if (c["sku"], 1, "VIC") in env["promo"] and c["brand"])
    out = run_router("why?", with_state(env), scripted_router(WEEK, brand, ("VIC", "NSW")), WEEK)
    assert out["route"] == "workflow" and "NOT_EFFECTIVE" in out["answer"] and "PROMOTION" in out["answer"]
    assert out["unverified"] == []


def test_cli_runs_the_router(env, monkeypatch, capsys):
    monkeypatch.setattr(chat, "make_context", lambda user, run_id=None: env["ctx"])
    monkeypatch.setattr(chat, "load_dotenv", lambda *a: None)
    monkeypatch.setenv("LLM_PROVIDER", "scripted")
    monkeypatch.setenv("AGENT_MODE", "router")
    assert chat.main(["why?", "--week", WEEK, "--quiet"]) == 0
    assert "intent diagnose, route workflow" in capsys.readouterr().out


# ======================= routing evaluation (evals/routing.py) =======================
def test_the_labelled_questions_use_known_intents_and_slots():
    from evals.routing import load_questions
    qs = load_questions()
    assert len({q["id"] for q in qs}) == len(qs) >= 20
    slots = set(CLASSIFY_TOOL["input_schema"]["properties"])
    for q in qs:
        assert q["intent"] in INTENTS and set(q.get("accept", [])) <= set(INTENTS) and set(q["slots"]) <= slots
    assert {q["intent"] for q in qs} == set(INTENTS)                                  # every intent is covered


def test_scoring_normalises_slots_and_accepts_listed_alternatives():
    from evals.routing import score
    q = {"intent": "diagnose", "accept": ["sap_prices"], "slots": {"brand": "Larkspur", "regions": ["VIC", "NSW"]}}
    assert score(q, {"intent": "sap_prices", "brand": "larkspur ", "regions": ["nsw", "vic"], "week": "x"}) == {
        "intent_ok": True, "slots_ok": True, "wrong_slots": {}}
    s = score(q, {"intent": "how_to", "brand": "Stonefield"})
    assert not s["intent_ok"] and s["wrong_slots"] == {"brand": ("Larkspur", "Stonefield"), "regions": (["VIC", "NSW"], None)}


def test_evaluate_summarise_and_report_with_a_scripted_model():
    from evals.routing import evaluate, report, summarise
    qs = [{"id": "a", "question": "x?", "intent": "diagnose", "slots": {"brand": "Larkspur"}},
          {"id": "b", "question": "y?", "intent": "how_to", "slots": {}, "state": "Session state: G1"}]
    llm = ScriptedLLM([[tool_use("classify", {"intent": "diagnose", "brand": "Larkspur"})],
                       [tool_use("classify", {"intent": "known_issues"})]])
    rows = evaluate(qs, llm)
    assert "Session state: G1" in llm.calls[1]["system"]
    s = summarise(rows)
    assert (s["intent_accuracy"], s["model_slot_accuracy"], s["slot_accuracy"], s["both"]) == (0.5, 1.0, 1.0, 1)
    assert s["per_intent"] == {"diagnose": [1, 1], "how_to": [0, 1]}
    out = report(rows)
    assert "intent 50%" in out and "MISS b 'y?': expected how_to, got known_issues" in out and " a " not in out


def test_evaluate_scores_the_model_alone_and_after_the_backfill():
    from evals.routing import evaluate, summarise
    qs = [{"id": "a", "question": "Larkspur in VIC: why?", "intent": "diagnose", "slots": {"brand": "Larkspur", "regions": ["VIC"]}},
          {"id": "b", "question": "Larkspur playbook?", "intent": "how_to", "slots": {"brand": "Larkspur"}}]
    llm = ScriptedLLM([[tool_use("classify", {"intent": "diagnose"})], [tool_use("classify", {"intent": "how_to"})]])
    rows = evaluate(qs, llm, NAMES)
    assert [(r["model_slots_ok"], r["slots_ok"]) for r in rows] == [(False, True), (False, False)]   # how_to: no backfill
    s = summarise(rows)
    assert (s["model_slot_accuracy"], s["slot_accuracy"]) == (0.0, 0.5)


def test_the_evaluation_needs_a_key(monkeypatch):
    from evals import routing
    monkeypatch.setattr("shared.envfile.load_dotenv", lambda *a: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert routing.main([]) == 2


# ======================= the HTTP API (Postgres) =======================
def _have_pg():
    from shared import db
    try:
        db.connect().close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _have_pg(), reason="Postgres not reachable (docker compose up -d postgres)")
def test_the_api_runs_router_turns_and_audits_the_trace(env, monkeypatch):
    from fastapi.testclient import TestClient

    from services.diagnostic_api.app import create_app
    from services.diagnostic_api.sessions import SessionStore
    from shared import db
    monkeypatch.setenv("DIAGNOSTIC_SCHEMA", "diagnostic_router_test")
    monkeypatch.setenv("DEFAULT_WEEK", WEEK)
    monkeypatch.delenv("USER_REGIONS", raising=False)
    with db.connect() as c:
        c.execute("DROP SCHEMA IF EXISTS diagnostic_router_test CASCADE")

    class FakeRuns:
        def latest(self, week):
            return {"run_id": env["run"], "status": "SUCCEEDED", "finished_at": None, "data_version": f"{env['run']}@t0"}

    skus = broken(env)
    app = create_app(SessionStore(), lambda week: scripted({"intent": "diagnose", "skus": skus}, "found it"),
                     lambda user, regions: env["ctx"], runs=FakeRuns(), mode="router")
    try:
        with TestClient(app) as client:
            sid = client.post("/sessions", headers={"X-User": "alice"}).json()["session_id"]
            r = client.post(f"/sessions/{sid}/messages", json={"text": "why?"}, headers={"X-User": "alice"}).json()
            assert (r["answer"], r["intent"], r["route"], r["unverified"]) == ("found it", "diagnose", "workflow", [])
            assert [c["tool"] for c in r["tool_calls"]] == ["diagnose_batch", "get_sap_conditions", "get_api_log"]
            with db.connect() as c:
                t = c.execute("SELECT trace, messages FROM diagnostic_router_test.turn_transcripts").fetchone()
            assert t["trace"]["classified"]["intent"] == "diagnose" and len(t["trace"]["steps"]) == 3
            assert [m["role"] for m in t["messages"]] == ["user", "assistant"]
    finally:
        with db.connect() as c:
            c.execute("DROP SCHEMA IF EXISTS diagnostic_router_test CASCADE")
