"""Who handles a problem and how sure we are. Plain rules: the LLM has no say in confidence, route or owner.

    CONFIRMED     the record's own evidence names the cause (SAP's answer, the engine's skip reason, a rule that failed)
    LIKELY        a playbook describes the symptom, but nothing here proves it is this week's cause
    UNCLASSIFIED  no playbook. Never guessed at: it goes to the development team with its evidence
"""
from __future__ import annotations

from collections import Counter, defaultdict

from services.diagnostic_api.knowledge import tags_for

CONFIRMED, LIKELY, UNCLASSIFIED = "CONFIRMED", "LIKELY", "UNCLASSIFIED"
BUSINESS, DEVELOPMENT = "business", "development"
DEVELOPMENT_TEAM = "Development team"
TECHNICAL_TEAMS = {"Data Platform team", "Pricing Data Science team", DEVELOPMENT_TEAM}
LOW_SELL_THROUGH = "LOW_SELL_THROUGH"

# A record alone cannot prove these: the symptom has more than one cause, or the cause is outside what we can see.
LIKELY_ONLY = {"PRICE_MISMATCH", "MISSING_IN_SAP", "MISSING_PRICE:NOT_SENT", "MISSING_PRICE:NO_RECOMMENDATION"}

SEVERITY_RANK = {"blocking": 0, "warning": 1}
# The cause an investigation may name, and who a confirmed one belongs to.
CAUSE_OWNERS = {"POS_PRICE_GAP": "SAP Pricing team", "STOCK_SHORTAGE": "Merchandising team",
                "DISCOUNT_STALLED": "Pricing Data Science team", "CANNIBALIZATION": "Promotions team",
                "SUBSTITUTION": "Promotions team"}
# What each owner is asked, if the model's own proposal is unusable. A live run submitted the action "x".
CAUSE_ACTIONS = {"POS_PRICE_GAP": "Check why the affected stores still charge the old price, and restore the price feed to them.",
                 "STOCK_SHORTAGE": "Check the replenishment for these items, and decide whether the clearance should continue.",
                 "DISCOUNT_STALLED": "Review why the markdown stopped deepening, and whether these items need a different plan.",
                 "CANNIBALIZATION": "Review the promotion on the other pack size against this clearance, and decide which should run.",
                 "SUBSTITUTION": "Review the competing promotion against this clearance, and decide whether both should run."}
MIN_ACTION_CHARS = 20


def route_for(owner: str) -> str:
    return DEVELOPMENT if owner in TECHNICAL_TEAMS else BUSINESS


def tag_of(code: str, reason: str) -> list[str]:
    return tags_for(code, reason)[0]


def _confidence(code: str, reason: str, records: list[dict]) -> str:
    tags = tag_of(code, reason)
    if any(t in LIKELY_ONLY for t in tags) or not all(r["detail"] for r in records):
        return LIKELY
    return CONFIRMED


def _evidence(records: list[dict]) -> dict:
    return {"by_region": dict(Counter(r["region"] for r in records)),
            "details": [{"detail": d, "records": n} for d, n in Counter(r["detail"] for r in records).most_common(5)]}


def record_cases(week: str, verdicts: list[dict], playbooks: dict[str, dict]) -> list[dict]:
    """One case per (code, reason, owner). `verdicts` are the failing records; `playbooks` is by tag."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for v in verdicts:
        groups[(v["code"], v["reason"])].append(v)
    cases = []
    for (code, reason), records in groups.items():
        book = next((playbooks[t] for t in tag_of(code, reason) if t in playbooks), None)
        owner = book["owner"] if book and book["owner"] else DEVELOPMENT_TEAM
        case = {"case_key": f"{week}|{code}|{reason}|{owner}", "week": week, "kind": "record", "error_code": code,
                "reason": reason, "brand": None, "regions": sorted({r["region"] for r in records}),
                "records": [{k: r[k] for k in ("sku", "pack_qty", "region", "detail")} for r in records],
                "record_count": len(records), "impact_units": 0, "evidence": _evidence(records), "owner": owner}
        if book:
            case |= {"confidence": _confidence(code, reason, records), "route": route_for(owner), "source": "playbook",
                     "severity": book["severity"], "doc_id": book["doc_id"], "proposed_reason": book["why"],
                     "proposed_action": book["what_to_do"], "do_not": book["what_not_to_do"]}
        else:
            case |= {"confidence": UNCLASSIFIED, "route": DEVELOPMENT, "source": "none", "severity": "blocking",
                     "doc_id": None, "proposed_reason": "", "proposed_action": "", "do_not": ""}
        cases.append(case)
    return rank(cases)


def outcome_cases(week: str, outcome_week: str, flagged: list[dict]) -> list[dict]:
    """One case per (brand, region): the agent finds out whether its records share a cause."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for f in flagged:
        groups[(f["brand"], f["region"])].append(f)
    cases = []
    for (brand, region), records in groups.items():
        lost = sum(r["lost_units"] for r in records)
        cases.append({
            "case_key": f"{week}|{LOW_SELL_THROUGH}|{brand}|{region}", "week": week, "kind": "outcome",
            "error_code": LOW_SELL_THROUGH, "reason": "", "brand": brand, "regions": [region],
            "records": [{**r, "detail": f"{r['actual_units']} units against a baseline of {r['baseline_units']}"}
                        for r in records], "record_count": len(records), "impact_units": lost,
            "evidence": {"outcome_week": outcome_week, "lost_units": lost}, "owner": DEVELOPMENT_TEAM,
            "confidence": UNCLASSIFIED, "route": DEVELOPMENT, "source": "none", "severity": "warning", "doc_id": None,
            "proposed_reason": "", "proposed_action": "", "do_not": ""})
    return rank(cases)


def incident_case(week: str, code: str, reason: str, detail: str) -> dict:
    """A problem with the run itself: there are no records to group."""
    return {"case_key": f"{week}|{code}|{reason}", "week": week, "kind": "incident", "error_code": code, "reason": reason,
            "brand": None, "regions": [], "records": [], "record_count": 0, "impact_units": 0,
            "evidence": {"detail": detail}, "owner": "Data Platform team", "confidence": CONFIRMED, "route": DEVELOPMENT,
            "source": "none", "severity": "blocking", "doc_id": None, "proposed_reason": detail,
            "proposed_action": "Find out why, fix it, and rerun the week with the same run id.", "do_not": ""}


def rank(cases: list[dict]) -> list[dict]:
    """Most urgent first: blocking before warning, then the larger impact and the more records."""
    return sorted(cases, key=lambda c: (SEVERITY_RANK.get(c["severity"], 2), -c["impact_units"], -c["record_count"],
                                        c["case_key"]))
