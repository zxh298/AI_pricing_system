"""Deterministic diagnosis: one verdict per (sku, pack_qty, region). No LLM, no I/O.

Every judgement the assistant reports comes from here. Verdict codes, in order of precedence:

    NOT_IN_LIST     the sku is not on this week's SAP clearance list for that region
    MISSING_PRICE   listed, but no price was produced or sent (engine skip reason, or never sent)
    RULE_VIOLATION  the recommended price breaks a hard rule (floor, max markdown, ladder, ...)
    API_REJECTED    SAP rejected the record or the request failed
    NOT_EFFECTIVE   SAP accepted our price but something overrides it (e.g. a promotion)
    PRICE_MISMATCH  SAP holds a different price than the one sent
    MISSING_IN_SAP  SAP accepted the record but holds no condition for it
    OK              SAP holds our price and it is the effective price

`reason` is a short, stable key used to collapse records into patterns; `detail` is the specific text.
Reuses the pipeline's pure validation and reconciliation functions, so the pipeline and the
diagnostic assistant can never disagree about what counts as a violation.
"""
from __future__ import annotations

from jobs.weekly_pipeline.reconcile import reconcile
from jobs.weekly_pipeline.validate import validate_rows

Key = tuple  # (sku, pack_qty, region)


def _verdict(sku, pack_qty, region, code, reason="", detail=""):
    return {"sku": sku, "pack_qty": pack_qty, "region": region, "code": code, "reason": reason, "detail": detail}


def diagnose(skus: list[str], regions: list[str], cands: list[dict], recs: list[dict],
             rules: dict[tuple, dict] | list[dict], conditions: list[dict]) -> list[dict]:
    """cands: SAP candidate list; recs: engine output incl. sap_status/sap_code/sap_message;
    rules: business rules (list or {(sku, pack_qty): rule}); conditions: live GET /pricing/conditions."""
    if isinstance(rules, list):
        rules = {(r["sku"], r["pack_qty"]): r for r in rules}
    listed: dict[tuple, list[dict]] = {}
    for c in cands:
        listed.setdefault((c["sku"], c["region"]), []).append(c)
    rec_by: dict[Key, dict] = {(r["sku"], r["pack_qty"], r["region"]): r for r in recs}
    held: dict[Key, dict] = {(c["sku"], c["pack_qty"], c["region"]): c for c in conditions}

    violations: dict[Key, list[str]] = {}          # whole week, so a multipack's ladder sees its Single
    for v in validate_rows(recs, rules):
        violations.setdefault((v["sku"], v["pack_qty"], v["region"]), []).append(v["code"])

    accepted = [r for r in recs if r["status"] == "PRICED" and r["sap_status"] == "ACCEPTED"]
    recon = {(x["sku"], x["pack_qty"], x["region"]): x for x in reconcile(accepted, list(held.values()))}

    out: list[dict] = []
    for sku in skus:
        for region in regions:
            rows = listed.get((sku, region))
            if not rows:
                out.append(_verdict(sku, None, region, "NOT_IN_LIST", "NOT_ON_LIST",
                                    "not on this week's clearance list for this region"))
                continue
            for c in sorted(rows, key=lambda c: c["pack_qty"]):
                key = (sku, c["pack_qty"], region)
                rec = rec_by.get(key)
                if rec is None:
                    v = _verdict(*key, "MISSING_PRICE", "NO_RECOMMENDATION", "the engine produced no row for this item")
                elif rec["status"] != "PRICED":
                    v = _verdict(*key, "MISSING_PRICE", rec["reason"], f"engine skipped the item: {rec['reason']}")
                elif key in violations:
                    codes = sorted(set(violations[key]))
                    v = _verdict(*key, "RULE_VIOLATION", ",".join(codes), f"recommended price breaks: {', '.join(codes)}")
                elif rec["sap_status"] in ("REJECTED", "ERROR"):
                    v = _verdict(*key, "API_REJECTED", rec["sap_code"], rec["sap_message"] or "")
                elif rec["sap_status"] != "ACCEPTED":
                    v = _verdict(*key, "MISSING_PRICE", "NOT_SENT", "priced but never sent to SAP")
                else:
                    status = recon[key]["reconcile_status"]
                    if status == "CONFIRMED":
                        v = _verdict(*key, "OK")
                    elif status == "NOT_EFFECTIVE":
                        v = _verdict(*key, "NOT_EFFECTIVE", held[key]["effective_source"], recon[key]["reconcile_detail"])
                    else:                                       # PRICE_MISMATCH / MISSING_IN_SAP
                        v = _verdict(*key, status, "", recon[key]["reconcile_detail"])
                out.append(v)
    return out
