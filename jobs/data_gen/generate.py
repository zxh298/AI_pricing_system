"""Step 1: seeded synthetic data -> DuckDB. Fictional brands only.

    python -m jobs.data_gen.generate [--seed 42] [--path data/warehouse.duckdb]
                                     [--force] [--skip-sap] [--skip-ingest]

Both stores keep the full history:
  * SAP (mock-sap's Postgres, schema `sap`) owns the weekly clearance list, business rules and the
    price conditions it holds: candidates + rules for W34-W39 and the published prices of W34-W38.
    It also keeps an append-only submission_log of every record it receives.
  * DuckDB holds sales and price history (W27-W38), inventory, products, and (via `jobs.sap_ingest`)
    the candidates + rules of every week. The weekly pipeline appends each new week's outcome.
This CLI is a FULL RESET of both stores, so it refuses to overwrite existing data without --force.

Timeline (2026 ISO weeks):
    W27-W33  history, no clearance (shelf price = regular price, small random promos to W32)
    W34-W38  clearance weeks already priced and published (candidates, rules, inventory, prices)
    W39      the week to be priced next: candidates + rules + inventory only, no prices yet

Hard constraints baked into the generated history (and to be enforced by the rule engine):
    1. For the same SKU and region, a clearance price is <= the previous week's price.
       (A SKU's first clearance week is compared with its regular shelf price.)
    2. A multipack's per-item price (pack price / pack_qty) is >= the Single's price of the
       same sku in the same week and region.
    3. Price >= max(SAP floor, shelf_price * (1 - max_markdown_pct)); max_markdown_pct is 0.5,
       so no price is below 50% off the full (shelf) price. The floor wins if it is higher.
    4. An item is on the list for at most max_clearance_weeks (10) consecutive weeks. After its
       week 10 SAP stops listing it; it gets no new price but stays in the history tables.
    Markdown schedule (shared/pricing_rules.py): week_no 1 = no discount, then +5 points of
    shelf price per week (week 2 = 5% off ... week 10 = 45% off), subject to the rules above.
    Simplification: a SKU that leaves the candidate list keeps its last shelf price.

`shelf_price` is the full price before any markdown. `week_no` is the nth consecutive week an
item has been on the clearance list (1 = first week).

An item is one `sku`; its pack sizes share that sku id and differ by `pack_qty` (units in
the pack: 1, 6, 12) and `pack_type` ('Single' | 'Multipack'). The row key is (sku, pack_qty).

Tables: weeks, products, stores, sales, price_history, inventory, clearance_candidates,
business_rules, price_conditions (SAP's published prices of past weeks), seed_manifest (planted W39
issues, used by tests and later scenarios).
"""
from __future__ import annotations

import argparse
import os
from datetime import date, timedelta

import duckdb
import numpy as np
import pandas as pd

from shared.pricing_rules import recommend

CAND_WEEK_N = 39                  # the week to be priced next
WEEK = f"2026-W{CAND_WEEK_N}"
WEEK_START = date(2026, 9, 21)    # Monday of 2026-W39
FIRST_HIST_N, LAST_HIST_N = 27, 38
FIRST_CLEAR_N = 34
NOISE_LAST_N = 32                 # random promos only up to here; W33 = regular price
BRANDS = ["Brand A", "Harbour Ridge", "Stonefield", "Larkspur", "Kestrel Bay"]
CATEGORIES = {"Red": 18, "White": 14, "Sparkling": 22, "Spirits": 40}   # typical shelf price
REGIONS = {"VIC": 5, "NSW": 5, "QLD": 3}                                 # stores per region
N_SINGLES = 100
PACKS = [(6, 20), (12, 8)]        # (pack_qty, how many singles get a pack of this size)
CAND_TARGET = {34: 20, 35: 28, 36: 34, 37: 38, 38: 42, 39: 45}
FLOOR_MARGIN = 1.05               # SAP floor = cost * 1.05
MAX_MARKDOWN = 0.5                # cumulative, vs shelf price (no price below 50% off)
MAX_CLEARANCE_WEEKS = 10          # SAP stops listing an item after this many weeks


def pack_type(qty: int) -> str:
    return "Single" if qty == 1 else "Multipack"


def week_id(n: int) -> str:
    return f"2026-W{n:02d}"


def week_start(n: int) -> date:
    return WEEK_START - timedelta(days=7 * (CAND_WEEK_N - n))


def build(seed: int = 42, max_clearance_weeks: int = MAX_CLEARANCE_WEEKS):
    rng = np.random.default_rng(seed)

    # ---------------- products: one sku, several pack sizes ----------------
    s = pd.DataFrame({
        "sku": [f"{100000 + i}" for i in range(N_SINGLES)],
        "brand": rng.choice(BRANDS, N_SINGLES),
        "category": rng.choice(list(CATEGORIES), N_SINGLES),
    })
    s["pack_qty"] = 1
    s["shelf_price"] = [round(CATEGORIES[c] * rng.uniform(0.6, 1.8)) - 0.01 for c in s.category]
    s["cost"] = (s.shelf_price * rng.uniform(0.45, 0.7, N_SINGLES)).round(2)
    s["name"] = s.brand + " " + s.category + " " + s.sku.str[-3:]
    e_item = dict(zip(s.sku, rng.uniform(-3.2, -0.8, N_SINGLES)))       # hidden truth per item
    u_item = dict(zip(s.sku, rng.uniform(0.4, 3.0, N_SINGLES)))         # units/store/day (single)

    pack_rows, i0 = [], 0
    for qty, n in PACKS:
        for _, p in s.iloc[i0:i0 + n].iterrows():       # same sku id, different pack_qty
            pack_rows.append({
                "sku": p.sku, "brand": p.brand, "category": p.category,
                "name": f"{p['name']} {qty}-pack", "pack_qty": qty,
                "shelf_price": round(qty * p.shelf_price, 2),      # pack regular price = qty singles
                "cost": round(qty * p.cost * 0.97, 2)})          # small bulk cost saving
        i0 += n
    products = pd.concat([s, pd.DataFrame(pack_rows)], ignore_index=True)
    products["pack_type"] = products.pack_qty.map(pack_type)
    products = products[["sku", "pack_type", "pack_qty", "brand", "category", "name",
                         "shelf_price", "cost"]]
    keys = list(zip(products.sku, products.pack_qty))
    shelf = dict(zip(keys, products.shelf_price))
    cost = dict(zip(keys, products.cost))
    elasticity = {k: e_item[k[0]] for k in keys}
    base_units = {k: u_item[k[0]] * (1.0 if k[1] == 1 else 0.3) for k in keys}
    singles = [k for k in keys if k[1] == 1]

    stores = pd.DataFrame(
        [(f"{r}{i + 1:02d}", r, f"{r}_METRO" if i < 3 else f"{r}_REGIONAL")
         for r, n in REGIONS.items() for i in range(n)],
        columns=["store_id", "region", "price_zone"])
    floor = {k: round(cost[k] * FLOOR_MARGIN, 2) for k in keys}

    # ---------------- weekly price state: promos, then clearance markdowns ----------------
    n_hist = LAST_HIST_N - FIRST_HIST_N + 1
    ph = {(k, r): [] for k in keys for r in REGIONS}         # price per history week
    promo = {k: rng.choice([1.0, 1.0, 0.9, 0.8, 1.1], NOISE_LAST_N - FIRST_HIST_N + 1)
             for k in keys}
    for n in range(FIRST_HIST_N, FIRST_CLEAR_N):
        for k in keys:
            m = promo[k][n - FIRST_HIST_N] if n <= NOISE_LAST_N else 1.0
            for r in REGIONS:
                ph[(k, r)].append(round(shelf[k] * m, 2))
    cur = {(k, r): shelf[k] for k in keys for r in REGIONS}   # W33 = regular price

    members: set[tuple] = set()
    age: dict[tuple, int] = {}
    ever: set[tuple] = set()
    cand_rows, rule_rows, inv_rows, clear_flag, wkno = [], [], [], {}, {}
    for n in range(FIRST_CLEAR_N, CAND_WEEK_N + 1):
        wk = week_id(n)
        # --- who is on this week's list (SAP decides WHAT) ---
        # an item whose next week_no would exceed the limit is no longer listed (history stays)
        keep = {k for k in members if age[k] + 2 <= max_clearance_weeks
                and not (age[k] >= 2 and rng.random() < 0.1)}
        marked = {k for k in ever if any(cur[(k, r)] < shelf[k] for r in REGIONS)}
        pool_s = [k for k in singles if k not in ever]
        pool_p = [k for k in keys if k[1] > 1 and k not in ever and (k[0], 1) in marked]
        new: list[tuple] = []
        while len(keep) + len(new) < CAND_TARGET[n] and (pool_s or pool_p):
            if pool_p and rng.random() < 0.25:
                new.append(pool_p.pop(rng.integers(len(pool_p))))
            else:
                new.append(pool_s.pop(rng.integers(len(pool_s))))
        members = keep | set(new)
        for k in members:
            age[k] = age.get(k, -1) + 1
        ever |= members
        clear_flag[n] = set(members)
        wkno[n] = {k: age[k] + 1 for k in members}

        for k in sorted(members):
            for r in REGIONS:
                cand_rows.append((wk, *k, r, shelf[k], wkno[n][k], cur[(k, r)]))   # cur = price before this week
                inv_rows.append((wk, *k, r, int(rng.integers(20, 400))))
            rule_rows.append((wk, *k, floor[k], MAX_MARKDOWN, max_clearance_weeks, f"SAP-R-{wk}"))
        if n == CAND_WEEK_N:
            break
        # --- price the members by the schedule (singles first: a pack depends on its single) ---
        for k in sorted(members, key=lambda x: (x[1] > 1, x)):
            for r in REGIONS:
                ladder = round(k[1] * cur[((k[0], 1), r)], 2) if k[1] > 1 else None
                cur[(k, r)] = recommend(shelf[k], wkno[n][k], cur[(k, r)], floor[k],
                                        MAX_MARKDOWN, ladder)["price"]
        for key, v in cur.items():
            ph[key].append(v)

    price_history = pd.DataFrame(
        [(week_id(FIRST_HIST_N + i), *k, pack_type(k[1]), r, shelf[k], ph[(k, r)][i],
          bool(k in clear_flag.get(FIRST_HIST_N + i, ())),
          wkno.get(FIRST_HIST_N + i, {}).get(k))
         for (k, r) in ph for i in range(n_hist)],
        columns=["week", "sku", "pack_qty", "pack_type", "region", "shelf_price", "price",
                 "is_clearance", "week_no"])
    price_history["week_no"] = price_history["week_no"].astype("Int64")     # null when not listed
    price_history = price_history[["week", "sku", "pack_type", "pack_qty", "region", "shelf_price",
                                   "price", "is_clearance", "week_no"]]

    # ---------------- sales history driven by the weekly shelf price ----------------
    days = np.arange(7 * n_hist)
    sale_dates = np.array([week_start(FIRST_HIST_N) + timedelta(days=int(d)) for d in days])
    wkend = np.array([d.weekday() >= 4 for d in sale_dates])
    frames = []
    for k in keys:
        for st in stores.itertuples():
            price = np.repeat(ph[(k, st.region)], 7)
            lam = base_units[k] * (price / shelf[k]) ** elasticity[k] * np.where(wkend, 1.3, 1.0)
            frames.append(pd.DataFrame({
                "sale_date": sale_dates, "sku": k[0], "pack_type": pack_type(k[1]),
                "pack_qty": k[1], "store_id": st.store_id,
                "region": st.region, "units": rng.poisson(lam), "price": price}))
    sales = pd.concat(frames, ignore_index=True)[[
        "sale_date", "sku", "pack_type", "pack_qty", "store_id", "region", "units", "price"]]

    def add_type(df, cols):
        df["pack_type"] = df.pack_qty.map(pack_type)
        return df[cols]

    candidates = add_type(pd.DataFrame(cand_rows, columns=[
        "week", "sku", "pack_qty", "region", "shelf_price", "week_no", "current_price"]),
        ["week", "sku", "pack_type", "pack_qty", "region", "shelf_price", "week_no",
         "current_price"])
    rules = add_type(pd.DataFrame(rule_rows, columns=[
        "week", "sku", "pack_qty", "price_floor", "max_markdown_pct", "max_clearance_weeks",
        "rule_version"]),
        ["week", "sku", "pack_type", "pack_qty", "price_floor", "max_markdown_pct",
         "max_clearance_weeks", "rule_version"])
    inventory = add_type(pd.DataFrame(inv_rows, columns=[
        "week", "sku", "pack_qty", "region", "on_hand"]),
        ["week", "sku", "pack_type", "pack_qty", "region", "on_hand"])

    # ---------------- planted W39 issues (error taxonomy, PROJECT_CONTEXT s2) ----------------
    w39 = sorted(k for k in clear_flag[CAND_WEEK_N] if k[1] == 1)
    (a, _), (b, _), (c, _), (d, _) = (w39 + [("", 1)] * 4)[:4]      # fewer only in tiny test runs
    is39 = rules.week == WEEK
    rules = rules[~(is39 & rules.sku.isin([a, b]) & (rules.pack_qty == 1))]  # missing rule
    top = candidates[(candidates.week == WEEK) & (candidates.sku == c)
                     & (candidates.pack_qty == 1)].current_price.max()
    rules.loc[is39 & (rules.sku == c) & (rules.pack_qty == 1), "price_floor"] = top + 1
    inventory.loc[(inventory.week == WEEK) & (inventory.sku == d)
                  & (inventory.pack_qty == 1), "on_hand"] = 0
    candidates = pd.concat([candidates, pd.DataFrame(
        [(WEEK, "999999", "Single", 1, r, 24.99, 1, 19.99) for r in REGIONS],
        columns=candidates.columns)], ignore_index=True)                     # no product master
    seed_manifest = pd.DataFrame([
        (WEEK, a, "Single", 1, "MISSING_RULE", "no floor in rules snapshot"),
        (WEEK, b, "Single", 1, "MISSING_RULE", "no floor in rules snapshot"),
        (WEEK, c, "Single", 1, "FLOOR_ABOVE_PRICE", "floor exceeds current price"),
        (WEEK, d, "Single", 1, "ZERO_INVENTORY", "on_hand = 0 in all regions"),
        (WEEK, "999999", "Single", 1, "NOT_IN_PRODUCTS", "candidate has no product master")],
        columns=["week", "sku", "pack_type", "pack_qty", "issue", "detail"])

    # SAP holds the prices it published in the past weeks (W34-W38) as condition records
    cond_rows = []
    for h in price_history[price_history.is_clearance].itertuples(index=False):
        n = int(h.week[-2:])
        vf, vt = week_start(n), week_start(n) + timedelta(days=6)
        cond_rows.append((f"SEED-{h.week}|{h.sku}|{h.pack_qty}|{h.region}|{vf}", f"SEED-{h.week}", h.week,
                          h.sku, int(h.pack_qty), h.region, float(h.price), vf, vt))
    price_conditions = pd.DataFrame(cond_rows, columns=[
        "idem_key", "run_id", "week", "sku", "pack_qty", "region", "price", "valid_from", "valid_to"])

    weeks = pd.DataFrame(
        [(week_id(n), week_start(n),
          "history" if n < FIRST_CLEAR_N else "published" if n < CAND_WEEK_N else "open")
         for n in range(FIRST_HIST_N, CAND_WEEK_N + 1)],
        columns=["week", "week_start", "status"])

    return {"weeks": weeks, "products": products, "stores": stores, "sales": sales,
            "price_history": price_history, "inventory": inventory,
            "clearance_candidates": candidates, "business_rules": rules,
            "price_conditions": price_conditions, "seed_manifest": seed_manifest}


SAP_TABLES = ("clearance_candidates", "business_rules", "price_conditions")


def write(path: str, seed: int = 42, with_sap_tables: bool = False,
          max_clearance_weeks: int = MAX_CLEARANCE_WEEKS) -> dict[str, int]:
    """Write the warehouse tables to DuckDB. SAP-owned tables are skipped unless
    with_sap_tables=True (used by tests that inspect the generated data directly)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if os.path.exists(path):
        os.remove(path)
    con = duckdb.connect(path)
    counts = {}
    for name, df in build(seed, max_clearance_weeks).items():
        if name in SAP_TABLES and not with_sap_tables:
            continue
        con.register("df", df)
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM df")
        con.unregister("df")
        counts[name] = len(df)
    con.close()
    return counts


def _sap_has_data() -> bool:
    from shared import db
    from shared.sap_schema import schema
    with db.connect() as con:
        return con.execute("SELECT to_regclass(%s) AS t", (f"{schema()}.price_conditions",)).fetchone()["t"] is not None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--path", default=os.environ.get("DUCKDB_PATH", "data/warehouse.duckdb"))
    ap.add_argument("--force", action="store_true", help="overwrite existing DuckDB file / SAP data (full reset)")
    ap.add_argument("--skip-sap", action="store_true", help="do not seed mock-sap's Postgres")
    ap.add_argument("--skip-ingest", action="store_true", help="do not ingest W34-W39 from SAP into DuckDB")
    args = ap.parse_args(argv)

    if not args.force:
        existing = [f"DuckDB file {args.path}"] if os.path.exists(args.path) else []
        if not args.skip_sap and _sap_has_data():
            existing.append("SAP schema in Postgres")
        if existing:
            print("Refusing to overwrite existing history: " + " and ".join(existing)
                  + ".\nThis command is a full reset. Re-run with --force to wipe and regenerate.")
            return 1

    data = build(args.seed)
    print(f"DuckDB -> {args.path}")
    for name, n in write(args.path, args.seed).items():
        print(f"  {name:22s} {n:>8d}")
    if args.skip_sap:
        return 0
    from jobs.data_gen.seed_sap import seed_sap
    print("SAP Postgres")
    for name, n in seed_sap({k: v for k, v in data.items() if k in SAP_TABLES}).items():
        print(f"  {name:22s} {n:>8d}")
    if not args.skip_ingest:
        import httpx

        from jobs.sap_ingest import ALL_WEEKS, ingest, sap_client
        try:
            with sap_client() as c:
                print("ingest SAP -> DuckDB (all weeks)")
                for w in ALL_WEEKS:
                    print(f"  {w}", ingest(c, w, args.path))
        except httpx.HTTPError as e:
            print(f"  mock-sap not reachable ({e!r}); start it and run: python -m jobs.sap_ingest --all-weeks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
