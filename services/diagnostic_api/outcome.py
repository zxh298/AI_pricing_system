"""Deterministic outcome checks: did a clean record actually sell? No LLM, no I/O.

The record checks in core.py ask "is this price right and in effect?". A record can pass all of them and still not
sell, and nothing in the record says why. LOW_SELL_THROUGH is that signal. Sales for the week being priced do not exist
yet when the pipeline finishes, so a scan looks at the last completed week.

"Sell-through" here means units sold against the record's own recent baseline, not against stock: on_hand in the
synthetic data is unrelated to sales volume, so a units / stock ratio would mostly measure noise. A record is flagged
when units fall to `drop_ratio` of the baseline or less and the fall is at least MIN_Z standard deviations (a Poisson
approximation), so a small seller's normal swing is not an alarm.

The signal functions below are what the agent's tools show and what code re-checks afterwards: a hypothesis only
counts as supported if the same function finds the same thing.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, timedelta

BASELINE_WEEKS = 3
MIN_BASELINE_WEEKS = 2
MIN_Z = 3.0
EPS = 0.005
PACK_GAP_POINTS = 10.0      # the per-unit gap between pack and single grew by at least this many points
RISE_RATIO = 1.5            # a record selling this far above its baseline is drawing shoppers from somewhere
MIN_SHIFT_SHARE = 0.25      # a rival's extra units must cover at least this share of the units lost

Key = tuple                 # (sku, pack_qty, region)


def week_dates(week: str) -> tuple[date, date]:
    year, number = week.split("-W")
    start = date.fromisocalendar(int(year), int(number), 1)
    return start, start + timedelta(days=6)


def shift_week(week: str, weeks: int) -> str:
    y, w, _ = (week_dates(week)[0] + timedelta(weeks=weeks)).isocalendar()
    return f"{y}-W{w:02d}"


def units_by_record(rows: list[dict]) -> dict[Key, int]:
    out: dict[Key, int] = defaultdict(int)
    for r in rows:
        out[(r["sku"], r["pack_qty"], r["region"])] += int(r["units"] or 0)
    return dict(out)


def baselines(history: list[dict[Key, int]]) -> dict[Key, float]:
    """Mean weekly units over the weeks a record has sales in; `history` is one {record: units} per earlier week."""
    seen: dict[Key, list[int]] = defaultdict(list)
    for week in history:
        for key, units in week.items():
            seen[key].append(units)
    return {k: sum(v) / len(v) for k, v in seen.items() if len(v) >= MIN_BASELINE_WEEKS}


def is_low(actual: float, baseline: float, drop_ratio: float, min_baseline: int) -> bool:
    if baseline < min_baseline:
        return False
    return actual / baseline <= drop_ratio and (baseline - actual) / math.sqrt(baseline) >= MIN_Z


def clean_records(cands: list[dict], conditions: list[dict]) -> list[dict]:
    """Records of the week SAP held at our price, with nothing overriding it: nothing is wrong with the record."""
    ours = {(c["sku"], c["pack_qty"], c["region"]) for c in conditions
            if c["effective_source"] == "MARKDOWN" and abs(c["effective_price"] - c["price"]) <= EPS}
    return [c for c in cands if (c["sku"], c["pack_qty"], c["region"]) in ours]


def low_sell_through(records: list[dict], actual: dict[Key, int], baseline: dict[Key, float], drop_ratio: float,
                     min_baseline: int) -> tuple[list[dict], int]:
    """(flagged records, how many had a usable baseline). A record with no sales row sold nothing."""
    flagged, checked = [], 0
    for c in records:
        key = (c["sku"], c["pack_qty"], c["region"])
        base = baseline.get(key)
        if base is None or base < min_baseline:
            continue
        checked += 1
        units = actual.get(key, 0)
        if is_low(units, base, drop_ratio, min_baseline):
            flagged.append({"sku": c["sku"], "pack_qty": c["pack_qty"], "region": c["region"], "brand": c["brand"],
                            "actual_units": units, "baseline_units": round(base, 1), "ratio": round(units / base, 2),
                            "lost_units": round(base - units)})
    return flagged, checked


# ---------------- signals: shown by the agent's tools, re-checked by code ----------------
def sell_through(rows: list[dict], baseline_rows: list[list[dict]], by: str, keep=None) -> list[dict]:
    """Units against the baseline, grouped by `by`: region, price_zone, store_id, pack_qty or sku. The baseline is
    the same grouping over the earlier weeks, so a store level comparison uses that store's own history."""
    def grouped(week_rows):
        out: dict = defaultdict(int)
        for r in week_rows:
            if keep is None or keep(r):
                out[r[by]] += int(r["units"] or 0)
        return out

    now, earlier = grouped(rows), [grouped(w) for w in baseline_rows]
    out = []
    for group in sorted({g for w in earlier for g in w} | set(now), key=str):
        past = [w[group] for w in earlier if group in w]
        base = sum(past) / len(past) if past else 0
        out.append({by: group, "actual_units": now.get(group, 0), "baseline_units": round(base, 1),
                    "ratio": round(now.get(group, 0) / base, 2) if base else None})
    return sorted(out, key=lambda x: (x["ratio"] is None, x["ratio"] if x["ratio"] is not None else 0))


def pos_gaps(rows: list[dict], conditions: list[dict]) -> list[dict]:
    """Stores selling above the price SAP says is in effect: the markdown did not reach the till. `list_price` is the
    highest price recorded, so a store that sold nothing at the wrong price still shows it."""
    held = {(c["sku"], c["pack_qty"], c["region"]): c["effective_price"] for c in conditions}
    out = []
    for r in rows:
        price = held.get((r["sku"], r["pack_qty"], r["region"]))
        if price is not None and r["list_price"] is not None and r["list_price"] - price > EPS:
            out.append({"sku": r["sku"], "pack_qty": r["pack_qty"], "region": r["region"], "store_id": r["store_id"],
                        "price_zone": r["price_zone"], "pos_price": round(float(r["list_price"]), 2),
                        "sap_effective_price": price})
    return out


def stock_rows(on_hand: dict[Key, int], actual: dict[Key, int], baseline: dict[Key, float]) -> list[dict]:
    """Opening stock against normal demand. `sold_out`: the week opened with less than a normal week's sales and all of
    it sold. Low stock alone proves nothing: a product that stopped selling for another reason can have low stock too."""
    out = []
    for key, stock in sorted(on_hand.items(), key=str):
        base = baseline.get(key)
        out.append({"sku": key[0], "pack_qty": key[1], "region": key[2], "opening_on_hand": stock,
                    "units_sold": actual.get(key, 0), "baseline_units": None if base is None else round(base, 1),
                    "sold_out": base is not None and stock < base and actual.get(key, 0) >= stock})
    return out


def discount_rows(history: list[dict], week: str) -> list[dict]:
    """Price per week for each record. `stalled`: the markdown did not deepen in `week`."""
    earlier = shift_week(week, -1)
    by_key: dict[Key, dict[str, dict]] = defaultdict(dict)
    for h in history:
        if h["week"] <= week:
            by_key[(h["sku"], h["pack_qty"], h["region"])][h["week"]] = h
    out = []
    for key, weeks in sorted(by_key.items(), key=str):
        now, before = weeks.get(week), weeks.get(earlier)
        if now is None:
            continue
        recent = sorted(weeks)[-4:]
        out.append({"sku": key[0], "pack_qty": key[1], "region": key[2], "shelf_price": now["shelf_price"],
                    "prices": {w: weeks[w]["price"] for w in recent},
                    "discount_pct": round((1 - now["price"] / now["shelf_price"]) * 100, 1),
                    "stalled": before is not None and now["price"] >= before["price"] - EPS})
    return out


def rising(actual: dict[Key, int], baseline: dict[Key, float], products: dict[tuple, dict], region: str,
           min_baseline: int) -> list[dict]:
    """Records of a region selling well above their baseline, biggest rise first. This can name a product that is not
    in the case: something the investigation only learns by asking."""
    out = []
    for key, base in baseline.items():
        units = actual.get(key, 0)
        if key[2] == region and base >= min_baseline and units / base >= RISE_RATIO:
            p = products.get(key[:2], {})
            out.append({"sku": key[0], "pack_qty": key[1], "region": region, "brand": p.get("brand"), "category": p.get("category"),
                        "actual_units": units, "baseline_units": round(base, 1), "ratio": round(units / base, 2)})
    return sorted(out, key=lambda r: -r["ratio"])


def explains_loss(gained_units: float, lost_units: float) -> bool:
    """Could the rise account for a meaningful part of the fall? Counted in single units, so packs compare fairly.
    A necessary check, not proof: without basket data nobody can show the same shoppers moved."""
    return lost_units > 0 and gained_units >= MIN_SHIFT_SHARE * lost_units


def pack_gaps(conditions: list[dict]) -> list[dict]:
    """Each multipack against its single, per unit, as we priced them and as shoppers pay. `gap_widened`: the single
    became that much cheaper than the pack because of an override on one of them, so the two were not priced together."""
    held = {(c["sku"], c["pack_qty"], c["region"]): c for c in conditions}
    out = []
    for (sku, qty, region), pack in sorted(held.items(), key=str):
        single = held.get((sku, 1, region))
        if qty == 1 or single is None:
            continue
        sent = (pack["price"] / qty - single["price"]) / single["price"] * 100
        paid = (pack["effective_price"] / qty - single["effective_price"]) / single["effective_price"] * 100
        out.append({"sku": sku, "pack_qty": qty, "region": region,
                    "single_sent": single["price"], "single_effective": single["effective_price"],
                    "single_effective_source": single["effective_source"],
                    "pack_per_unit_sent": round(pack["price"] / qty, 2),
                    "pack_per_unit_effective": round(pack["effective_price"] / qty, 2),
                    "pack_premium_pct_sent": round(sent, 1), "pack_premium_pct_effective": round(paid, 1),
                    "gap_widened": paid - sent >= PACK_GAP_POINTS})
    return out
