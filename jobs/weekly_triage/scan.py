"""The weekly scan. Human-written checks find the problems; the LLM only proposes explanations for the ones rules cannot.

    run the checks -> group into cases -> sync with what is already known -> LLM steps within budget -> notify owners

Every record is checked directly, so the 500-sku cap of the chat tools does not apply. Failures are isolated: an LLM
error leaves the case with the development team, and the scan still finishes.

Circuit breaker: when a large share of records fail together, they almost always share one cause upstream. Spending a
model call per case would be useless, so no LLM runs, no owner is paged and no deadline starts; Data Platform gets one
incident message. After the rerun, a normal scan releases whatever is left.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from services.diagnostic_api import outcome
from services.diagnostic_api.core import diagnose
from services.diagnostic_api.findings import FindingsStore
from services.diagnostic_api.investigate import investigate, summarize
from services.diagnostic_api.knowledge import KnowledgeMissing
from services.diagnostic_api.llm import LLM
from services.diagnostic_api.tools import ToolContext, ToolError, _sap_get
from services.diagnostic_api.triage_rules import (BUSINESS, CAUSE_ACTIONS, CAUSE_OWNERS, DEVELOPMENT, DEVELOPMENT_TEAM, LIKELY,
                                                  MIN_ACTION_CHARS, UNCLASSIFIED, incident_case, outcome_cases, record_cases,
                                                  route_for, tag_of)
from services.diagnostic_api.triage_store import CaseStore
from shared.config import Config, load_config

log = logging.getLogger("weekly.triage")

NOTIFIED = ("new", "changed", "reopened")


@dataclass
class Scan:
    ctx: ToolContext
    store: CaseStore
    findings: FindingsStore
    llm: LLM | None
    cfg: Config


def _record_verdicts(scan: Scan, week: str, run_id: str, cands: list[dict]) -> tuple[list[dict], int]:
    """(failing records, records checked). `NOT_IN_LIST` answers a question about one sku in one region; here it only
    says that sku is listed elsewhere."""
    wh = scan.ctx.wh
    conditions = _sap_get(scan.ctx, "/pricing/conditions", week=week)["items"]
    skus, regions = sorted({c["sku"] for c in cands}), sorted({c["region"] for c in cands})
    res = [v for v in diagnose(skus, regions, cands, wh.get_recommendations(week, run_id), wh.get_rules(week), conditions)
           if v["code"] != "NOT_IN_LIST"]
    return [v for v in res if v["code"] != "OK"], len(res)


def _playbooks(scan: Scan, failing: list[dict]) -> tuple[dict, bool]:
    tags = sorted({t for v in failing for t in tag_of(v["code"], v["reason"])})
    try:
        return scan.ctx.kb.playbook_sections(tags), True
    except KnowledgeMissing:
        log.warning("knowledge base not built: every case is unclassified")
    except Exception:                                                  # noqa: BLE001 - never lose the scan to this
        log.warning("could not read playbooks", exc_info=True)
    return {}, False


def _outcome_flags(scan: Scan, week: str) -> tuple[list[dict], int, str, bool]:
    """(flagged records, records checked, the week looked at, whether its sales exist)."""
    wh, cfg, sold = scan.ctx.wh, scan.cfg, outcome.shift_week(week, -1)
    rows = wh.get_store_sales(*outcome.week_dates(sold))
    if not rows:
        return [], 0, sold, False
    earlier = [outcome.units_by_record(wh.get_store_sales(*outcome.week_dates(outcome.shift_week(sold, -i))))
               for i in range(1, outcome.BASELINE_WEEKS + 1)]
    conditions = _sap_get(scan.ctx, "/pricing/conditions", week=sold)["items"]
    records = outcome.clean_records(wh.get_candidates(sold), conditions)
    flagged, checked = outcome.low_sell_through(records, outcome.units_by_record(rows), outcome.baselines(earlier),
                                                cfg.triage_drop_ratio, cfg.triage_min_baseline)
    return flagged, checked, sold, True


def _known(week: str, cases: list[dict], findings: FindingsStore) -> dict[str, list[int]]:
    """Cases an open finding already covers completely: known issues stay quiet."""
    rows = findings.list(week=week, status="open")
    out = {}
    for case in cases:
        ids = []
        for region in case["regions"]:
            wanted = {r["sku"] for r in case["records"] if r["region"] == region}
            ids += [f["finding_id"] for f in rows if f["region"] == region and f["error_code"] == case["error_code"]
                    and f["reason"] == case["reason"] and wanted <= set(f["skus"])]
        covered = {f["region"] for f in rows if f["finding_id"] in ids}
        if ids and covered == set(case["regions"]):
            out[case["case_key"]] = ids
    return out


def _explain_unclassified(scan: Scan, case: dict) -> dict:
    similar = []
    try:
        text = " ".join(str(d["detail"]) for d in case["evidence"]["details"]) or case["error_code"]
        similar = scan.ctx.kb.similar(f"{case['error_code']} {case['reason']} {text}")
    except Exception:                                                  # noqa: BLE001 - optional context
        log.warning("similarity search failed for case %s", case["case_id"], exc_info=True)
    result = summarize(case, similar, scan.llm)
    return scan.store.save_proposal(case["case_id"], result, source="llm_summary" if result["status"] == "summarized" else "none",
                                    proposed_reason=result.get("summary", ""),
                                    proposed_action="\n".join(result.get("next_checks", [])))


def _action(cause: dict) -> str:
    text = cause["proposed_action"].strip()
    return text if len(text) >= MIN_ACTION_CHARS else CAUSE_ACTIONS[cause["cause"]]


def _proposal_from(result: dict) -> dict:
    """What an investigation becomes. Code picks owner, route and confidence from the causes code confirmed."""
    causes = result["causes"]
    if not causes:
        return {"confidence": UNCLASSIFIED, "route": DEVELOPMENT, "owner": DEVELOPMENT_TEAM, "source": "agent",
                "reason": "", "proposed_reason": "", "proposed_action": ""}
    first = causes[0]
    owner = CAUSE_OWNERS[first["cause"]]
    return {"confidence": LIKELY, "route": route_for(owner), "owner": owner, "source": "agent", "reason": first["cause"],
            "proposed_reason": " ".join(f"{c['explanation']} (skus: {', '.join(c['skus'])})" for c in causes),
            "proposed_action": "\n".join(f"{CAUSE_OWNERS[c['cause']]}: {_action(c)}" for c in causes)}


def _investigate_case(scan: Scan, case: dict) -> dict:
    result = investigate(case, scan.ctx, scan.llm, scan.cfg.triage_max_steps)
    return scan.store.save_proposal(case["case_id"], result, **_proposal_from(result))


def _llm_steps(scan: Scan, cases: list[dict], breaker: bool) -> tuple[int, list[dict]]:
    """Unclassified errors and outcome cases, largest first, while the budget lasts. Returns (calls used, cases as saved)."""
    pending = [c for c in cases if c["decision"] != "known_finding"
               and (c["kind"] == "outcome" or (c["kind"] == "record" and c["confidence"] == UNCLASSIFIED))]
    pending = sorted(pending, key=lambda c: (-c["impact_units"], -c["record_count"]))
    used, out = 0, {c["case_id"]: c for c in cases}
    for case in pending:
        if breaker or scan.llm is None or used >= scan.cfg.triage_max_investigations:
            status = "held_by_breaker" if breaker else "no_llm" if scan.llm is None else "over_budget"
            out[case["case_id"]] = scan.store.save_proposal(case["case_id"], {"status": status})
            continue
        used += 1
        try:
            run = _investigate_case if case["kind"] == "outcome" else _explain_unclassified
            out[case["case_id"]] = run(scan, case)
        except Exception as e:                                         # noqa: BLE001 - the case keeps its evidence
            log.exception("LLM step failed for case %s", case["case_id"])
            out[case["case_id"]] = scan.store.save_proposal(case["case_id"], {"status": "failed", "error": repr(e)})
    return used, list(out.values())


def _line(c: dict) -> str:
    head = f"#{c['case_id']} [{c['confidence']}] {c['error_code']}" + (f":{c['reason']}" if c["reason"] else "")
    scope = f"{c['record_count']} records in {'/'.join(c['regions'])}" + (f", {c['brand']}" if c["brand"] else "")
    lost = f", about {c['impact_units']} units below baseline" if c["impact_units"] else ""
    return f"{head}: {scope}{lost}"


def _notify_owners(scan: Scan, week: str, run_id: str | None, cases: list[dict]) -> int:
    """One digest per team per scan. Written by code; nothing an LLM wrote decides who gets it."""
    teams: dict[str, list[dict]] = {}
    for c in cases:
        teams.setdefault(c["owner"], []).append(c)
    for team, own in teams.items():
        subject = f"{week}: {len(own)} pricing issue(s) need your decision" if own[0]["route"] == BUSINESS else \
            f"{week}: {len(own)} pricing issue(s) for investigation"
        scan.store.notify(week, run_id, team, "digest", subject, "\n".join(_line(c) for c in own), [c["case_id"] for c in own])
    return len(teams)


def _incident(scan: Scan, week: str, run_id: str | None, version: str | None, code: str, reason: str, detail: str) -> dict:
    synced = scan.store.sync(week, run_id, [incident_case(week, code, reason, detail)], {"incident"})
    fresh = synced["new"] + synced["reopened"]
    if fresh:
        scan.store.notify(week, run_id, "Data Platform team", "incident", f"{week}: triage could not scan the week", detail,
                          [c["case_id"] for c in fresh])
    summary = {"incident": code, "detail": detail}
    scan.store.log_scan(week, run_id, version, None, "INCIDENT", summary)
    return {"status": "INCIDENT", **summary}


def run_triage(week: str, scan: Scan) -> dict:
    ctx = scan.ctx
    latest, newest = ctx.runs.latest(week), ctx.runs.newest_status(week)
    if latest is None:
        return _incident(scan, week, None, None, "NO_USABLE_RUN", "NO_FINISHED_RUN",
                         f"No finished pipeline run for {week}, so there is nothing to scan.")
    if newest == "FAILED":
        return _incident(scan, week, latest["run_id"], latest["data_version"], "NO_USABLE_RUN", "LAST_RUN_FAILED",
                         f"The newest pipeline run for {week} failed, so the last good run no longer describes the week.")
    run_id, version = latest["run_id"], latest["data_version"]
    cands = ctx.wh.get_candidates(week)
    try:
        failing, checked = _record_verdicts(scan, week, run_id, cands)
        flagged, outcome_checked, sold, sales_ok = _outcome_flags(scan, week)
    except ToolError as e:
        return _incident(scan, week, run_id, version, "SCAN_FAILED", "SAP_UNAVAILABLE", str(e))

    playbooks, kb_ok = _playbooks(scan, failing)
    found = record_cases(week, failing, playbooks) + outcome_cases(week, sold, flagged)

    share = len(failing) / checked if checked else 0
    outcome_share = len(flagged) / outcome_checked if outcome_checked else 0
    breaker = share >= scan.cfg.triage_breaker_share or outcome_share >= scan.cfg.triage_breaker_share

    kinds = {"record", "incident"} | ({"outcome"} if sales_ok else set())
    synced = scan.store.sync(week, run_id, found, kinds, hold=breaker, known=_known(week, found, scan.findings))
    closed = scan.store.resolve_earlier_outcomes(week, {(f["brand"], f["region"]) for f in flagged}) if sales_ok else []

    # auto_resolve needs the whole scope, and this scan is the whole scope
    closed_findings = scan.findings.auto_resolve(
        week, sorted({c["region"] for c in cands}), sorted({c["sku"] for c in cands}),
        {f"{v['sku']}|{v['pack_qty']}|{v['region']}": f"{v['code']}:{v['reason']}" for v in failing})

    fresh = [c for k in ("new", "reopened") for c in synced[k]]
    used, saved = _llm_steps(scan, fresh, breaker)
    by_id = {c["case_id"]: c for c in saved}
    to_notify = [by_id.get(c["case_id"], c) for k in NOTIFIED for c in synced[k] if c["decision"] != "known_finding"]

    if breaker:
        detail = (f"{len(failing)} of {checked} records failed and {len(flagged)} of {outcome_checked} clean records sold far "
                  "below baseline. Treated as one upstream incident: owners were not paged and no LLM ran.")
        if to_notify:
            scan.store.notify(week, run_id, "Data Platform team", "incident", f"{week}: mass failure, one incident", detail,
                              [c["case_id"] for c in to_notify])
        notified = 1 if to_notify else 0
    else:
        notified = _notify_owners(scan, week, run_id, to_notify)

    summary = {"records_checked": checked, "records_failing": len(failing), "outcome_records_checked": outcome_checked,
               "outcome_flagged": len(flagged), "sales_available": sales_ok, "knowledge_ok": kb_ok, "breaker": breaker,
               "llm_steps": used, "teams_notified": notified, "findings_closed": closed_findings,
               **{k: len(v) for k, v in synced.items()}, "earlier_outcomes_resolved": len(closed)}
    status = "BREAKER" if breaker else "OK"
    scan.store.log_scan(week, run_id, version, sold, status, summary)
    return {"status": status, "run_id": run_id, "outcome_week": sold, **summary}


def run_sweep(store: CaseStore) -> dict:
    """Escalate whatever is past its deadline."""
    late = store.sweep()
    teams: dict[str, list[dict]] = {}
    for c in late:
        teams.setdefault(c["owner"], []).append(c)
    for team, own in teams.items():
        store.notify(own[0]["week"], own[0]["last_run_id"], team, "escalation", f"{len(own)} case(s) passed their deadline",
                     "\n".join(_line(c) for c in own), [c["case_id"] for c in own])
    return {"status": "SWEPT", "escalated": len(late)}


def build_scan(ctx: ToolContext, llm: LLM | None, cfg: Config | None = None) -> Scan:
    cfg = cfg or load_config()
    store = CaseStore(response_hours=cfg.triage_response_hours, decision_owner=cfg.triage_decision_owner)
    return Scan(ctx, store, FindingsStore(), llm, cfg)
