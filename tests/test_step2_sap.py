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
    """Each test starts with no W39 prices received (candidates, rules and past weeks' prices stay)."""
    from shared.sap_schema import schema
    with db.connect() as c:
        c.execute(f"CREATE SCHEMA IF NOT EXISTS {schema()}")
        exists = c.execute("SELECT to_regclass(%s) AS t", (f"{schema()}.price_conditions",)).fetchone()["t"]
        if exists:
            c.execute(f"DELETE FROM {schema()}.price_conditions WHERE week = %s", (WEEK,))


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
    assert all(i["max_markdown_pct"] == 0.5 and i["max_clearance_weeks"] == 10 for i in ruled)
    assert all(1 <= i["week_no"] <= 10 and i["shelf_price"] >= i["current_price"] for i in r["items"])
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


def test_ingest_then_price_and_send(sap, tmp_path):
    from jobs.pricing_engine.run import run
    from jobs.sap_ingest import ingest
    path = str(tmp_path / "w.duckdb")
    write(path, seed=1)
    sap.headers.update(hdr("wk"))
    counts = ingest(sap, WEEK, path)
    assert counts == {"candidates": 138, "rules": 43, "candidates_without_rule": 9}
    assert ingest(sap, WEEK, path)["candidates"] == 138                     # re-ingest replaces, no duplicates
    con = duckdb.connect(path, read_only=True)
    assert con.execute("SELECT count(*) FROM clearance_candidates").fetchone()[0] == 138
    con.close()

    rows, results = run(WEEK, "run-e2e", DuckDBWarehouse(path), path, client=sap, batch_size=20)
    priced = [r for r in rows if r["status"] == "PRICED"]
    assert len(rows) == 138 and len(results) == len(priced) == 126
    assert all(r["status"] == "ACCEPTED" for r in results)
    con = duckdb.connect(path, read_only=True)
    assert con.execute("SELECT count(*) FROM price_recommendations WHERE sap_status = 'ACCEPTED'").fetchone()[0] == 126
    con.close()


def test_sap_keeps_past_weeks_and_a_full_submission_log(sap):
    from shared.sap_schema import schema
    past = sap.get("/pricing/conditions", params={"week": "2026-W38"}, headers=hdr("rk")).json()
    assert past["count"] > 0 and {i["run_id"] for i in past["items"]} == {"SEED-2026-W38"}

    items = sap.get("/clearance/candidates", params={"week": WEEK}, headers=hdr("rk")).json()["items"]
    a = next(i for i in items if i["price_floor"] is not None)
    body = {"run_id": "run-log", "week": WEEK, "prices": [
        rec(a["sku"], a["pack_qty"], a["region"], round(a["current_price"] * 0.9, 2)),
        rec(a["sku"], a["pack_qty"], a["region"], -1.0, vf="2026-09-22"),        # bad format
        {"sku": "x"}]}
    sap.post("/pricing/markdown-prices", json=body, headers=hdr("wk"))
    sap.post("/pricing/markdown-prices", json=body, headers=hdr("wk"))          # replay
    with db.connect() as c:
        log = c.execute(f"SELECT status, code, duplicate, payload FROM {schema()}.submission_log "
                        "WHERE run_id = 'run-log' ORDER BY id").fetchall()
    assert [(r["code"], r["duplicate"]) for r in log] == [
        ("OK", False), ("BAD_FORMAT", False), ("BAD_FORMAT", False),
        ("OK", True), ("BAD_FORMAT", False), ("BAD_FORMAT", False)]            # rejected + replayed kept, nothing overwritten
    assert log[0]["payload"]["sku"] == a["sku"] and log[2]["payload"] == {"sku": "x"}
