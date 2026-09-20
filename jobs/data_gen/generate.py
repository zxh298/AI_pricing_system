"""Step 1: seeded synthetic data -> DuckDB. Fictional brands only.

    python -m jobs.data_gen.generate [--seed 42] [--path data/warehouse.duckdb]

Tables: products, stores, sales, inventory, clearance_candidates, business_rules,
seed_manifest (the deliberately planted data issues, used by tests and later scenarios).
"""
from __future__ import annotations

import argparse
import os
from datetime import date, timedelta

import duckdb
import numpy as np
import pandas as pd

WEEK = "2026-W39"                 # candidate week
WEEK_START = date(2026, 9, 21)    # Monday of 2026-W39
HISTORY_WEEKS = 12
BRANDS = ["Brand A", "Harbour Ridge", "Stonefield", "Larkspur", "Kestrel Bay"]
CATEGORIES = {"Red": 18, "White": 14, "Sparkling": 22, "Spirits": 40}   # typical base price
REGIONS = {"VIC": 5, "NSW": 5, "QLD": 3}                                 # stores per region
N_SKUS = 120
N_CANDIDATES = 45


def build(seed: int = 42):
    rng = np.random.default_rng(seed)

    products = pd.DataFrame({
        "sku": [f"{100000 + i}" for i in range(N_SKUS)],
        "brand": rng.choice(BRANDS, N_SKUS),
        "category": rng.choice(list(CATEGORIES), N_SKUS),
    })
    products["pack_size"] = rng.choice([1, 1, 1, 6], N_SKUS)
    products["base_price"] = [
        round(CATEGORIES[c] * rng.uniform(0.6, 1.8) * (5 if p == 6 else 1)) - 0.01
        for c, p in zip(products.category, products.pack_size)]
    products["cost"] = (products.base_price * rng.uniform(0.45, 0.7, N_SKUS)).round(2)
    products["name"] = products.brand + " " + products.category + " " + products.sku.str[-3:]
    elasticity = rng.uniform(-3.2, -0.8, N_SKUS)        # hidden truth the pricing model must learn
    base_units = rng.uniform(0.4, 3.0, N_SKUS)          # per store per day

    stores = pd.DataFrame(
        [(f"{r}{i + 1:02d}", r, f"{r}_METRO" if i < 3 else f"{r}_REGIONAL")
         for r, n in REGIONS.items() for i in range(n)],
        columns=["store_id", "region", "price_zone"])

    # --- sales history: weekly price moves so elasticity is estimable ---
    days = [WEEK_START - timedelta(days=7 * HISTORY_WEEKS) + timedelta(days=d)
            for d in range(7 * HISTORY_WEEKS)]
    rows = []
    for i, sku in enumerate(products.sku):
        bp = products.base_price[i]
        weekly_mult = rng.choice([1.0, 1.0, 0.9, 0.8, 1.1], HISTORY_WEEKS)
        for s in stores.itertuples():
            for d_i, d in enumerate(days):
                price = round(bp * weekly_mult[d_i // 7], 2)
                lam = base_units[i] * (price / bp) ** elasticity[i] * (1.3 if d.weekday() >= 4 else 1.0)
                rows.append((d, sku, s.store_id, s.region, int(rng.poisson(lam)), price))
    sales = pd.DataFrame(rows, columns=["sale_date", "sku", "store_id", "region", "units", "price"])

    # --- weekly candidate list: SAP decides WHAT is on clearance ---
    cand_skus = list(rng.choice(products.sku, N_CANDIDATES, replace=False))
    cand = []
    for sku in cand_skus:
        bp = float(products.loc[products.sku == sku, "base_price"].iloc[0])
        for r in REGIONS:
            cand.append((WEEK, sku, r, bp))
    candidates = pd.DataFrame(cand, columns=["week", "sku", "region", "current_price"])

    # --- business rules snapshot ---
    rules = pd.DataFrame({
        "week": WEEK, "sku": cand_skus,
        "price_floor": [round(float(products.loc[products.sku == s, "cost"].iloc[0]) * 1.05, 2)
                        for s in cand_skus],
        "max_markdown_pct": 0.5, "rule_version": "SAP-R-2026-09-18"})

    inventory = candidates.assign(on_hand=rng.integers(20, 400, len(candidates)))[
        ["week", "sku", "region", "on_hand"]]

    # --- planted issues (error taxonomy, PROJECT_CONTEXT s2) ---
    manifest = []
    a, b, c, d, e = cand_skus[:5]
    rules = rules[~rules.sku.isin([a, b])]                                   # missing rule
    manifest += [(WEEK, s, "MISSING_RULE", "no floor in rules snapshot") for s in (a, b)]
    rules.loc[rules.sku == c, "price_floor"] = (
        candidates.loc[candidates.sku == c, "current_price"].iloc[0] + 1)    # floor above price
    manifest.append((WEEK, c, "FLOOR_ABOVE_PRICE", "floor exceeds current price"))
    inventory.loc[inventory.sku == d, "on_hand"] = 0                         # zero stock
    manifest.append((WEEK, d, "ZERO_INVENTORY", "on_hand = 0 in all regions"))
    candidates = pd.concat([candidates, pd.DataFrame(
        [(WEEK, "999999", r, 19.99) for r in REGIONS], columns=candidates.columns)])
    manifest.append((WEEK, "999999", "NOT_IN_PRODUCTS", "candidate has no product master"))
    seed_manifest = pd.DataFrame(manifest, columns=["week", "sku", "issue", "detail"])

    return {"products": products, "stores": stores, "sales": sales, "inventory": inventory,
            "clearance_candidates": candidates, "business_rules": rules,
            "seed_manifest": seed_manifest}


def write(path: str, seed: int = 42) -> dict[str, int]:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if os.path.exists(path):
        os.remove(path)
    con = duckdb.connect(path)
    counts = {}
    for name, df in build(seed).items():
        con.register("df", df)
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM df")
        con.unregister("df")
        counts[name] = len(df)
    con.close()
    return counts


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--path", default=os.environ.get("DUCKDB_PATH", "data/warehouse.duckdb"))
    args = ap.parse_args()
    for t, n in write(args.path, args.seed).items():
        print(f"{t:22s} {n:>8d}")
