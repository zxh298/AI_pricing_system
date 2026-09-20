"""Throwaway stub sender (replaced by the real pricing engine + sender in step 3).

    python -m jobs.stub_sender --week 2026-W39 [--run-id RUN] [--discount 0.10] [--batch-size 50]

Reads the ingested candidates/rules from the warehouse, prices each row at
current_price * (1 - discount) clamped to the rules, and POSTs them to SAP in batches.
Singles are priced before multipacks: a pack's per-item price must not go below its Single's.
Rows that cannot be priced are skipped and reported (no rule, floor above current price,
week_no beyond max_clearance_weeks).
"""
from __future__ import annotations

import argparse
import math
from datetime import date, timedelta

import httpx

from shared.config import load_config
from shared.warehouse import get_warehouse


def week_dates(week: str) -> tuple[date, date]:
    y, w = week.split("-W")
    monday = date.fromisocalendar(int(y), int(w), 1)
    return monday, monday + timedelta(days=6)


def _ceil_cents(x: float) -> float:
    return math.ceil(round(x * 100, 6)) / 100


def build_prices(week: str, discount: float, wh=None) -> tuple[list[dict], list[dict]]:
    """Returns (price records, skipped rows with reason)."""
    wh = wh or get_warehouse()
    rules = {(r["sku"], r["pack_qty"]): r for r in wh.get_rules(week)}
    valid_from, valid_to = week_dates(week)
    priced: dict[tuple, float] = {}
    out, skipped = [], []
    cands = sorted(wh.get_candidates(week), key=lambda c: (c["pack_qty"] > 1, c["sku"], c["pack_qty"], c["region"]))
    for c in cands:
        key, rule = (c["sku"], c["pack_qty"]), rules.get((c["sku"], c["pack_qty"]))
        ident = {"sku": c["sku"], "pack_qty": c["pack_qty"], "region": c["region"]}
        if rule is None:
            skipped.append({**ident, "reason": "MISSING_RULE"})
            continue
        if c["week_no"] > rule["max_clearance_weeks"]:        # SAP should have stopped listing it
            skipped.append({**ident, "reason": "EXCEEDED_MAX_WEEKS"})
            continue
        # never below 50% off shelf price (max_markdown_pct) and never below the SAP floor
        lo = max(rule["price_floor"], c["shelf_price"] * (1 - rule["max_markdown_pct"]))
        if c["pack_qty"] > 1:
            single = priced.get(((c["sku"], 1), c["region"]), c["current_price"] / c["pack_qty"])
            lo = max(lo, round(c["pack_qty"] * single, 2))
        lo = _ceil_cents(lo)
        if lo > c["current_price"]:
            skipped.append({**ident, "reason": "FLOOR_ABOVE_PRICE"})
            continue
        price = min(c["current_price"], max(round(c["current_price"] * (1 - discount), 2), lo))
        priced[((c["sku"], c["pack_qty"]), c["region"])] = price
        out.append({**ident, "markdown_price": price,
                    "valid_from": valid_from.isoformat(), "valid_to": valid_to.isoformat()})
    return out, skipped


def send(client: httpx.Client, week: str, run_id: str, prices: list[dict], batch_size: int = 50) -> list[dict]:
    results = []
    for i in range(0, len(prices), batch_size):
        r = client.post("/pricing/markdown-prices",
                        json={"run_id": run_id, "week": week, "prices": prices[i:i + batch_size]})
        r.raise_for_status()
        results.extend(r.json()["results"])
    return results


def sap_write_client() -> httpx.Client:
    cfg = load_config()
    return httpx.Client(base_url=cfg.sap_base_url, headers={"X-API-Key": cfg.sap_write_key}, timeout=30)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", default="2026-W39")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--discount", type=float, default=0.10)
    ap.add_argument("--batch-size", type=int, default=50)
    a = ap.parse_args()
    run_id = a.run_id or f"{a.week}-stub"
    prices, skipped = build_prices(a.week, a.discount)
    print(f"{len(prices)} prices built, {len(skipped)} skipped: {skipped}")
    with sap_write_client() as c:
        res = send(c, a.week, run_id, prices, a.batch_size)
    from collections import Counter
    print("results:", dict(Counter((r["status"], r["code"]) for r in res)))
