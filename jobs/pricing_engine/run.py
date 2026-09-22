"""Run the pricing engine for a week.

    python -m jobs.pricing_engine --week 2026-W39 [--run-id RUN] [--no-send] [--batch-size 50]

1. price every candidate row (shared/pricing_rules.py), skipped rows get a reason
2. save all rows to the warehouse table price_recommendations
3. POST the priced rows to SAP (write key) and store SAP's per-record result on the table
"""
from __future__ import annotations

import argparse
from collections import Counter

import httpx

from jobs.pricing_engine.engine import price_week, record_send_results, save_recommendations
from jobs.pricing_engine.sender import sap_write_client, send, to_payload
from shared.config import load_config
from shared.warehouse import WarehouseClient, get_warehouse


def run(week: str, run_id: str, wh: WarehouseClient, duckdb_path: str,
        client: httpx.Client | None = None, batch_size: int = 50) -> tuple[list[dict], list[dict]]:
    rows = price_week(week, wh)
    save_recommendations(duckdb_path, run_id, week, rows)
    results: list[dict] = []
    if client is not None:
        priced = [r for r in rows if r["status"] == "PRICED"]
        results = send(client, week, run_id, to_payload(priced, week), batch_size)
        record_send_results(duckdb_path, run_id, week, results)
    return rows, results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", default="2026-W39")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--no-send", action="store_true", help="price and save, do not POST to SAP")
    ap.add_argument("--batch-size", type=int, default=50)
    a = ap.parse_args()
    run_id = a.run_id or f"{a.week}-engine"
    cfg = load_config()
    client = None if a.no_send else sap_write_client()
    try:
        rows, results = run(a.week, run_id, get_warehouse(), cfg.duckdb_path, client, a.batch_size)
    finally:
        if client:
            client.close()
    print(f"run {run_id}: {len(rows)} rows")
    print("  status/reason :", dict(Counter((r["status"], r["reason"]) for r in rows)))
    print("  warnings      :", dict(Counter(r["warning"] for r in rows if r["warning"])))
    if results:
        print("  SAP results   :", dict(Counter((r["status"], r["code"]) for r in results)))


if __name__ == "__main__":
    main()
