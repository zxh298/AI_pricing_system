"""Unit tests for the deterministic pricing core (no database)."""
import pytest

from shared.pricing_rules import ceil_cents, discount_pct, recommend, scheduled_price


def test_schedule_five_points_per_week():
    assert [discount_pct(n) for n in range(1, 11)] == [0, .05, .10, .15, .20, .25, .30, .35, .40, .45]
    assert scheduled_price(100.0, 1) == 100.0                 # first week: no discount
    assert scheduled_price(100.0, 2) == 95.0
    assert scheduled_price(100.0, 10) == 55.0
    assert scheduled_price(36.99, 2) == 35.14                 # 35.1405 -> cents, half-up
    assert scheduled_price(0.5, 2) == 0.48                    # 0.475 rounds half-up, not banker's


def test_max_ten_weeks_never_reaches_half_off():
    assert scheduled_price(100.0, 10) > 50.0                  # so the 50% rule is only a safety net


def r(**kw):
    base = dict(shelf_price=100.0, week_no=3, current_price=95.0, price_floor=40.0, max_markdown_pct=0.5)
    return recommend(**(base | kw))


def test_follows_schedule():
    assert r() == {"price": 90.0, "binding": "SCHEDULE", "error": None}


def test_floor_beats_schedule():
    assert r(price_floor=92.0)["price"] == 92.0 and r(price_floor=92.0)["binding"] == "FLOOR"


def test_never_below_half_of_shelf():
    res = r(week_no=30, current_price=95.0, price_floor=10.0)       # schedule would go negative
    assert res["price"] == 50.0 and res["binding"] == "MAX_MARKDOWN"


def test_never_above_last_week():
    res = r(week_no=2, current_price=80.0)                          # schedule 95 > last week's 80
    assert res == {"price": 80.0, "binding": "LAST_WEEK", "error": None}


def test_ladder_lifts_multipack_to_single_price():
    res = r(ladder_min=93.5)
    assert res["price"] == 93.5 and res["binding"] == "LADDER"
    assert r(ladder_min=80.0)["price"] == 90.0                      # ladder below schedule: no effect


def test_no_valid_price_when_floor_above_current():
    assert r(price_floor=99.0) == {"price": None, "binding": None, "error": "FLOOR_ABOVE_PRICE"}
    assert r(ladder_min=99.0)["error"] == "LADDER_ABOVE_PRICE"


def test_ceil_cents():
    assert ceil_cents(10.001) == 10.01 and ceil_cents(10.0) == 10.0 and ceil_cents(12.34) == 12.34
