"""Record the week's outcome in the warehouse history (price_history), after reconciliation.

price_history holds one row per (sku, pack_qty, region, week). For the new week:
    price          SAP's effective price when SAP holds a condition for the item (a promotion
                   override is recorded as the promotion price); otherwise last week's price
                   carried forward (item not listed, skipped, rejected or errored)
    is_clearance   the item is on this week's candidate list; week_no comes from that list
Rerunning replaces the week's rows; every other week is untouched.
"""
from __future__ import annotations

import duckdb

INSERT = """
INSERT INTO price_history
SELECT ?, p.sku, p.pack_type, p.pack_qty, r.region, p.shelf_price,
       COALESCE(e.price, prev.price, p.shelf_price) AS price,
       (c.sku IS NOT NULL) AS is_clearance, c.week_no
FROM products p
CROSS JOIN (SELECT DISTINCT region FROM stores) r
LEFT JOIN _eff e ON e.sku = p.sku AND e.pack_qty = p.pack_qty AND e.region = r.region
LEFT JOIN (SELECT sku, pack_qty, region, price FROM price_history
           WHERE week = (SELECT max(week) FROM price_history WHERE week < ?)) prev
       ON prev.sku = p.sku AND prev.pack_qty = p.pack_qty AND prev.region = r.region
LEFT JOIN (SELECT DISTINCT sku, pack_qty, region, week_no FROM clearance_candidates WHERE week = ?) c
       ON c.sku = p.sku AND c.pack_qty = p.pack_qty AND c.region = r.region
"""


def record_history(path: str, week: str, conditions: list[dict]) -> dict:
    """conditions: items from SAP's GET /pricing/conditions for the week (effective_price is used)."""
    con = duckdb.connect(path)
    try:
        con.execute("CREATE OR REPLACE TEMP TABLE _eff (sku VARCHAR, pack_qty BIGINT, region VARCHAR, price DOUBLE)")
        if conditions:
            con.executemany("INSERT INTO _eff VALUES (?,?,?,?)",
                            [(c["sku"], c["pack_qty"], c["region"], c["effective_price"]) for c in conditions])
        con.execute("DELETE FROM price_history WHERE week = ?", [week])
        con.execute(INSERT, [week, week, week])
        rows, from_sap = con.execute(
            "SELECT count(*), count(e.sku) FROM price_history h "
            "LEFT JOIN _eff e ON e.sku = h.sku AND e.pack_qty = h.pack_qty AND e.region = h.region "
            "WHERE h.week = ?", [week]).fetchone()
        con.execute("UPDATE weeks SET status = 'published' WHERE week = ?", [week])
        return {"week": week, "rows": rows, "from_sap": from_sap, "carried_forward": rows - from_sap}
    finally:
        con.close()
