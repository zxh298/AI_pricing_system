"""    python -m jobs.weekly_triage --week 2026-W39     scan the week (run after the weekly pipeline)
    python -m jobs.weekly_triage --sweep              escalate cases past their deadline

Runs with the SAP read key and a read-only warehouse: it can look at everything and change no price. Exit code 0 when
the scan finished, whatever it found; 1 when it could not scan (so a scheduler can alert).
"""
import argparse
import json
import logging
import sys

from jobs.weekly_triage.scan import build_scan, run_sweep, run_triage
from services.diagnostic_api.llm import get_triage_llm
from services.diagnostic_api.tools import make_context
from services.diagnostic_api.triage_store import CaseStore
from shared.config import load_config
from shared.envfile import load_dotenv


def main() -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    cfg = load_config()
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", default=cfg.default_week)
    ap.add_argument("--sweep", action="store_true")
    a = ap.parse_args()
    store = CaseStore(response_hours=cfg.triage_response_hours, decision_owner=cfg.triage_decision_owner)
    store.ensure_schema()
    if a.sweep:
        print(json.dumps(run_sweep(store), indent=2))
        return 0
    ctx = make_context("weekly-triage")
    try:
        result = run_triage(a.week, build_scan(ctx, get_triage_llm(), cfg))
    finally:
        ctx.sap.close()
    print(json.dumps(result, indent=2, default=str))
    return 1 if result["status"] == "INCIDENT" else 0


if __name__ == "__main__":
    sys.exit(main())
