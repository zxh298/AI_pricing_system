"""Plant LOW_SELL_THROUGH causes in a COPY of the warehouse. Records stay clean; only what happened afterwards changes.

    cp data/warehouse.duckdb data/demo.duckdb
    python -m jobs.data_gen.outcome_scenarios --path data/demo.duckdb --apply cannibal
    python -m jobs.data_gen.outcome_scenarios --clear          # removes the planted promotions from mock-sap

Each planter edits sales (and inventory or prices) of `week` and returns the records it touched, so an evaluation knows
the true cause. The agent never sees these labels. `promotions` are returned, not written: an evaluation serves them
from a fake SAP; `--apply` writes them to mock-sap for a live demo.

pos      a brand's top sellers in the VIC metro stores still ring up at last week's price, and sell less
stock    a brand in NSW opens the week with almost no stock
stalled  a brand's markdown does not deepen in any region, and demand fades
cannibal a promotion on the Single takes the sales of the same sku's Multipack, while both records stay clean
mixed    two skus of one brand and region, one with a POS gap and one short of stock
substitute  a promotion on another brand's product takes the sales; the case does not name that product
"""
from __future__ import annotations

import argparse
import json
import os

import duckdb

from services.diagnostic_api.outcome import shift_week, week_dates
from shared import db
from shared.config import load_config
from shared.sap_schema import schema

WEEK = "2026-W38"
SCENARIO_PROMO = "Scenario: single-pack catalogue promotion"


def _picks(con, week: str, where: str, args: list | None = None, limit: int = 3) -> list[tuple]:
    """Records that sell well enough to be judged, biggest first."""
    start, end = week_dates(shift_week(week, -1))
    return con.execute(f"""
        SELECT c.sku, c.pack_qty, c.region FROM clearance_candidates c JOIN products p USING (sku, pack_qty)
        JOIN sales s ON s.sku = c.sku AND s.pack_qty = c.pack_qty AND s.region = c.region
        WHERE c.week = ? AND s.sale_date BETWEEN ? AND ? AND {where}
        GROUP BY 1,2,3 HAVING SUM(s.units) >= 60 ORDER BY SUM(s.units) DESC, 1, 2, 3 LIMIT ?""",
                       [week, start, end, *(args or []), limit]).fetchall()


def _top_brand(con, week: str, region: str) -> str:
    return con.execute("""SELECT p.brand FROM clearance_candidates c JOIN products p USING (sku, pack_qty)
                          WHERE c.week = ? AND c.region = ? GROUP BY 1 ORDER BY COUNT(*) DESC, 1 LIMIT 1""",
                       [week, region]).fetchone()[0]


def _scale(con, week: str, record: tuple, factor: float, stores: list[str] | None = None) -> None:
    start, end = week_dates(week)
    sql, args = "UPDATE sales SET units = CAST(FLOOR(units * ?) AS INTEGER) WHERE sku = ? AND pack_qty = ? AND region = ? AND sale_date BETWEEN ? AND ?", [factor, *record, start, end]
    if stores:
        sql += f" AND store_id IN ({','.join('?' * len(stores))})"
        args += stores
    con.execute(sql, args)


def _brand(con, record: tuple) -> str:
    return con.execute("SELECT brand FROM products WHERE sku = ? AND pack_qty = ?", record[:2]).fetchone()[0]


def _ring_up_old_price(con, week: str, record: tuple, stores: list[str]) -> None:
    before = con.execute("SELECT price FROM price_history WHERE week = ? AND sku = ? AND pack_qty = ? AND region = ?",
                         [shift_week(week, -1), *record]).fetchone()[0]
    start, end = week_dates(week)
    con.execute(f"""UPDATE sales SET price = ? WHERE sku = ? AND pack_qty = ? AND region = ? AND sale_date BETWEEN ? AND ?
                    AND store_id IN ({','.join('?' * len(stores))})""", [before, *record, start, end, *stores])


def _stock_short(con, week: str, record: tuple) -> None:
    """Sales stop when the stock runs out, so the opening stock is exactly what sold."""
    _scale(con, week, record, 0.25)
    start, end = week_dates(week)
    sold = con.execute("SELECT SUM(units) FROM sales WHERE sku = ? AND pack_qty = ? AND region = ? AND sale_date BETWEEN ? AND ?",
                       [*record, start, end]).fetchone()[0]
    con.execute("UPDATE inventory SET on_hand = ? WHERE week = ? AND sku = ? AND pack_qty = ? AND region = ?", [sold, week, *record])


def plant_pos(path: str, week: str = WEEK) -> dict:
    with duckdb.connect(path) as con:
        records = _picks(con, week, "c.region = 'VIC' AND p.brand = ?", [_top_brand(con, week, "VIC")])
        stores = ["VIC01", "VIC02", "VIC03"]
        for r in records:
            _ring_up_old_price(con, week, r, stores)
            _scale(con, week, r, 0.0, stores)        # the rest of the region sold more at the new price: a milder fall here
        return {"cause": "POS_PRICE_GAP", "records": records, "brand": _brand(con, records[0]), "promotions": []}


def plant_stock(path: str, week: str = WEEK) -> dict:
    with duckdb.connect(path) as con:
        records = _picks(con, week, "c.region = 'NSW' AND p.brand = ?", [_top_brand(con, week, "NSW")])
        for r in records:
            _stock_short(con, week, r)
        return {"cause": "STOCK_SHORTAGE", "records": records, "brand": _brand(con, records[0]), "promotions": []}


def plant_stalled(path: str, week: str = WEEK) -> dict:
    before, (start, end) = shift_week(week, -1), week_dates(week)
    with duckdb.connect(path) as con:
        records = _picks(con, week, "c.region = 'QLD' AND p.brand = ?", [_top_brand(con, week, "QLD")])
        for sku, qty, region in records:
            price = con.execute("SELECT price FROM price_history WHERE week = ? AND sku = ? AND pack_qty = ? AND region = ?",
                                [before, sku, qty, region]).fetchone()[0]
            con.execute("UPDATE price_history SET price = ? WHERE week = ? AND sku = ? AND pack_qty = ? AND region = ?",
                        [price, week, sku, qty, region])
            con.execute("UPDATE sales SET price = ? WHERE sku = ? AND pack_qty = ? AND region = ? AND sale_date BETWEEN ? AND ?",
                        [price, sku, qty, region, start, end])
            _scale(con, week, (sku, qty, region), 0.3)
        return {"cause": "DISCOUNT_STALLED", "records": records, "brand": _brand(con, records[0]), "promotions": []}


def plant_cannibal(path: str, week: str = WEEK, region: str = "VIC") -> dict:
    with duckdb.connect(path) as con:
        row = con.execute("""SELECT p.sku, p.pack_qty FROM clearance_candidates p JOIN clearance_candidates s
                             ON s.week = p.week AND s.sku = p.sku AND s.region = p.region AND s.pack_qty = 1
                             WHERE p.week = ? AND p.region = ? AND p.pack_qty > 1 ORDER BY p.pack_qty, p.sku LIMIT 1""",
                          [week, region]).fetchone()
        sku, qty = row
        pack, single = (sku, qty, region), (sku, 1, region)
        single_price = con.execute("SELECT price FROM price_history WHERE week = ? AND sku = ? AND pack_qty = 1 AND region = ?",
                                   [week, sku, region]).fetchone()[0]
        start, end = week_dates(week)
        promo = round(single_price * 0.75, 2)
        con.execute("""UPDATE sales SET price = ?, units = units * 2 WHERE sku = ? AND pack_qty = 1 AND region = ?
                       AND sale_date BETWEEN ? AND ?""", [promo, sku, region, start, end])
        _boost_baseline(con, week, pack)
        _scale(con, week, pack, 0.2)
        return {"cause": "CANNIBALIZATION", "records": [pack], "brand": _brand(con, pack),
                "promotions": [{"week": week, "sku": sku, "pack_qty": 1, "region": region, "promo_price": promo}]}


def _boost_baseline(con, week: str, record: tuple) -> None:
    """The planted pack may be a small seller; give it enough history for the check to be able to judge it."""
    start = week_dates(shift_week(week, -3))[0]
    end = week_dates(week)[1]
    con.execute("""UPDATE sales SET units = units + 2 WHERE sku = ? AND pack_qty = ? AND region = ? AND sale_date BETWEEN ? AND ?""",
                [*record, start, end])


def plant_substitute(path: str, week: str = WEEK, region: str = "NSW") -> dict:
    """A promotion on another product takes the sales. Nothing in the case names that product."""
    start, end = week_dates(week)
    with duckdb.connect(path) as con:
        brand = _top_brand(con, week, region)
        lost = _picks(con, week, "c.region = ? AND p.brand = ?", [region, brand])
        category = con.execute("SELECT category FROM products WHERE sku = ? AND pack_qty = ?", lost[0][:2]).fetchone()[0]
        lost = [r for r in lost if con.execute("SELECT category FROM products WHERE sku = ? AND pack_qty = ?", r[:2]).fetchone()[0] == category]
        rival = _picks(con, week, "c.region = ? AND p.brand <> ? AND p.category = ?", [region, brand, category], limit=1)[0]
        promo = round(con.execute("SELECT price FROM price_history WHERE week = ? AND sku = ? AND pack_qty = ? AND region = ?",
                                  [week, *rival]).fetchone()[0] * 0.7, 2)
        con.execute("""UPDATE sales SET price = ?, units = units * 3 WHERE sku = ? AND pack_qty = ? AND region = ?
                       AND sale_date BETWEEN ? AND ?""", [promo, *rival, start, end])
        for r in lost:
            _scale(con, week, r, 0.25)
        return {"cause": "SUBSTITUTION", "records": lost, "brand": brand, "rival": rival,
                "promotions": [{"week": week, "sku": rival[0], "pack_qty": rival[1], "region": rival[2], "promo_price": promo}]}


def plant_mixed(path: str, week: str = WEEK) -> dict:
    with duckdb.connect(path) as con:
        brand = _top_brand(con, week, "VIC")
        records = _picks(con, week, "c.region = 'VIC' AND p.brand = ?", [brand], limit=2)
        _ring_up_old_price(con, week, records[0], ["VIC01", "VIC02", "VIC03", "VIC04", "VIC05"])
        _scale(con, week, records[0], 0.1)
        _stock_short(con, week, records[1])
        return {"cause": "MIXED", "records": records, "brand": brand, "promotions": [],
                "causes": {"POS_PRICE_GAP": [records[0][0]], "STOCK_SHORTAGE": [records[1][0]]}}


SCENARIOS = {"pos": plant_pos, "stock": plant_stock, "stalled": plant_stalled, "cannibal": plant_cannibal, "mixed": plant_mixed,
             "substitute": plant_substitute}


def write_promotions(promotions: list[dict], database_url: str | None = None) -> None:
    with db.connect(database_url) as con:
        for p in promotions:
            con.execute(f"""INSERT INTO {schema()}.promotions (week, sku, pack_qty, region, promo_price, promo_name)
                            VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                        (p["week"], p["sku"], p["pack_qty"], p["region"], p["promo_price"], SCENARIO_PROMO))


def clear_promotions(database_url: str | None = None) -> None:
    with db.connect(database_url) as con:
        con.execute(f"DELETE FROM {schema()}.promotions WHERE promo_name = %s", (SCENARIO_PROMO,))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--path")
    ap.add_argument("--week", default=WEEK)
    ap.add_argument("--apply", nargs="*", choices=sorted(SCENARIOS), default=[])
    ap.add_argument("--clear", action="store_true")
    a = ap.parse_args()
    if a.clear:
        clear_promotions()
        print("planted promotions removed")
    if a.apply:
        if not a.path or not os.path.exists(a.path):
            raise SystemExit("--apply needs --path to an existing copy of the warehouse")
        if os.path.abspath(a.path) == os.path.abspath(load_config().duckdb_path):
            raise SystemExit("refusing to plant into the main warehouse; copy it first and pass the copy")
        for name in a.apply:
            planted = SCENARIOS[name](a.path, a.week)
            write_promotions(planted["promotions"])
            print(name, json.dumps(planted, default=str))
