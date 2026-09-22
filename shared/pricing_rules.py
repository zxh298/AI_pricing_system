"""Deterministic pricing core: the markdown schedule and the hard price rules.

Used by the pricing engine and by the synthetic-data generator, so history and recommendations
follow exactly the same rules. No LLM and no I/O here.

Schedule: week_no 1 has no discount; each later week adds DISCOUNT_STEP (5 points of shelf price):
    discount(week_no) = 0.05 * (week_no - 1)        price = shelf_price * (1 - discount)

Hard rules applied on top of the schedule (in this order of precedence for the lower bound):
    - price >= SAP floor
    - price >= shelf_price * (1 - max_markdown_pct)      (no more than 50% off)
    - multipack per-item price >= the Single's price (ladder), passed in as `ladder_min`
    - price <= last week's price (never goes up)
"""
from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal

DISCOUNT_STEP = 0.05


def discount_pct(week_no: int) -> float:
    return round(DISCOUNT_STEP * (week_no - 1), 4)


def scheduled_price(shelf_price: float, week_no: int) -> float:
    """Shelf price after the scheduled discount, rounded half-up to cents."""
    d = Decimal(str(shelf_price)) * (1 - Decimal(str(DISCOUNT_STEP)) * (week_no - 1))
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def ceil_cents(x: float) -> float:
    return math.ceil(round(x * 100, 6)) / 100


def recommend(shelf_price: float, week_no: int, current_price: float, price_floor: float,
              max_markdown_pct: float, ladder_min: float | None = None) -> dict:
    """Returns {"price", "binding", "error"}.

    binding says what set the price: SCHEDULE, FLOOR, MAX_MARKDOWN, LADDER or LAST_WEEK.
    error is set (and price is None) when the lower bound is above last week's price, so no
    valid price exists: FLOOR_ABOVE_PRICE or LADDER_ABOVE_PRICE.
    """
    bounds = {"FLOOR": price_floor, "MAX_MARKDOWN": shelf_price * (1 - max_markdown_pct)}
    if ladder_min is not None:
        bounds["LADDER"] = ladder_min
    name, lo = max(bounds.items(), key=lambda kv: kv[1])
    lo = ceil_cents(lo)
    if lo > current_price + 1e-9:
        err = "LADDER_ABOVE_PRICE" if name == "LADDER" else "FLOOR_ABOVE_PRICE"
        return {"price": None, "binding": None, "error": err}
    sched = scheduled_price(shelf_price, week_no)
    price, binding = (sched, "SCHEDULE") if sched >= lo else (lo, name)
    if price > current_price:
        price, binding = current_price, "LAST_WEEK"
    return {"price": price, "binding": binding, "error": None}
