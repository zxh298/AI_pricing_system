from jobs.data_gen.generate import WEEK, write
from shared.warehouse import DuckDBWarehouse


def test_generate_and_query(tmp_path):
    path = str(tmp_path / "w.duckdb")
    counts = write(path, seed=1)
    assert counts["products"] == 120 and counts["stores"] == 13
    wh = DuckDBWarehouse(path)
    cands = wh.get_candidates(WEEK)
    assert cands and all(c["sku"] != "999999" for c in cands)   # inner join drops orphan
    skus = sorted({c["sku"] for c in cands})[:3]
    assert wh.get_sales_history(skus, "VIC")
    assert wh.get_inventory(skus, WEEK)
    assert len(wh.get_rules(WEEK)) == 43                         # 45 minus 2 missing rules


def test_seeded_and_deterministic(tmp_path):
    a, b = str(tmp_path / "a.duckdb"), str(tmp_path / "b.duckdb")
    write(a, seed=7); write(b, seed=7)
    assert DuckDBWarehouse(a).get_rules(WEEK) == DuckDBWarehouse(b).get_rules(WEEK)
