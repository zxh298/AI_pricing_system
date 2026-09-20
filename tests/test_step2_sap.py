"""Step 2 smoke tests: mock-sap endpoints, SAP->DuckDB ingest, stub sender.

Needs the Postgres container (docker compose up -d postgres); skipped otherwise.
Uses a throwaway schema (sap_test) so the real `sap` schema is untouched.
"""
import os

import duckdb
import pytest
from fastapi.testclient import TestClient

from jobs.data_gen.generate import SAP_TABLES, WEEK, build, write
from shared import db
from shared.warehouse import DuckDBWarehouse

os.environ["SAP_SCHEMA"] = "sap_test"
os.environ["SAP_READ_KEY"], os.environ["SAP_WRITE_KEY"] = "rk", "wk"

try:
    db.connect().close()
    HAVE_PG = True
except Exception:
    HAVE_PG = False
pytestmark = pytest.mark.skipif(not HAVE_PG, reason="Postgres not reachable (docker compose up -d postgres)")


@pytest.fixture(scope="module")
def sap():
    from jobs.data_gen.seed_sap import seed_sap
    from services.mock_sap.main import app
    seed_sap({k: v for k, v in build(1).items() if k in SAP_TABLES})
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_conditions():
    """Each test starts with no received prices (candidates/rules stay seeded)."""
    from shared.sap_schema import schema
    with db.connect() as c:
        c.execute(f"CREATE SCHEMA IF NOT EXISTS {schema()}")
        exists = c.execute("SELECT to_regclass(%s) AS t", (f"{schema()}.price_conditions",)).fetchone()["t"]
        if exists:
            c.execute(f"TRUNCATE {schema()}.price_conditions")


def hdr(k):
    return {"X-API-Key": k}


def rec(sku, qty, region, price, vf="2026-09-21", vt="2026-09-27"):
    return {"sku": sku, "pack_qty": qty, "region": region, "markdown_price": price, "valid_from": vf, "valid_to": vt}


def test_auth(sap):
    assert sap.get("/clearance/candidates", params={"week": WEEK}).status_code == 401
    assert sap.get("/clearance/candidates", params={"week": WEEK}, headers=hdr("bad")).status_code == 401
    assert sap.get("/clearance/candidates", params={"week": WEEK}, headers=hdr("rk")).status_code == 200
    body = {"run_id": "r", "week": WEEK, "prices": []}
    assert sap.post("/pricing/markdown-prices", json=body, headers=hdr("rk")).status_code == 403   # read key cannot write
    assert sap.post("/pricing/markdown-prices", json=body).status_code == 401
    assert sap.post("/pricing/markdown-prices", json=body, headers=hdr("wk")).status_code == 200


def test_candidates_with_rules(sap):
    r = sap.get("/clearance/candidates", params={"week": WEEK}, headers=hdr("rk")).json()
    assert r["count"] == 138 and r["items"][0].keys() >= {"sku", "pack_type", "pack_qty", "region", "current_price",
                                                        "price_floor", "max_markdown_pct", "rule_version",
                                                        "shelf_price", "week_no", "max_clearance_weeks"}
    ruled = [i for i in r["items"] if i["price_floor"] is not None]
    assert all(i["max_markdown_pct"] == 0.5 and i["max_clearance_weeks"] == 20 for i in ruled)
    assert all(1 <= i["week_no"] <= 20 and i["shelf_price"] >= i["current_price"] for i in r["items"])
    assert sum(i["price_floor"] is None for i in r["items"]) == 3 * 2 + 3      # 2 missing rules + orphan, x3 regions
    assert sap.get("/clearance/candidates", params={"week": "2030-W01"}, headers=hdr("rk")).status_code == 404


def test_post_per_record_results_idempotency_overlap(sap):
    items = sap.get("/clearance/candidates", params={"week": WEEK}, headers=hdr("rk")).json()["items"]
    a, b = [i for i in items if i["price_floor"] is not None][:2]
    batch = [rec(a["sku"], a["pack_qty"], a["region"], round(a["current_price"] * 0.9, 2)),
             rec(b["sku"], b["pack_qty"], b["region"], -1.0),                       # bad format
             rec("000000", 1, "VIC", 9.99),                                          # not on list
             {"sku": "x"}]                                                           # missing fields
    body = {"run_id": "run-1", "week": WEEK, "prices": batch}
    r = sap.post("/pricing/markdown-prices", json=body, headers=hdr("wk")).json()
    assert [x["code"] for x in r["results"]] == ["OK", "BAD_FORMAT", "NOT_ON_LIST", "BAD_FORMAT"]
    assert r["summary"] == {"received": 4, "accepted": 1, "rejected": 3, "duplicates": 0}      # partial failure

    again = sap.post("/pricing/markdown-prices", json=body, headers=hdr("wk")).json()          # idempotent resend
    assert again["results"][0]["duplicate"] is True and again["results"][0]["status"] == "ACCEPTED"
    changed = {**body, "prices": [batch[0] | {"markdown_price": 1.23}]}
    assert sap.post("/pricing/markdown-prices", json=changed, headers=hdr("wk")).json()["results"][0]["code"] == "IDEMPOTENCY_CONFLICT"

    other = {**body, "run_id": "run-2", "prices": [batch[0]]}                                  # new run, same validity
    assert sap.post("/pricing/markdown-prices", json=other, headers=hdr("wk")).json()["results"][0]["code"] == "VALIDITY_OVERLAP"

    cond = sap.get("/pricing/conditions", params={"week": WEEK}, headers=hdr("rk")).json()
    assert cond["count"] == 1 and cond["items"][0]["run_id"] == "run-1"


def test_malformed_body(sap):
    assert sap.post("/pricing/markdown-prices", json={"nope": 1}, headers=hdr("wk")).status_code == 400


def test_ingest_and_stub_sender(sap, tmp_path):
    from jobs.sap_ingest import ingest
    from jobs.stub_sender import build_prices, send
    path = str(tmp_path / "w.duckdb")
    write(path, seed=1)
    sap.headers.update(hdr("wk"))
    counts = ingest(sap, WEEK, path)
    assert counts == {"candidates": 138, "rules": 43, "candidates_without_rule": 9}
    assert ingest(sap, WEEK, path)["candidates"] == 138                     # re-ingest replaces, no duplicates
    con = duckdb.connect(path, read_only=True)
    assert con.execute("SELECT count(*) FROM clearance_candidates").fetchone()[0] == 138
    con.close()

    wh = DuckDBWarehouse(path)
    prices, skipped = build_prices(WEEK, 0.10, wh)
    assert {s["reason"] for s in skipped} == {"MISSING_RULE", "FLOOR_ABOVE_PRICE"}
    cands = {(c["sku"], c["pack_qty"], c["region"]): c for c in wh.get_candidates(WEEK)}
    rules = {(r["sku"], r["pack_qty"]): r for r in wh.get_rules(WEEK)}
    price = {(p["sku"], p["pack_qty"], p["region"]): p["markdown_price"] for p in prices}
    for k, p in price.items():                                              # stub obeys the hard constraints
        assert p <= cands[k]["current_price"] + 1e-9                       # never above last week
        assert p >= rules[k[:2]]["price_floor"] - 1e-9
        assert p >= 0.5 * cands[k]["shelf_price"] - 0.01                   # never below 50% off shelf price
        if k[1] > 1 and (k[0], 1, k[2]) in price:                           # multipack per-item >= Single
            assert p / k[1] >= price[(k[0], 1, k[2])] - 1e-9
    results = send(sap, WEEK, "stub-run", prices, batch_size=20)
    assert len(results) == len(prices) and all(r["status"] == "ACCEPTED" for r in results)


def test_stub_sender_skips_items_past_max_weeks():
    from jobs.stub_sender import build_prices

    class FakeWH:
        def get_candidates(self, week):
            base = dict(week=week, sku="1", pack_type="Single", pack_qty=1, region="VIC",
                        shelf_price=20.0, current_price=15.0, cost=5.0)
            return [base | {"sku": "1", "week_no": 20}, base | {"sku": "2", "week_no": 21}]

        def get_rules(self, week):
            return [dict(sku=s, pack_qty=1, price_floor=6.0, max_markdown_pct=0.5, max_clearance_weeks=20)
                    for s in ("1", "2")]

    prices, skipped = build_prices(WEEK, 0.10, FakeWH())
    assert [p["sku"] for p in prices] == ["1"] and prices[0]["markdown_price"] == 13.5
    assert skipped == [{"sku": "2", "pack_qty": 1, "region": "VIC", "reason": "EXCEEDED_MAX_WEEKS"}]


def test_stub_sender_price_floor_beats_half_off():
    from jobs.stub_sender import build_prices

    class FakeWH:
        def get_candidates(self, week):
            return [dict(week=week, sku="1", pack_type="Single", pack_qty=1, region="VIC", shelf_price=20.0,
                         week_no=5, current_price=11.0, cost=8.0)]

        def get_rules(self, week):        # floor 10.50 is above 50% of shelf (10.00): floor wins
            return [dict(sku="1", pack_qty=1, price_floor=10.5, max_markdown_pct=0.5, max_clearance_weeks=20)]

    prices, _ = build_prices(WEEK, 0.50, FakeWH())
    assert prices[0]["markdown_price"] == 10.5
