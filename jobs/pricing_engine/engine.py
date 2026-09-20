"""Pricing engine: one recommended price per candidate row (sku, pack_qty, region).

Reads the ingested candidates / rules / inventory from the warehouse and applies the
deterministic core in shared/pricing_rules.py. Rows that cannot be priced safely are returned
as SKIPPED with a reason, never silently dropped. No LLM here.

Skip reasons (blocking):  NOT_IN_PRODUCTS, MISSING_RULE, EXCEEDED_MAX_WEEKS,
                          FLOOR_ABOVE_PRICE, LADDER_ABOVE_PRICE
Warnings (still priced):  ZERO_INVENTORY
"""
from __future__ import annotations

import duckdb

from shared.pricing_rules import recommend
from shared.warehouse import WarehouseClient

ROW_COLS = ["week", "sku", "pack_type", "pack_qty", "region", "week_no", "shelf_price",
            "current_price", "discount_pct", "recommended_price", "binding", "status",
            "reason", "warning"]


def price_week(week: str, wh: WarehouseClient) -> list[dict]:
    cands = wh.get_candidates(week)
    rules = {(r["sku"], r["pack_qty"]): r for r in wh.get_rules(week)}
    skus = sorted({c["sku"] for c in cands})
    on_hand = {(i["sku"], i["pack_qty"], i["region"]): i["on_hand"] for i in wh.get_inventory(skus, week)}
    current = {(c["sku"], c["pack_qty"], c["region"]): c["current_price"] for c in cands}

    # Single's latest known price: ladder reference when the Single is not on this week's list
    latest: dict[tuple, float] = {}
    pack_skus = sorted({c["sku"] for c in cands if c["pack_qty"] > 1})
    for h in wh.get_price_history(pack_skus):                     # ordered by week within key
        if h["pack_qty"] == 1:
            latest[(h["sku"], h["region"])] = h["price"]

    priced: dict[tuple, float] = {}
    rows = []
    # Singles first: a multipack's per-item price must not go below its Single's price
    for c in sorted(cands, key=lambda c: (c["pack_qty"] > 1, c["sku"], c["pack_qty"], c["region"])):
        sku, qty, region = c["sku"], c["pack_qty"], c["region"]
        row = {"week": week, "sku": sku, "pack_type": c["pack_type"], "pack_qty": qty, "region": region,
               "week_no": c["week_no"], "shelf_price": c["shelf_price"], "current_price": c["current_price"],
               "discount_pct": None, "recommended_price": None, "binding": None,
               "status": "SKIPPED", "reason": None, "warning": None}
        rule = rules.get((sku, qty))
        if c["name"] is None:
            row["reason"] = "NOT_IN_PRODUCTS"
        elif rule is None:
            row["reason"] = "MISSING_RULE"
        elif c["week_no"] > rule["max_clearance_weeks"]:
            row["reason"] = "EXCEEDED_MAX_WEEKS"
        else:
            ladder = None
            if qty > 1:
                ref = priced.get((sku, 1, region), current.get((sku, 1, region), latest.get((sku, region))))
                ladder = None if ref is None else qty * ref
            res = recommend(c["shelf_price"], c["week_no"], c["current_price"], rule["price_floor"],
                            rule["max_markdown_pct"], ladder)
            if res["error"]:
                row["reason"] = res["error"]
            else:
                price = res["price"]
                priced[(sku, qty, region)] = price
                row.update(status="PRICED", recommended_price=price, binding=res["binding"],
                           discount_pct=round(1 - price / c["shelf_price"], 4))
                if on_hand.get((sku, qty, region)) == 0:
                    row["warning"] = "ZERO_INVENTORY"
        rows.append(row)
    return rows


# ---------------- persistence: price_recommendations in the warehouse ----------------
DDL = """
CREATE TABLE IF NOT EXISTS price_recommendations (
    run_id VARCHAR, week VARCHAR, sku VARCHAR, pack_type VARCHAR, pack_qty BIGINT, region VARCHAR,
    week_no BIGINT, shelf_price DOUBLE, current_price DOUBLE, discount_pct DOUBLE,
    recommended_price DOUBLE, binding VARCHAR, status VARCHAR, reason VARCHAR, warning VARCHAR,
    sap_status VARCHAR, sap_code VARCHAR, sap_message VARCHAR)
"""


def save_recommendations(path: str, run_id: str, week: str, rows: list[dict]) -> None:
    con = duckdb.connect(path)
    try:
        con.execute(DDL)
        con.execute("DELETE FROM price_recommendations WHERE run_id = ? AND week = ?", [run_id, week])
        con.executemany(
            "INSERT INTO price_recommendations VALUES (?" + ",?" * (len(ROW_COLS) + 3) + ")",
            [(run_id, *[r[c] for c in ROW_COLS], None, None, None) for r in rows])
    finally:
        con.close()


def record_send_results(path: str, run_id: str, week: str, results: list[dict]) -> None:
    con = duckdb.connect(path)
    try:
        con.executemany(
            "UPDATE price_recommendations SET sap_status = ?, sap_code = ?, sap_message = ? "
            "WHERE run_id = ? AND week = ? AND sku = ? AND pack_qty = ? AND region = ?",
            [(r["status"], r["code"], r["message"], run_id, week, r["sku"], int(r["pack_qty"]), r["region"])
             for r in results])
    finally:
        con.close()
