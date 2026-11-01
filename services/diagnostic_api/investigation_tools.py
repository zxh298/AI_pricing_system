"""Read-only tools for the triage agent (investigate.py): what happened to sales, and what could explain it.

A second allow-list, separate from tools.py, so these are not callable from chat. Everything here is computed by
outcome.py; the model only picks what to look at next. The agent also gets two tools from tools.py:
get_sap_conditions and search_docs.
"""
from __future__ import annotations

from services.diagnostic_api import outcome
from services.diagnostic_api.tools import (ToolContext, ToolError, _as_of, _sap_get, _schema, _skus, _week, TOOLS,
                                           _regions, TOOL_SCHEMAS)
from shared.config import load_config

MAX_ROWS = 60
GROUPINGS = {"region": "region", "zone": "price_zone", "store": "store_id", "pack": "pack_qty", "sku": "sku"}


def _sales(ctx: ToolContext, week: str, skus: list[str] | None = None) -> list[dict]:
    return ctx.wh.get_store_sales(*outcome.week_dates(week), skus)


def _history(ctx: ToolContext, week: str, skus: list[str] | None) -> list[list[dict]]:
    weeks = [outcome.shift_week(week, -i) for i in range(1, outcome.BASELINE_WEEKS + 1)]
    return [rows for rows in (_sales(ctx, w, skus) for w in weeks) if rows]


def _conditions(ctx: ToolContext, week: str, skus: list[str], regions: list[str]) -> list[dict]:
    return [c for c in _sap_get(ctx, "/pricing/conditions", week=week)["items"]
            if c["sku"] in skus and c["region"] in regions]


def _scope(ctx: ToolContext, week, skus, regions) -> tuple[str, list[str], list[str]]:
    week = _week(week)
    return week, _skus(skus), _regions(ctx, ctx.wh.get_candidates(week), regions)


def _capped(rows: list[dict]) -> dict:
    return {"rows": rows[:MAX_ROWS], "row_count": len(rows), "truncated": len(rows) > MAX_ROWS}


def compare_sell_through(ctx: ToolContext, week: str, skus: list[str], regions: list[str] | None = None,
                         by: str = "region", pack_qty: int | None = None) -> dict:
    week, skus, regions = _scope(ctx, week, skus, regions)
    if by not in GROUPINGS:
        raise ToolError(f"by must be one of {sorted(GROUPINGS)}")

    def keep(r):
        return r["region"] in regions and (pack_qty is None or r["pack_qty"] == pack_qty)

    rows = outcome.sell_through(_sales(ctx, week, skus), _history(ctx, week, skus), GROUPINGS[by], keep)
    return {"as_of": _as_of(), "week": week, "by": by, **_capped(rows),
            "fields": {"ratio": "actual_units / baseline_units; 1.0 is normal, below 0.6 is a large fall",
                       "baseline_units": f"mean of up to {outcome.BASELINE_WEEKS} weeks before this one"}}


def get_pos_prices(ctx: ToolContext, week: str, skus: list[str], regions: list[str] | None = None) -> dict:
    week, skus, regions = _scope(ctx, week, skus, regions)
    rows = [r for r in _sales(ctx, week, skus) if r["region"] in regions]
    gaps = outcome.pos_gaps(rows, _conditions(ctx, week, skus, regions))
    return {"as_of": _as_of(), "week": week, "store_records_checked": len(rows), "gap_count": len(gaps), **_capped(gaps),
            "fields": {"rows": "only store records selling above the price SAP has in effect; none means the tills match"}}


def get_stock_trend(ctx: ToolContext, week: str, skus: list[str], regions: list[str] | None = None) -> dict:
    week, skus, regions = _scope(ctx, week, skus, regions)
    on_hand = {(i["sku"], i["pack_qty"], i["region"]): i["on_hand"] for i in ctx.wh.get_inventory(skus, week)
               if i["region"] in regions}
    actual = outcome.units_by_record(_sales(ctx, week, skus))
    base = outcome.baselines([outcome.units_by_record(w) for w in _history(ctx, week, skus)])
    rows = outcome.stock_rows(on_hand, actual, base)
    return {"as_of": _as_of(), "week": week, "sold_out": sum(r["sold_out"] for r in rows), **_capped(rows),
            "fields": {"sold_out": "the week opened with less than a normal week's sales, and all of it sold"}}


def get_discount_history(ctx: ToolContext, week: str, skus: list[str], regions: list[str] | None = None) -> dict:
    week, skus, regions = _scope(ctx, week, skus, regions)
    history = [h for r in regions for h in ctx.wh.get_price_history(skus, r)]
    rows = outcome.discount_rows(history, week)
    return {"as_of": _as_of(), "week": week, "stalled": sum(r["stalled"] for r in rows), **_capped(rows),
            "fields": {"stalled": "the markdown did not deepen this week", "prices": "price per recent week"}}


def compare_pack_prices(ctx: ToolContext, week: str, skus: list[str], regions: list[str] | None = None) -> dict:
    week, skus, regions = _scope(ctx, week, skus, regions)
    rows = outcome.pack_gaps(_conditions(ctx, week, skus, regions))
    return {"as_of": _as_of(), "week": week, "gap_widened": sum(r["gap_widened"] for r in rows), **_capped(rows),
            "fields": {"pack_premium_pct_effective": "how much more a pack's unit costs than the single, in effect",
                       "gap_widened": "an override moved the single and the pack apart; the pricing rules never see this"}}


def find_rising_products(ctx: ToolContext, week: str, region: str, category: str | None = None) -> dict:
    """The whole region, not just the case: what sold far above normal, so what shoppers may have moved to."""
    week = _week(week)
    region = _regions(ctx, ctx.wh.get_candidates(week), [region])[0]
    rows = _rising(ctx, week, region)
    rows = [r for r in rows if category is None or (r["category"] or "").lower() == category.lower()]
    return {"as_of": _as_of(), "week": week, "region": region, **_capped(rows),
            "fields": {"ratio": "actual_units / baseline_units", "note": "a rise says shoppers bought it; ask get_sap_conditions why"}}


def _rising(ctx: ToolContext, week: str, region: str) -> list[dict]:
    rows = _sales(ctx, week)
    skus = sorted({r["sku"] for r in rows})
    products = {(p["sku"], p["pack_qty"]): p for p in ctx.wh.get_products(skus)}
    return outcome.rising(outcome.units_by_record(rows), outcome.baselines([outcome.units_by_record(w) for w in _history(ctx, week, None)]),
                          products, region, load_config().triage_min_baseline)


INVESTIGATION_TOOLS = {f.__name__: f for f in (compare_sell_through, get_pos_prices, get_stock_trend, get_discount_history,
                                               compare_pack_prices, find_rising_products)}
INVESTIGATION_TOOLS |= {n: TOOLS[n] for n in ("get_sap_conditions", "search_docs")}

_WEEK = {"type": "string", "description": "ISO week id such as 2026-W38"}
_SKUS = {"type": "array", "items": {"type": "string"}, "description": "skus to look at"}
_REGIONS = {"type": "array", "items": {"type": "string"}, "description": "regions to include; default all"}

INVESTIGATION_SCHEMAS = [
    _schema("compare_sell_through", "Units sold against the recent baseline, grouped by region, zone, store, pack or "
            "sku. Use it to see where a fall is concentrated, and whether something else rose.",
            {"week": _WEEK, "skus": _SKUS, "regions": _REGIONS,
             "by": {"type": "string", "enum": sorted(GROUPINGS)},
             "pack_qty": {"type": "integer", "description": "only this pack size"}}, ["week", "skus"]),
    _schema("get_pos_prices", "Stores whose till price is above the price SAP has in effect.",
            {"week": _WEEK, "skus": _SKUS, "regions": _REGIONS}, ["week", "skus"]),
    _schema("get_stock_trend", "Opening stock of the week against normal weekly sales.",
            {"week": _WEEK, "skus": _SKUS, "regions": _REGIONS}, ["week", "skus"]),
    _schema("get_discount_history", "Prices of recent weeks and whether the markdown deepened this week.",
            {"week": _WEEK, "skus": _SKUS, "regions": _REGIONS}, ["week", "skus"]),
    _schema("compare_pack_prices", "Each multipack against its single per unit, as priced and as shoppers pay. Pass "
            "the sku; both pack sizes are looked up.", {"week": _WEEK, "skus": _SKUS, "regions": _REGIONS}, ["week", "skus"]),
    _schema("find_rising_products", "Records in a whole region selling far above normal, including products outside this "
            "case. Use it to see where shoppers went.",
            {"week": _WEEK, "region": {"type": "string"}, "category": {"type": "string"}}, ["week", "region"]),
] + [s for s in TOOL_SCHEMAS if s["name"] in ("get_sap_conditions", "search_docs")]


def supported_signals(ctx: ToolContext, week: str, skus: list[str], regions: list[str]) -> dict[str, set]:
    """What the deterministic functions find for these records, keyed by cause. The agent cannot influence this."""
    limit = load_config()
    rows = [r for r in _sales(ctx, week, skus) if r["region"] in regions]
    conditions = _conditions(ctx, week, skus, regions)
    actual = outcome.units_by_record(rows)
    base = outcome.baselines([outcome.units_by_record(w) for w in _history(ctx, week, skus)])
    on_hand = {(i["sku"], i["pack_qty"], i["region"]): i["on_hand"] for i in ctx.wh.get_inventory(skus, week)
               if i["region"] in regions}
    history = [h for r in regions for h in ctx.wh.get_price_history(skus, r)]
    low = {k for k, v in base.items() if outcome.is_low(actual.get(k, 0), v, limit.triage_drop_ratio, limit.triage_min_baseline)}
    key = lambda r: (r["sku"], r["pack_qty"], r["region"])
    return {"POS_PRICE_GAP": {key(r) for r in outcome.pos_gaps(rows, conditions)},
            "STOCK_SHORTAGE": {key(r) for r in outcome.stock_rows(on_hand, actual, base) if r["sold_out"]},
            "DISCOUNT_STALLED": {key(r) for r in outcome.discount_rows(history, week) if r["stalled"]},
            "CANNIBALIZATION": {key(r) for r in outcome.pack_gaps(conditions) if r["gap_widened"]} & low,
            "SUBSTITUTION": _substituted(ctx, week, regions, low, actual, base)}


def _substituted(ctx: ToolContext, week: str, regions: list[str], low: set, actual: dict, base: dict) -> set:
    """Low records of a category in which products under a promotion rose by enough to explain a real part of the fall."""
    held = {(c["sku"], c["pack_qty"], c["region"]): c for c in _sap_get(ctx, "/pricing/conditions", week=week)["items"]}
    products = {(p["sku"], p["pack_qty"]): p for p in ctx.wh.get_products(sorted({k[0] for k in low}))}
    out = set()
    for region in regions:
        gained: dict = {}
        for r in _rising(ctx, week, region):
            if held.get((r["sku"], r["pack_qty"], r["region"]), {}).get("effective_source") == "PROMOTION":
                gained[r["category"]] = gained.get(r["category"], 0) + (r["actual_units"] - r["baseline_units"]) * r["pack_qty"]
        for category, units in gained.items():
            hit = {k for k in low if k[2] == region and products.get(k[:2], {}).get("category") == category}
            if outcome.explains_loss(units, sum((base[k] - actual.get(k, 0)) * k[1] for k in hit)):
                out |= hit
    return out
