"""Pre-send validation: an independent re-check of every hard rule on the rows about to be sent.

The engine already applies these rules; this gate exists so a bug in the engine (or bad input)
can never reach SAP. Any violation blocks the send and fails the run. It deliberately does not
call shared.pricing_rules.
"""
from __future__ import annotations

EPS = 1e-9


def validate_rows(rows: list[dict], rules: dict[tuple, dict]) -> list[dict]:
    """rows: engine output. rules: {(sku, pack_qty): rule}. Returns a list of violations."""
    out: list[dict] = []
    priced = [r for r in rows if r["status"] == "PRICED"]
    price = {(r["sku"], r["pack_qty"], r["region"]): r["recommended_price"] for r in priced}
    current = {(r["sku"], r["pack_qty"], r["region"]): r["current_price"] for r in rows}
    seen: set[tuple] = set()

    def bad(r, code, msg):
        out.append({"sku": r["sku"], "pack_qty": r["pack_qty"], "region": r["region"], "code": code, "message": msg})

    for r in priced:
        key = (r["sku"], r["pack_qty"], r["region"])
        p, rule = r["recommended_price"], rules.get((r["sku"], r["pack_qty"]))
        if key in seen:
            bad(r, "DUPLICATE_KEY", "row appears more than once")
        seen.add(key)
        if p is None or p <= 0 or abs(p * 100 - round(p * 100)) > 1e-6:
            bad(r, "BAD_PRICE_FORMAT", f"price {p} must be positive with at most 2 decimals")
            continue
        if p > r["current_price"] + EPS:
            bad(r, "PRICE_ABOVE_LAST_WEEK", f"{p} > {r['current_price']}")
        if rule is None:
            bad(r, "MISSING_RULE", "priced row has no rule")
            continue
        if p < rule["price_floor"] - EPS:
            bad(r, "BELOW_FLOOR", f"{p} < floor {rule['price_floor']}")
        if p < r["shelf_price"] * (1 - rule["max_markdown_pct"]) - 0.005:
            bad(r, "BELOW_MAX_MARKDOWN", f"{p} is more than {rule['max_markdown_pct']:.0%} off shelf {r['shelf_price']}")
        if r["week_no"] > rule["max_clearance_weeks"]:
            bad(r, "EXCEEDS_MAX_WEEKS", f"week_no {r['week_no']} > {rule['max_clearance_weeks']}")
        if r["pack_qty"] > 1:                                     # multipack per-item price >= Single's
            ref = price.get((r["sku"], 1, r["region"]), current.get((r["sku"], 1, r["region"])))
            if ref is not None and p / r["pack_qty"] < ref - 0.005 / r["pack_qty"] - EPS:
                bad(r, "LADDER_VIOLATION", f"per-item {p / r['pack_qty']:.4f} < Single {ref}")
    return out
