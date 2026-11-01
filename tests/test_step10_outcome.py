"""Step 10: the deterministic half of the weekly triage. Nothing here needs Postgres, a key or the network."""
import pytest

from services.diagnostic_api import outcome
from services.diagnostic_api.triage_rules import (BUSINESS, CONFIRMED, DEVELOPMENT, DEVELOPMENT_TEAM, LIKELY, UNCLASSIFIED,
                                                  outcome_cases, rank, record_cases, route_for)

BOOK = {"owner": "Promotions team", "severity": "warning", "doc_id": "PB-X", "why": "because", "what_to_do": "do it",
        "what_not_to_do": "not this"}


def v(code, reason="", sku="1", region="VIC", detail="seen"):
    return {"sku": sku, "pack_qty": 1, "region": region, "code": code, "reason": reason, "detail": detail}


def cond(sku="1", qty=1, region="VIC", price=10.0, effective=None, source="MARKDOWN"):
    return {"sku": sku, "pack_qty": qty, "region": region, "price": price,
            "effective_price": price if effective is None else effective, "effective_source": source}


def rec(sku="1", qty=1, region="VIC", brand="B"):
    return {"sku": sku, "pack_qty": qty, "region": region, "brand": brand}


# ======================= weeks =======================
def test_week_helpers_cross_a_year_boundary():
    assert outcome.shift_week("2027-W01", -1) == "2026-W53"
    assert outcome.shift_week("2026-W39", -1) == "2026-W38"
    start, end = outcome.week_dates("2026-W39")
    assert (str(start), str(end)) == ("2026-09-21", "2026-09-27")


# ======================= LOW_SELL_THROUGH =======================
KEY = ("1", 1, "VIC")


@pytest.mark.parametrize("actual,base,low", [(10, 100, True), (60, 100, True), (61, 100, False), (0, 19, False),
                                             (10, 20, False), (0, 20, True)])
def test_is_low_needs_both_the_ratio_and_the_size_of_the_fall(actual, base, low):
    assert outcome.is_low(actual, base, 0.6, 20) is low


def test_a_record_with_no_sales_row_sold_nothing():
    flagged, checked = outcome.low_sell_through([rec()], {}, {KEY: 100.0}, 0.6, 20)
    assert checked == 1 and flagged[0]["actual_units"] == 0 and flagged[0]["lost_units"] == 100


def test_a_record_without_a_usable_baseline_is_not_judged():
    assert outcome.low_sell_through([rec()], {}, {KEY: 5.0}, 0.6, 20) == ([], 0)
    assert outcome.low_sell_through([rec()], {}, {}, 0.6, 20) == ([], 0)


def test_baseline_needs_more_than_one_week_of_history():
    assert outcome.baselines([{KEY: 10}]) == {}
    assert outcome.baselines([{KEY: 10}, {KEY: 20}, {}]) == {KEY: 15.0}


def test_only_records_with_nothing_wrong_are_judged_on_sales():
    cands = [rec("1"), rec("2"), rec("3")]
    conditions = [cond("1"), cond("2", effective=7.0, source="PROMOTION")]           # 3: SAP holds nothing
    assert [c["sku"] for c in outcome.clean_records(cands, conditions)] == ["1"]


# ======================= signals =======================
def sale(store="VIC01", zone="VIC_METRO", units=10, price=10.0, sku="1", qty=1, region="VIC"):
    return {"sku": sku, "pack_qty": qty, "region": region, "store_id": store, "price_zone": zone, "units": units,
            "avg_price": price, "list_price": price}


def test_sell_through_compares_each_group_with_its_own_history():
    now = [sale("VIC01", units=10), sale("VIC02", units=100)]
    before = [[sale("VIC01", units=100), sale("VIC02", units=100)]] * 3
    rows = outcome.sell_through(now, before, "store_id")
    assert rows[0] == {"store_id": "VIC01", "actual_units": 10, "baseline_units": 100.0, "ratio": 0.1}
    assert rows[1]["ratio"] == 1.0


def test_pos_gap_finds_stores_above_the_sap_price_only():
    rows = [sale("VIC01", price=12.0), sale("VIC02", price=10.0)]
    assert [g["store_id"] for g in outcome.pos_gaps(rows, [cond(price=10.0)])] == ["VIC01"]


def test_pos_gap_sees_a_store_that_sold_nothing_at_the_wrong_price():
    row = {**sale(price=12.0), "units": 0, "avg_price": None}
    assert len(outcome.pos_gaps([row], [cond(price=10.0)])) == 1


def test_a_shortage_means_the_stock_ran_out():
    second, third = ("2", 1, "VIC"), ("3", 1, "VIC")
    rows = outcome.stock_rows({KEY: 4, second: 100, third: 30}, {KEY: 4, second: 10, third: 10},
                              {KEY: 50.0, second: 50.0, third: 50.0})
    assert [r["sold_out"] for r in rows] == [True, False, False]       # third: low stock, but it was not what stopped sales


@pytest.mark.parametrize("gained,lost,explains", [(25, 100, True), (24, 100, False), (500, 100, True), (10, 0, False)])
def test_a_rise_must_explain_a_real_part_of_the_fall(gained, lost, explains):
    assert outcome.explains_loss(gained, lost) is explains


def test_rising_finds_what_shoppers_moved_to_in_that_region_only():
    actual = {("9", 1, "NSW"): 90, ("8", 1, "NSW"): 31, ("7", 1, "VIC"): 90, ("6", 1, "NSW"): 9}
    base = {("9", 1, "NSW"): 30.0, ("8", 1, "NSW"): 30.0, ("7", 1, "VIC"): 30.0, ("6", 1, "NSW"): 3.0}
    rows = outcome.rising(actual, base, {("9", 1): {"brand": "B", "category": "Red"}}, "NSW", 20)
    assert [(r["sku"], r["category"]) for r in rows] == [("9", "Red")]


def test_discount_stalls_when_the_price_did_not_deepen():
    hist = [{"week": w, "sku": "1", "pack_qty": 1, "region": "VIC", "shelf_price": 20.0, "price": p}
            for w, p in (("2026-W36", 18.0), ("2026-W37", 16.0), ("2026-W38", 16.0))]
    row = outcome.discount_rows(hist, "2026-W38")[0]
    assert row["stalled"] and row["discount_pct"] == 20.0
    assert not outcome.discount_rows(hist, "2026-W37")[0]["stalled"]


def test_pack_gap_is_only_about_what_an_override_changed():
    priced = [cond("1", 1, price=11.0), cond("1", 6, price=66.0)]
    assert not outcome.pack_gaps(priced)[0]["gap_widened"]
    promoted = [cond("1", 1, price=11.0, effective=8.25, source="PROMOTION"), cond("1", 6, price=66.0)]
    row = outcome.pack_gaps(promoted)[0]
    assert row["gap_widened"] and row["pack_premium_pct_sent"] == 0.0 and row["pack_premium_pct_effective"] > 30


# ======================= rules: confidence, route, owner =======================
def test_evidence_that_names_the_cause_is_confirmed():
    c = record_cases("W", [v("NOT_EFFECTIVE", "PROMOTION")], {"NOT_EFFECTIVE:PROMOTION": BOOK})[0]
    assert (c["confidence"], c["route"], c["owner"], c["source"]) == (CONFIRMED, BUSINESS, "Promotions team", "playbook")
    assert (c["proposed_reason"], c["proposed_action"], c["do_not"]) == ("because", "do it", "not this")


@pytest.mark.parametrize("code,reason,tag", [("PRICE_MISMATCH", "", "PRICE_MISMATCH"), ("MISSING_IN_SAP", "", "MISSING_IN_SAP"),
                                             ("MISSING_PRICE", "NOT_SENT", "MISSING_PRICE:NOT_SENT")])
def test_a_symptom_with_several_causes_stays_likely(code, reason, tag):
    assert record_cases("W", [v(code, reason)], {tag: BOOK})[0]["confidence"] == LIKELY


def test_a_record_without_evidence_is_not_confirmed():
    c = record_cases("W", [v("NOT_EFFECTIVE", "PROMOTION", detail="")], {"NOT_EFFECTIVE:PROMOTION": BOOK})[0]
    assert c["confidence"] == LIKELY


def test_no_playbook_means_no_guess():
    c = record_cases("W", [v("API_REJECTED", "CURRENCY_MISMATCH")], {})[0]
    assert (c["confidence"], c["route"], c["owner"]) == (UNCLASSIFIED, DEVELOPMENT, DEVELOPMENT_TEAM)
    assert c["proposed_reason"] == "" and c["proposed_action"] == ""


@pytest.mark.parametrize("owner,route", [("Data Platform team", DEVELOPMENT), ("Pricing Data Science team", DEVELOPMENT),
                                         ("SAP Pricing team", BUSINESS), ("Merchandising team", BUSINESS)])
def test_technical_owners_skip_the_business(owner, route):
    assert route_for(owner) == route


def test_records_of_one_cause_across_regions_are_one_case():
    cases = record_cases("W", [v("API_REJECTED", "VALIDITY_OVERLAP", s, r) for s in "12" for r in ("VIC", "NSW")],
                         {"API_REJECTED:VALIDITY_OVERLAP": BOOK})
    assert len(cases) == 1 and cases[0]["record_count"] == 4 and cases[0]["regions"] == ["NSW", "VIC"]


def test_a_multi_rule_reason_finds_its_playbook():
    cases = record_cases("W", [v("RULE_VIOLATION", "BELOW_FLOOR,LADDER_VIOLATION")], {"RULE_VIOLATION:LADDER_VIOLATION": BOOK})
    assert cases[0]["doc_id"] == "PB-X"


def test_outcomes_group_by_brand_and_region_and_start_with_no_owner_guess():
    flags = [{"brand": "B", "region": "VIC", "sku": s, "pack_qty": 1, "actual_units": 1, "baseline_units": 50.0, "ratio": 0.02,
              "lost_units": 49} for s in "12"] + [{"brand": "C", "region": "VIC", "sku": "3", "pack_qty": 1, "actual_units": 1,
                                                  "baseline_units": 90.0, "ratio": 0.01, "lost_units": 89}]
    cases = outcome_cases("2026-W39", "2026-W38", flags)
    assert [c["brand"] for c in cases] == ["B", "C"]                                  # 98 lost before 89
    assert all(c["confidence"] == UNCLASSIFIED and c["owner"] == DEVELOPMENT_TEAM for c in cases)
    assert cases[0]["evidence"] == {"outcome_week": "2026-W38", "lost_units": 98}


def test_blocking_comes_before_a_bigger_warning():
    cases = [{"severity": "warning", "impact_units": 500, "record_count": 50, "case_key": "a"},
             {"severity": "blocking", "impact_units": 0, "record_count": 1, "case_key": "b"}]
    assert rank(cases)[0]["case_key"] == "b"
