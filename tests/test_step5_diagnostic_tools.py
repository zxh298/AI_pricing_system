"""Step 5, slice 1: deterministic diagnostic tools (no LLM, no sessions).

Unit tests need nothing: the `env` fixture (tests/conftest.py) builds DuckDB from the seeded generator, runs
the engine for real, and serves a planted scenario (VIC promotion override + rejected records) from a fake
read-only SAP. The mock-sap tests need the Postgres container (docker compose up -d postgres).
"""
import dataclasses
import inspect
import json
import logging
import os
import shutil

import duckdb
import httpx
import pytest

os.environ["SAP_SCHEMA"] = "sap_test"
os.environ["SAP_READ_KEY"], os.environ["SAP_WRITE_KEY"] = "rk", "wk"

from jobs.data_gen.generate import WEEK
from services.diagnostic_api import tools
from services.diagnostic_api.tools import TOOL_SCHEMAS, TOOLS, ToolContext, execute_tool
from shared import db
from shared.warehouse import DuckDBWarehouse


def call(ctx, name, **args):
    result, is_error = execute_tool(name, args, ctx)
    assert not is_error, result
    return result


def tampered(env, tmp_path, sql):
    """A copy of the warehouse with one edit to price_recommendations, and a context reading it."""
    path = str(tmp_path / "t.duckdb")
    shutil.copy(env["path"], path)
    con = duckdb.connect(path)
    con.execute(sql)
    con.close()
    return ToolContext("tester", DuckDBWarehouse(path), env["sap"])


# ======================= diagnose_batch: the canonical "VIC broken, NSW fine" scenario =======================
def test_diagnose_batch_finds_the_planted_issues(env):
    d = call(env["ctx"], "diagnose_batch", week=WEEK, skus=env["skus"])
    assert d["run_id"] == env["run"] and d["regions"] == ["NSW", "QLD", "VIC"]
    assert sum(d["totals"].values()) == 138                                  # every candidate row gets a verdict
    assert d["by_region"]["VIC"]["NOT_EFFECTIVE"] == 3 and d["by_region"]["VIC"]["API_REJECTED"] == 2
    for region in ("NSW", "QLD"):
        assert set(d["by_region"][region]) == {"OK", "MISSING_PRICE"}       # only engine-skipped items are missing
    assert d["totals"]["OK"] == 126 - 5 and d["totals"]["MISSING_PRICE"] == 12
    pats = {(p["region"], p["code"], p["reason"]): p for p in d["patterns"]}
    assert pats[("VIC", "NOT_EFFECTIVE", "PROMOTION")]["count"] == 3
    assert pats[("VIC", "API_REJECTED", "VALIDITY_OVERLAP")]["count"] == 2
    assert {s for p in d["patterns"] for s in p["sample_skus"] if p["code"] == "NOT_EFFECTIVE"} \
        == {k[0] for k in env["promo"]}


def test_diagnose_batch_can_be_narrowed_to_regions(env):
    d = call(env["ctx"], "diagnose_batch", week=WEEK, skus=env["skus"], regions=["NSW"])
    assert d["regions"] == ["NSW"] and set(d["by_region"]) == {"NSW"} and "NOT_EFFECTIVE" not in d["totals"]


def test_missing_price_reasons_are_the_engine_skip_reasons(env):
    d = call(env["ctx"], "diagnose_batch", week=WEEK, skus=env["skus"], regions=["NSW"])
    reasons = {p["reason"]: p["count"] for p in d["patterns"] if p["code"] == "MISSING_PRICE"}
    assert reasons == {"NOT_IN_PRODUCTS": 1, "MISSING_RULE": 2, "FLOOR_ABOVE_PRICE": 1}


def test_sku_not_on_the_list(env):
    d = call(env["ctx"], "diagnose_batch", week=WEEK, skus=["no-such-sku"])
    assert d["totals"] == {"NOT_IN_LIST": 3}


def test_price_never_sent_is_missing_price(env, tmp_path):
    ctx = tampered(env, tmp_path, "UPDATE price_recommendations SET sap_status = NULL, sap_code = NULL "
                                  "WHERE region = 'NSW'")
    d = call(ctx, "diagnose_batch", week=WEEK, skus=env["skus"], regions=["NSW"])
    assert {(p["code"], p["reason"]) for p in d["patterns"]} >= {("MISSING_PRICE", "NOT_SENT")}


def test_rule_violation_beats_everything_else(env, tmp_path):
    sku = next(r["sku"] for r in env["rows"] if r["status"] == "PRICED" and r["region"] == "NSW" and r["pack_qty"] == 1)
    ctx = tampered(env, tmp_path, f"UPDATE price_recommendations SET recommended_price = 0.01 "
                                  f"WHERE sku = '{sku}' AND pack_qty = 1 AND region = 'NSW'")
    d = call(ctx, "diagnose_batch", week=WEEK, skus=[sku], regions=["NSW"])
    assert d["totals"]["RULE_VIOLATION"] == 1                                # the sku's other pack sizes stay OK
    assert [(p["code"], p["count"]) for p in d["patterns"]] == [("RULE_VIOLATION", 1)]
    assert "BELOW_FLOOR" in d["patterns"][0]["reason"]


def test_no_pipeline_run_yet_means_missing_price_not_a_crash(env, tmp_path):
    ctx = tampered(env, tmp_path, "DROP TABLE price_recommendations")
    d = call(ctx, "diagnose_batch", week=WEEK, skus=env["skus"], regions=["NSW"])
    assert d["run_id"] is None and d["totals"] == {"MISSING_PRICE": 46}
    assert d["patterns"][0]["reason"] == "NO_RECOMMENDATION"


# ======================= which pipeline run is diagnosed =======================
def two_runs(env, tmp_path):
    """run-1 plus an older run whose id sorts later ('zzz-old', nothing sent), as in a warehouse with many runs."""
    return tampered(env, tmp_path, "INSERT INTO price_recommendations "
                                   "SELECT * REPLACE ('zzz-old' AS run_id, NULL AS sap_status) FROM price_recommendations")


def test_unpinned_default_is_the_alphabetically_last_run(env, tmp_path):
    d = call(two_runs(env, tmp_path), "diagnose_batch", week=WEEK, skus=env["skus"], regions=["VIC"])
    assert d["run_id"] == "zzz-old" and "NOT_SENT" in {p["reason"] for p in d["patterns"]}


def test_a_pinned_run_wins_and_the_model_cannot_override_it(env, tmp_path):
    ctx = dataclasses.replace(two_runs(env, tmp_path), run_id=env["run"])
    d = call(ctx, "diagnose_batch", week=WEEK, skus=env["skus"], regions=["VIC"])
    assert d["run_id"] == env["run"] and d["by_region"]["VIC"]["NOT_EFFECTIVE"] == 3
    d = call(ctx, "diagnose_batch", week=WEEK, skus=env["skus"], regions=["VIC"], run_id="zzz-old")
    assert d["run_id"] == env["run"]                                         # the pin beats the model's argument
    assert call(ctx, "check_rules", week=WEEK, skus=env["skus"], run_id="zzz-old")["run_id"] == env["run"]


def test_without_a_pin_the_model_may_name_a_run(env, tmp_path):
    d = call(two_runs(env, tmp_path), "diagnose_batch", week=WEEK, skus=env["skus"], regions=["VIC"], run_id=env["run"])
    assert d["run_id"] == env["run"]


def test_the_api_log_is_filtered_to_the_pinned_run(env):
    asked = []

    def handler(req):
        asked.append(dict(req.url.params))
        return httpx.Response(200, json={"items": []})
    ctx = ToolContext("t", env["wh"], httpx.Client(base_url="http://sap", transport=httpx.MockTransport(handler)),
                      run_id=env["run"])
    call(ctx, "get_api_log", week=WEEK, skus=env["skus"], run_id="something-else")
    assert asked == [{"week": WEEK, "run_id": env["run"]}]


# ======================= the other tools =======================
def test_resolve_products_by_brand(env):
    cands = env["wh"].get_candidates(WEEK)
    brand = next(c["brand"] for c in cands if c["name"] is not None)
    expect = sorted({c["sku"] for c in cands if c["name"] is not None and c["brand"] == brand})
    r = call(env["ctx"], "resolve_products", week=WEEK, brand=brand.upper())        # case-insensitive
    assert r["skus"] == expect and r["count"] == len(expect) and not r["truncated"]
    assert call(env["ctx"], "resolve_products", week=WEEK, brand="Nonexistent")["count"] == 0


def test_get_sap_conditions_lists_only_overrides(env):
    r = call(env["ctx"], "get_sap_conditions", week=WEEK, skus=env["skus"])
    assert r["conditions_found"] == 126 - 2                                          # 2 rejected are not held
    assert {(o["region"], o["effective_source"]) for o in r["overridden"]} == {("VIC", "PROMOTION")}
    assert len(r["overridden"]) == 3 and all(o["effective_price"] < o["clearance_price_sent"] for o in r["overridden"])
    assert r["by_effective_source"]["VIC:PROMOTION"] == 3


def test_condition_fields_cannot_be_misread(env):
    r = call(env["ctx"], "get_sap_conditions", week=WEEK, skus=env["skus"])
    for o in r["overridden"]:
        assert set(o) == {"sku", "pack_qty", "region", "clearance_price_sent", "effective_price", "effective_source",
                          "markdown_valid_from", "markdown_valid_to"}     # no bare "price" or "valid_*" to guess about
    assert set(r["fields"]) == {"clearance_price_sent", "effective_price", "effective_source",
                                "markdown_valid_from / markdown_valid_to"}
    assert "not the shelf" in r["fields"]["clearance_price_sent"] and "not promotion dates" in r["fields"][
        "markdown_valid_from / markdown_valid_to"]


def test_get_api_log_groups_problems(env):
    r = call(env["ctx"], "get_api_log", week=WEEK, skus=env["skus"])
    assert r["problems"] == 2 and r["records_logged"] == 126
    assert r["patterns"] == [{"region": "VIC", "status": "REJECTED", "code": "VALIDITY_OVERLAP",
                              "message": env["overlap_msg"], "count": 2,
                              "sample_skus": sorted(k[0] for k in env["rejected"])}]


def test_get_api_log_keeps_the_latest_answer_per_record(env):
    # a replay that succeeded after a rejection must not still show as a problem
    def handler(req):
        rec = {"run_id": env["run"], "week": WEEK, "sku": "1", "pack_qty": "1", "region": "VIC", "message": ""}
        return httpx.Response(200, json={"items": [{"id": 1, **rec, "status": "REJECTED", "code": "X"},
                                                   {"id": 2, **rec, "status": "ACCEPTED", "code": "OK"}]})
    ctx = ToolContext("t", env["wh"], httpx.Client(base_url="http://sap", transport=httpx.MockTransport(handler)))
    assert call(ctx, "get_api_log", week=WEEK, skus=["1"])["problems"] == 0


def test_check_rules_clean_and_tampered(env, tmp_path):
    r = call(env["ctx"], "check_rules", week=WEEK, skus=env["skus"])
    assert r["violations"] == 0 and r["priced_checked"] == 126 and r["patterns"] == []
    ctx = tampered(env, tmp_path, "UPDATE price_recommendations SET recommended_price = recommended_price * 3 "
                                  "WHERE status = 'PRICED' AND region = 'QLD'")
    r = call(ctx, "check_rules", week=WEEK, skus=env["skus"])
    assert r["violations"] > 0 and {p["region"] for p in r["patterns"]} == {"QLD"}
    assert "PRICE_ABOVE_LAST_WEEK" in {p["code"] for p in r["patterns"]}


def test_search_docs_is_a_stub_until_rag_exists(env):
    assert call(env["ctx"], "search_docs", query="promotion override")["results"] == []


# ======================= dispatch, permissions, audit =======================
def test_every_schema_has_a_tool_and_vice_versa():
    assert {s["name"] for s in TOOL_SCHEMAS} == set(TOOLS)
    for s in TOOL_SCHEMAS:
        props = s["input_schema"]["properties"]
        assert set(s["input_schema"]["required"]) <= set(props)
        assert set(inspect.signature(TOOLS[s["name"]]).parameters) - {"ctx"} == set(props)


@pytest.mark.parametrize("name,args", [
    ("delete_prices", {}),                                                      # not on the allow-list
    ("diagnose_batch", {"week": "W39", "skus": ["1"]}),                         # bad week
    ("diagnose_batch", {"week": WEEK, "skus": []}),                             # empty scope
    ("diagnose_batch", {"week": WEEK, "skus": "1"}),                            # wrong type
    ("diagnose_batch", {"week": WEEK, "skus": [str(i) for i in range(501)]}),   # too many
    ("diagnose_batch", {"week": WEEK, "skus": ["1"], "regions": ["MOON"]}),     # unknown region
    ("diagnose_batch", {"week": WEEK, "skus": ["1"], "sql": "DROP TABLE x"}),   # unexpected argument
    ("diagnose_batch", {"week": WEEK, "skus": ["1"], "ctx": None}),             # cannot override injected context
])
def test_bad_calls_come_back_as_is_error_results(env, name, args):
    result, is_error = execute_tool(name, args, env["ctx"])
    assert is_error and isinstance(result["error"], str)


def test_sap_outage_is_an_error_result(env):
    down = httpx.Client(base_url="http://sap", transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    result, is_error = execute_tool("diagnose_batch", {"week": WEEK, "skus": env["skus"]},
                                    ToolContext("t", env["wh"], down))
    assert is_error and "SAP request failed" in result["error"]


def test_region_permissions_come_from_the_injected_context(env):
    nsw_only = ToolContext("nsw-user", env["wh"], env["sap"], frozenset({"NSW"}))
    d = call(nsw_only, "diagnose_batch", week=WEEK, skus=env["skus"])
    assert d["regions"] == ["NSW"] and "NOT_EFFECTIVE" not in d["totals"]
    result, is_error = execute_tool("diagnose_batch", {"week": WEEK, "skus": env["skus"], "regions": ["VIC"]}, nsw_only)
    assert is_error and "VIC" in result["error"]
    assert call(nsw_only, "resolve_products", week=WEEK)["regions"] == ["NSW"]


def test_every_call_writes_one_audit_line(env, caplog):
    with caplog.at_level(logging.INFO, logger="diagnostic.audit"):
        execute_tool("check_rules", {"week": WEEK, "skus": env["skus"]}, env["ctx"])
        execute_tool("delete_prices", {}, env["ctx"])
    lines = [json.loads(r.message) for r in caplog.records]
    assert [(x["user"], x["tool"], x["is_error"]) for x in lines] == [("tester", "check_rules", False),
                                                                      ("tester", "delete_prices", True)]


def test_tools_cannot_write_to_sap(env):
    src = inspect.getsource(tools)
    assert "sap_write_key" not in src and ".post(" not in src and ".put(" not in src and ".delete(" not in src
    seen = []
    real = env["sap"].send
    env["sap"].send = lambda req, **kw: (seen.append(req.method), real(req, **kw))[1]
    try:
        for name, args in [("diagnose_batch", {}), ("get_sap_conditions", {}), ("get_api_log", {}), ("check_rules", {})]:
            execute_tool(name, {"week": WEEK, "skus": env["skus"]} | args, env["ctx"])
    finally:
        del env["sap"].send
    assert seen and set(seen) == {"GET"}


# ======================= mock-sap: read key cannot write, submissions endpoint (Postgres) =======================
try:
    db.connect().close()
    HAVE_PG = True
except Exception:
    HAVE_PG = False
pg = pytest.mark.skipif(not HAVE_PG, reason="Postgres not reachable (docker compose up -d postgres)")


@pytest.fixture
def sap_app():
    from fastapi.testclient import TestClient

    from jobs.data_gen.generate import SAP_TABLES, build
    from jobs.data_gen.seed_sap import seed_sap
    from services.mock_sap import main as sapmain
    sapmain.reset_faults()
    seed_sap({k: v for k, v in build(1).items() if k in SAP_TABLES})
    with TestClient(sapmain.app) as client:
        yield client


def _first_item(client):
    return client.get("/clearance/candidates", params={"week": WEEK}, headers={"X-API-Key": "rk"}).json()["items"][0]


@pg
def test_read_key_is_refused_on_post_but_allowed_on_get(sap_app):
    item = {"sku": "1", "pack_qty": 1, "region": "VIC", "markdown_price": 9.99,
            "valid_from": "2026-09-21", "valid_to": "2026-09-27"}
    r = sap_app.post("/pricing/markdown-prices", json={"run_id": "x", "week": WEEK, "prices": [item]},
                     headers={"X-API-Key": "rk"})
    assert r.status_code == 403
    for path in ("/pricing/conditions", "/pricing/submissions"):
        assert sap_app.get(path, params={"week": WEEK}, headers={"X-API-Key": "rk"}).status_code == 200
        assert sap_app.get(path, params={"week": WEEK}).status_code == 401


@pg
def test_submissions_endpoint_returns_the_log_oldest_first_with_filters(sap_app):
    c = _first_item(sap_app)
    good = {"sku": c["sku"], "pack_qty": c["pack_qty"], "region": c["region"], "markdown_price": 1.00,
            "valid_from": "2026-09-21", "valid_to": "2026-09-27"}
    bad = good | {"sku": "not-on-the-list"}
    w = {"X-API-Key": "wk"}
    sap_app.post("/pricing/markdown-prices", json={"run_id": "r1", "week": WEEK, "prices": [good, bad]}, headers=w)
    sap_app.post("/pricing/markdown-prices", json={"run_id": "r1", "week": WEEK, "prices": [good]}, headers=w)   # replay
    sap_app.post("/pricing/markdown-prices", json={"run_id": "r2", "week": WEEK, "prices": [good]}, headers=w)   # other run
    r = {"X-API-Key": "rk"}
    items = sap_app.get("/pricing/submissions", params={"week": WEEK}, headers=r).json()["items"]
    assert [(i["run_id"], i["status"], i["code"], i["duplicate"]) for i in items] == [
        ("r1", "ACCEPTED", "OK", False), ("r1", "REJECTED", "NOT_ON_LIST", False),
        ("r1", "ACCEPTED", "OK", True),                                     # same run replayed: idempotent
        ("r2", "REJECTED", "VALIDITY_OVERLAP", False)]                      # another run overlaps r1's condition
    assert [i["id"] for i in items] == sorted(i["id"] for i in items)
    only = sap_app.get("/pricing/submissions", params={"week": WEEK, "run_id": "r1", "status": "REJECTED"}, headers=r)
    assert [i["sku"] for i in only.json()["items"]] == ["not-on-the-list"]
    r2 = sap_app.get("/pricing/submissions", params={"week": WEEK, "run_id": "r2"}, headers=r).json()
    assert r2["count"] == 1 and r2["items"][0]["code"] == "VALIDITY_OVERLAP"
