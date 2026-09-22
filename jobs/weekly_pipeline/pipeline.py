"""Weekly pipeline: ingest -> price -> validate -> send -> reconcile -> history.

Statuses of a run:
    SUCCEEDED              every priced record accepted by SAP and confirmed effective
    COMPLETED_WITH_ISSUES  the run finished but some records were rejected, errored or not effective
    BLOCKED                pre-send validation found a rule violation; nothing was sent
    FAILED                 a step raised an exception (e.g. SAP unreachable during ingest)
Rerunning the same run_id resends only records SAP has not accepted yet (idempotent).
The history step appends the week's effective prices to the warehouse price_history.
"""
from __future__ import annotations

import time
from collections import Counter

import httpx

from jobs.pricing_engine.engine import price_week, record_send_results, save_recommendations
from jobs.pricing_engine.sender import send, to_payload
from jobs.sap_ingest import ingest
from jobs.weekly_pipeline.history import record_history
from jobs.weekly_pipeline.reconcile import reconcile
from jobs.weekly_pipeline.state import RunState
from jobs.weekly_pipeline.validate import validate_rows
from shared.warehouse import WarehouseClient


def run_pipeline(week: str, run_id: str, client: httpx.Client, duckdb_path: str, wh: WarehouseClient,
                 database_url: str | None = None, batch_size: int = 50, max_retries: int = 4,
                 sleep=time.sleep) -> dict:
    state = RunState(run_id, week, database_url)
    step = "ingest"
    try:
        # 1. ingest: SAP -> warehouse
        state.step("ingest", "RUNNING", finished=False)
        state.step("ingest", "OK", ingest(client, week, duckdb_path))

        # 2. price: engine -> price_recommendations
        step = "price"
        rows = price_week(week, wh)
        save_recommendations(duckdb_path, run_id, week, rows)
        priced = [r for r in rows if r["status"] == "PRICED"]
        state.step("price", "OK", {"rows": len(rows), "priced": len(priced),
                                   "skipped": dict(Counter(r["reason"] for r in rows if r["status"] == "SKIPPED"))})

        # 3. pre-send validation gate
        step = "validate"
        rules = {(r["sku"], r["pack_qty"]): r for r in wh.get_rules(week)}
        violations = validate_rows(rows, rules)
        if violations:
            state.step("validate", "BLOCKED", {"violations": violations[:50], "count": len(violations)})
            summary = {"status": "BLOCKED", "priced": len(priced), "violations": len(violations)}
            state.finish("BLOCKED", summary)
            return summary
        state.step("validate", "OK", {"checked": len(priced)})

        # 4. send: only records SAP has not accepted yet
        step = "send"
        state.init_records(priced)
        done = state.accepted_keys()
        todo = [r for r in priced if (r["sku"], r["pack_qty"], r["region"]) not in done]
        results = send(client, week, run_id, to_payload(todo, week), batch_size, max_retries, sleep=sleep)
        state.save_send_results(results)
        record_send_results(duckdb_path, run_id, week, results)
        counts = Counter(r["status"] for r in results)
        state.step("send", "OK", {"sent": len(todo), "already_accepted": len(done), **counts,
                                  "codes": dict(Counter(r["code"] for r in results if r["status"] != "ACCEPTED"))})

        # 5. reconcile: does SAP hold (and apply) what we sent?
        step = "reconcile"
        accepted = {(r["sku"], r["pack_qty"], r["region"]) for r in state.records() if r["send_status"] == "ACCEPTED"}
        r = client.get("/pricing/conditions", params={"week": week})
        r.raise_for_status()
        conditions = r.json()["items"]
        recon = reconcile([p for p in priced if (p["sku"], p["pack_qty"], p["region"]) in accepted], conditions)
        state.save_reconcile(recon)
        rc = Counter(x["reconcile_status"] for x in recon)
        state.step("reconcile", "OK", dict(rc))

        # 6. history: record the week's actual (effective) prices in the warehouse
        step = "history"
        state.step("history", "OK", record_history(duckdb_path, week, conditions))
    except Exception as e:                                            # noqa: BLE001 - record then re-raise
        state.step(step, "FAILED", {"error": repr(e)})
        state.finish("FAILED", error=f"{step}: {e!r}")
        raise

    final = state.records()
    summary = {
        "priced": len(priced), "skipped": len(rows) - len(priced),
        "accepted": sum(x["send_status"] == "ACCEPTED" for x in final),
        "rejected": sum(x["send_status"] == "REJECTED" for x in final),
        "errors": sum(x["send_status"] == "ERROR" for x in final),
        "confirmed": rc.get("CONFIRMED", 0), "not_effective": rc.get("NOT_EFFECTIVE", 0),
        "missing_in_sap": rc.get("MISSING_IN_SAP", 0), "price_mismatch": rc.get("PRICE_MISMATCH", 0)}
    ok = summary["confirmed"] == summary["priced"]
    summary["status"] = "SUCCEEDED" if ok else "COMPLETED_WITH_ISSUES"
    state.finish(summary["status"], summary)
    return summary
