"""Step 3: pricing engine on the generated data (compute-only tests need no Postgres)."""
import duckdb
import pytest

from jobs.data_gen.generate import WEEK, write
from jobs.pricing_engine.engine import price_week, save_recommendations
from jobs.pricing_engine.sender import to_payload, week_dates
from shared.pricing_rules import discount_pct
from shared.warehouse import DuckDBWarehouse


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("e") / "w.duckdb")
    write(path, seed=1, with_sap_tables=True)          # candidates/rules directly (no SAP needed)
    wh = DuckDBWarehouse(path)
    return path, wh, price_week(WEEK, wh)


def test_every_candidate_row_accounted_for(env):
    _, _, rows = env
    assert len(rows) == 138
    assert {r["status"] for r in rows} == {"PRICED", "SKIPPED"}
    skipped = {}
    for r in rows:
        if r["status"] == "SKIPPED":
            skipped[r["reason"]] = skipped.get(r["reason"], 0) + 1
    assert skipped == {"NOT_IN_PRODUCTS": 3, "MISSING_RULE": 6, "FLOOR_ABOVE_PRICE": 3}
    assert all(r["recommended_price"] is None for r in rows if r["status"] == "SKIPPED")
    assert sum(r["warning"] == "ZERO_INVENTORY" for r in rows) == 3       # planted zero-stock item, 3 regions


def test_prices_obey_hard_rules(env):
    _, wh, rows = env
    rules = {(r["sku"], r["pack_qty"]): r for r in wh.get_rules(WEEK)}
    price = {(r["sku"], r["pack_qty"], r["region"]): r["recommended_price"] for r in rows if r["status"] == "PRICED"}
    assert len(price) == 126
    for r in rows:
        if r["status"] != "PRICED":
            continue
        p, rule = r["recommended_price"], rules[(r["sku"], r["pack_qty"])]
        assert p <= r["current_price"] + 1e-9                              # never above last week
        assert p >= rule["price_floor"] - 1e-9                             # SAP floor
        assert p >= 0.5 * r["shelf_price"] - 0.01                          # never below 50% off shelf
        assert r["week_no"] <= rule["max_clearance_weeks"]
        if r["pack_qty"] > 1 and (r["sku"], 1, r["region"]) in price:      # multipack per-item >= Single
            assert p / r["pack_qty"] >= price[(r["sku"], 1, r["region"])] - 1e-9


def test_discount_follows_schedule_unless_a_rule_binds(env):
    _, _, rows = env
    priced = [r for r in rows if r["status"] == "PRICED"]
    on_schedule = [r for r in priced if r["binding"] == "SCHEDULE"]
    assert len(on_schedule) > len(priced) / 2
    for r in on_schedule:
        assert abs(r["discount_pct"] - discount_pct(r["week_no"])) < 0.001   # rounding to cents only
    assert {r["binding"] for r in priced} <= {"SCHEDULE", "FLOOR", "MAX_MARKDOWN", "LADDER", "LAST_WEEK"}
    assert all(r["discount_pct"] == 0 for r in priced if r["week_no"] == 1)   # first week: no discount


def test_week_one_items_are_at_shelf_price(env):
    _, _, rows = env
    first = [r for r in rows if r["status"] == "PRICED" and r["week_no"] == 1]
    assert first and all(r["recommended_price"] == r["shelf_price"] for r in first)


def test_prices_continue_from_history(env):
    """W39 price never above the W38 price and an item's discount only deepens."""
    path, _, rows = env
    con = duckdb.connect(path, read_only=True)
    last = {(s, q, g): p for s, q, g, p in con.execute(
        "SELECT sku, pack_qty, region, price FROM price_history WHERE week = '2026-W38'").fetchall()}
    con.close()
    for r in rows:
        if r["status"] == "PRICED":
            assert r["recommended_price"] <= last.get((r["sku"], r["pack_qty"], r["region"]), r["current_price"]) + 1e-9


def test_save_and_payload(env, tmp_path):
    path, _, rows = env
    out = str(tmp_path / "rec.duckdb")
    save_recommendations(out, "run-x", WEEK, rows)
    save_recommendations(out, "run-x", WEEK, rows)                          # rerun replaces, no duplicates
    con = duckdb.connect(out, read_only=True)
    assert con.execute("SELECT count(*) FROM price_recommendations").fetchone()[0] == 138
    assert con.execute("SELECT count(*) FROM price_recommendations WHERE status='PRICED'").fetchone()[0] == 126
    con.close()
    payload = to_payload([r for r in rows if r["status"] == "PRICED"], WEEK)
    assert len(payload) == 126 and payload[0]["valid_from"] == "2026-09-21" and payload[0]["valid_to"] == "2026-09-27"
    assert week_dates(WEEK)[0].isoformat() == "2026-09-21"
