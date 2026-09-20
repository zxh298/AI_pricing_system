"""SAP -> warehouse ingestion (stands in for the weekly Airflow ingestion).

    python -m jobs.sap_ingest --week 2026-W39      # one week
    python -m jobs.sap_ingest --all-weeks          # 2026-W34 .. 2026-W39

Calls GET /clearance/candidates on SAP and (re)writes that week's rows in the warehouse tables
clearance_candidates and business_rules. Rows without a rule stay in candidates only.
"""
from __future__ import annotations

import argparse

import duckdb
import httpx

from shared.config import load_config

ALL_WEEKS = [f"2026-W{n}" for n in range(34, 40)]      # weeks SAP holds a clearance list for
CAND_COLS = ["week", "sku", "pack_type", "pack_qty", "region", "shelf_price", "week_no", "current_price"]
RULE_COLS = ["week", "sku", "pack_type", "pack_qty", "price_floor", "max_markdown_pct",
             "max_clearance_weeks", "rule_version"]

DDL = """
CREATE TABLE IF NOT EXISTS clearance_candidates (
    week VARCHAR, sku VARCHAR, pack_type VARCHAR, pack_qty BIGINT, region VARCHAR,
    shelf_price DOUBLE, week_no BIGINT, current_price DOUBLE);
CREATE TABLE IF NOT EXISTS business_rules (
    week VARCHAR, sku VARCHAR, pack_type VARCHAR, pack_qty BIGINT,
    price_floor DOUBLE, max_markdown_pct DOUBLE, max_clearance_weeks BIGINT, rule_version VARCHAR);
"""


def fetch_candidates(client: httpx.Client, week: str) -> list[dict]:
    r = client.get("/clearance/candidates", params={"week": week})
    r.raise_for_status()
    return r.json()["items"]


def ingest(client: httpx.Client, week: str, duckdb_path: str) -> dict[str, int]:
    items = fetch_candidates(client, week)
    cands = [tuple(i[c] for c in CAND_COLS) for i in items]
    rules = sorted({tuple(i[c] for c in RULE_COLS) for i in items if i["price_floor"] is not None})
    con = duckdb.connect(duckdb_path)                     # single writer; short-lived
    try:
        con.execute(DDL)
        con.execute("DELETE FROM clearance_candidates WHERE week = ?", [week])
        con.execute("DELETE FROM business_rules WHERE week = ?", [week])
        if cands:
            con.executemany(f"INSERT INTO clearance_candidates VALUES ({','.join('?' * len(CAND_COLS))})", cands)
        if rules:
            con.executemany(f"INSERT INTO business_rules VALUES ({','.join('?' * len(RULE_COLS))})", rules)
    finally:
        con.close()
    return {"candidates": len(cands), "rules": len(rules), "candidates_without_rule": len(cands) - sum(
        1 for i in items if i["price_floor"] is not None)}


def sap_client() -> httpx.Client:
    cfg = load_config()
    return httpx.Client(base_url=cfg.sap_base_url, headers={"X-API-Key": cfg.sap_read_key}, timeout=30)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", default="2026-W39")
    ap.add_argument("--all-weeks", action="store_true")
    args = ap.parse_args()
    weeks = ALL_WEEKS if args.all_weeks else [args.week]
    path = load_config().duckdb_path
    with sap_client() as c:
        for w in weeks:
            print(w, ingest(c, w, path))
