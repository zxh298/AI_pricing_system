import duckdb
import pytest

import pandas as pd

from jobs.data_gen.generate import WEEK, build, write
from shared.warehouse import DuckDBWarehouse

EPS = 1e-9


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("d") / "w.duckdb")
    write(path, seed=1, with_sap_tables=True)
    return path


def q(db, sql):
    con = duckdb.connect(db, read_only=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_all_weeks_stored(db):
    assert q(db, "SELECT count(*) FROM weeks")[0][0] == 13                        # W27..W39
    assert q(db, "SELECT count(DISTINCT date_trunc('week', sale_date)) FROM sales")[0][0] == 12            # W27..W38
    assert q(db, "SELECT count(DISTINCT week) FROM price_history")[0][0] == 12
    assert [r[0] for r in q(db, "SELECT DISTINCT week FROM clearance_candidates ORDER BY 1")] == \
        [f"2026-W{n}" for n in range(34, 40)]
    assert q(db, "SELECT count(DISTINCT week) FROM business_rules")[0][0] == 6
    assert q(db, "SELECT count(DISTINCT week) FROM inventory")[0][0] == 6


def test_multipacks_share_sku_id(db):
    assert {r[0] for r in q(db, "SELECT DISTINCT pack_qty FROM products")} == {1, 6, 12}
    assert dict(q(db, "SELECT pack_type, count(*) FROM products GROUP BY 1")) == \
        {"Single": 100, "Multipack": 28}
    assert q(db, "SELECT count(DISTINCT sku) FROM products")[0][0] == 100         # same sku ids
    assert q(db, "SELECT count(*) FROM (SELECT sku, pack_qty FROM products GROUP BY 1, 2 "
                 "HAVING count(*) > 1)")[0][0] == 0                               # key is unique
    assert q(db, "SELECT count(*) FROM products WHERE (pack_qty = 1) <> (pack_type = 'Single')"
             )[0][0] == 0
    for t in ("sales", "price_history", "clearance_candidates", "business_rules", "inventory",
              "seed_manifest"):
        cols = [r[0] for r in q(db, f"SELECT column_name FROM information_schema.columns "
                                    f"WHERE table_name = '{t}'")]
        assert "pack_type" in cols and "pack_qty" in cols, t


def test_price_never_above_last_week(db):
    bad = q(db, """SELECT count(*) FROM (
        SELECT price, lag(price) OVER (PARTITION BY sku, pack_qty, region ORDER BY week) AS prev, is_clearance
        FROM price_history WHERE week >= '2026-W33') WHERE prev IS NOT NULL AND price > prev + 1e-9""")
    assert bad[0][0] == 0
    assert q(db, "SELECT count(*) FROM price_history WHERE is_clearance")[0][0] > 0
    # some SKUs actually got cheaper
    assert q(db, """SELECT count(*) FROM price_history a JOIN price_history b
        ON a.sku = b.sku AND a.pack_qty = b.pack_qty AND a.region = b.region AND a.week = '2026-W38' AND b.week = '2026-W33'
        WHERE a.price < b.price""")[0][0] > 0


def test_multipack_unit_price_not_below_single(db):
    bad = q(db, """SELECT count(*) FROM price_history pk
        JOIN price_history sg ON sg.sku = pk.sku AND sg.pack_qty = 1
             AND sg.region = pk.region AND sg.week = pk.week
        WHERE pk.pack_qty > 1 AND pk.week >= '2026-W34'
          AND pk.price / pk.pack_qty < sg.price - 1e-9""")
    assert bad[0][0] == 0


def test_price_respects_floor_and_max_markdown(db):
    assert q(db, """SELECT count(*) FROM price_history h JOIN business_rules r
        ON r.sku = h.sku AND r.pack_qty = h.pack_qty AND r.week = h.week WHERE h.is_clearance AND h.price < r.price_floor - 1e-9
        """)[0][0] == 0
    assert q(db, """SELECT count(*) FROM price_history h JOIN business_rules r
        ON r.sku = h.sku AND r.pack_qty = h.pack_qty AND r.week = h.week
        JOIN products p ON p.sku = h.sku AND p.pack_qty = h.pack_qty
        WHERE h.is_clearance AND h.price < p.shelf_price * (1 - r.max_markdown_pct) - 0.01""")[0][0] == 0


def test_w39_current_price_is_w38_price(db):
    bad = q(db, """SELECT count(*) FROM clearance_candidates c JOIN price_history h
        ON h.sku = c.sku AND h.pack_qty = c.pack_qty AND h.region = c.region AND h.week = '2026-W38'
        WHERE c.week = '2026-W39' AND abs(c.current_price - h.price) > 1e-9""")
    assert bad[0][0] == 0


def test_warehouse_queries_and_planted_issues(db):
    wh = DuckDBWarehouse(db)
    cands = wh.get_candidates(WEEK)
    orphan = [c for c in cands if c["name"] is None]               # candidate with no product master
    assert {c["sku"] for c in orphan} == {"999999"} and len(orphan) == 3
    assert len({(c["sku"], c["pack_qty"]) for c in cands if c["name"] is not None}) == 45
    skus = sorted({c["sku"] for c in cands})[:3]
    assert wh.get_sales_history(skus, "VIC") and wh.get_inventory(skus, WEEK)
    assert wh.get_price_history(skus) and wh.get_products(skus)
    assert len(wh.get_rules(WEEK)) == 43                          # 45 minus 2 missing rules
    assert q(db, "SELECT count(*) FROM seed_manifest")[0][0] == 5


def test_deterministic(tmp_path):
    a, b = str(tmp_path / "a.duckdb"), str(tmp_path / "b.duckdb")
    write(a, seed=7, with_sap_tables=True); write(b, seed=7, with_sap_tables=True)
    assert DuckDBWarehouse(a).get_rules(WEEK) == DuckDBWarehouse(b).get_rules(WEEK)
    ph = "SELECT * FROM price_history ORDER BY week, sku, region"
    assert q(a, ph) == q(b, ph)


def test_sap_tables_not_in_duckdb_by_default(tmp_path):
    path = str(tmp_path / "w.duckdb")
    write(path, seed=1)
    tables = {r[0] for r in q(path, "SHOW TABLES")}
    assert "clearance_candidates" not in tables and "business_rules" not in tables
    assert {"products", "sales", "price_history", "inventory", "weeks", "seed_manifest"} <= tables


def test_shelf_price_and_week_no_columns(db):
    for t in ("price_history", "clearance_candidates"):
        cols = [r[0] for r in q(db, f"SELECT column_name FROM information_schema.columns WHERE table_name = '{t}'")]
        assert "shelf_price" in cols and "week_no" in cols, t
    # shelf price is the same full price everywhere
    assert q(db, """SELECT count(*) FROM price_history h JOIN products p USING (sku, pack_qty)
                    WHERE abs(h.shelf_price - p.shelf_price) > 1e-9""")[0][0] == 0
    assert q(db, """SELECT count(*) FROM clearance_candidates c JOIN products p USING (sku, pack_qty)
                    WHERE abs(c.shelf_price - p.shelf_price) > 1e-9""")[0][0] == 0
    # week_no only set for clearance weeks
    assert q(db, "SELECT count(*) FROM price_history WHERE is_clearance <> (week_no IS NOT NULL)")[0][0] == 0


def _week_no_is_consecutive(cands: pd.DataFrame):
    items = cands[cands.sku != "999999"].drop_duplicates(["week", "sku", "pack_qty"])
    for _, g in items.groupby(["sku", "pack_qty"]):
        g = g.sort_values("week")
        n = g.week.str[-2:].astype(int).tolist()
        assert n == list(range(n[0], n[0] + len(n)))              # consecutive weeks on the list
        assert g.week_no.tolist() == list(range(1, len(n) + 1))   # 1, 2, 3, ...


def test_week_no_counts_consecutive_weeks_from_one():
    _week_no_is_consecutive(build(1)["clearance_candidates"])


def test_price_never_below_half_of_shelf_price(db):
    assert q(db, "SELECT count(*) FROM price_history WHERE is_clearance AND price < shelf_price * 0.5 - 0.01"
             )[0][0] == 0


def test_history_follows_the_markdown_schedule(db):
    # week_no 1 = shelf price; later weeks step down 5 points, unless the floor / ladder binds
    assert q(db, "SELECT count(*) FROM price_history WHERE week_no = 1 AND abs(price - shelf_price) > 1e-9"
             )[0][0] == 0
    assert q(db, """SELECT count(*) FROM price_history h JOIN business_rules r
        ON r.sku = h.sku AND r.pack_qty = h.pack_qty AND r.week = h.week
        WHERE h.pack_qty = 1 AND h.week_no > 1 AND h.price > round(h.shelf_price * (1 - 0.05 * (h.week_no - 1)), 2) + 0.011
        """)[0][0] == 0
    assert q(db, "SELECT min(max_markdown_pct), max(max_markdown_pct), min(max_clearance_weeks) "
                 "FROM business_rules")[0] == (0.5, 0.5, 10)


def test_items_removed_after_max_weeks_but_kept_in_history():
    d = build(1, max_clearance_weeks=3)
    cands, hist = d["clearance_candidates"], d["price_history"]
    assert cands.week_no.max() == 3                                        # never listed past week 3
    _week_no_is_consecutive(cands)
    listed = cands[cands.sku != "999999"].drop_duplicates(["week", "sku", "pack_qty"])
    done = listed.groupby(["sku", "pack_qty"]).filter(lambda g: g.week_no.max() == 3)
    assert len(done) > 0
    last = done.groupby(["sku", "pack_qty"]).week.max().reset_index()
    for r in last.itertuples():                                            # not listed after week 3 ...
        assert not ((cands.sku == r.sku) & (cands.pack_qty == r.pack_qty) & (cands.week > r.week)).any()
    # ... but the item, its prices and its sales are still in the history tables
    keys = set(zip(done.sku, done.pack_qty))
    ph = hist[[k in keys for k in zip(hist.sku, hist.pack_qty)]]
    assert ph.week.nunique() == 12 and ph.is_clearance.any()
    assert {(s, p) for s, p in zip(d["sales"].sku, d["sales"].pack_qty)} >= keys
    assert set(d["business_rules"].max_clearance_weeks) == {3}
