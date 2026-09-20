"""Inject SAP-side failure scenarios into mock-sap's Postgres (kept out of the default data).

    python -m jobs.data_gen.scenarios --week 2026-W39 --apply promo overlap
    python -m jobs.data_gen.scenarios --week 2026-W39 --clear

promo    A VIC-only catalogue promotion with a higher priority than the markdown: SAP accepts our
         price but the effective price is the promotion price (NOT_EFFECTIVE at reconciliation).
overlap  Last week's condition record was created with an open end date, so this week's price for
         the same item overlaps it and SAP answers 422-style VALIDITY_OVERLAP.

Targets are deterministic: Singles that have a rule and can be priced (floor below current price).
"""
from __future__ import annotations

import argparse
from datetime import date

from shared import db
from shared.sap_schema import schema

LEGACY_RUN = "LEGACY-PREV-WEEK"


def _targets(con, week: str, region: str | None, skip: set[tuple]) -> list[dict]:
    s = schema()
    sql = f"""SELECT c.sku, c.pack_qty, c.region, c.current_price::float AS current_price
              FROM {s}.clearance_candidates c JOIN {s}.business_rules r USING (week, sku, pack_qty)
              WHERE c.week = %s AND c.pack_qty = 1 AND r.price_floor < c.current_price"""
    args: list = [week]
    if region:
        sql += " AND c.region = %s"
        args.append(region)
    return [r for r in con.execute(sql + " ORDER BY c.sku, c.region", args).fetchall()
            if (r["sku"], r["pack_qty"]) not in skip]


def apply_promo_override(week: str, n: int = 3, region: str = "VIC", database_url: str | None = None) -> list[dict]:
    s = schema()
    with db.connect(database_url) as con:
        rows = _targets(con, week, region, set())[:n]
        for r in rows:
            con.execute(f"""INSERT INTO {s}.promotions (week, sku, pack_qty, region, promo_price, promo_name)
                            VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                        (week, r["sku"], r["pack_qty"], r["region"], round(r["current_price"] * 0.7, 2),
                         "Catalogue promotion"))
    return rows


def apply_validity_overlap(week: str, n: int = 3, database_url: str | None = None) -> list[dict]:
    s = schema()
    with db.connect(database_url) as con:
        taken = {(r["sku"], r["pack_qty"]) for r in _targets(con, week, "VIC", set())[:n]}   # promo items
        rows = _targets(con, week, None, taken)
        first = sorted({(r["sku"], r["pack_qty"]) for r in rows})[:n]
        rows = [r for r in rows if (r["sku"], r["pack_qty"]) in first]
        y, w = week.split("-W")
        prev_monday = date.fromisocalendar(int(y), int(w) - 1, 1)
        for r in rows:
            con.execute(f"""INSERT INTO {s}.price_conditions
                (idem_key, run_id, week, sku, pack_qty, region, price, valid_from, valid_to)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                        (f"{LEGACY_RUN}|{r['sku']}|{r['pack_qty']}|{r['region']}", LEGACY_RUN,
                         f"{y}-W{int(w) - 1:02d}", r["sku"], r["pack_qty"], r["region"], r["current_price"],
                         prev_monday, date(2099, 12, 31)))                                       # open end date
    return rows


def clear_scenarios(database_url: str | None = None) -> None:
    s = schema()
    with db.connect(database_url) as con:
        con.execute(f"DELETE FROM {s}.promotions")
        con.execute(f"DELETE FROM {s}.price_conditions WHERE run_id = %s", (LEGACY_RUN,))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", default="2026-W39")
    ap.add_argument("--apply", nargs="*", choices=["promo", "overlap"], default=[])
    ap.add_argument("--clear", action="store_true")
    a = ap.parse_args()
    if a.clear:
        clear_scenarios()
        print("scenarios cleared")
    if "promo" in a.apply:
        print("promo override:", [(r["sku"], r["region"]) for r in apply_promo_override(a.week)])
    if "overlap" in a.apply:
        print("validity overlap:", sorted({(r["sku"], r["region"]) for r in apply_validity_overlap(a.week)}))
