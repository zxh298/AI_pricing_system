"""    python -m jobs.weekly_pipeline --week 2026-W39 [--run-id RUN] [--batch-size 50] [--max-retries 4]

Exit code: 0 = SUCCEEDED or COMPLETED_WITH_ISSUES, 1 = BLOCKED or FAILED (so a scheduler can alert).
"""
import argparse
import json
import sys

from jobs.pricing_engine.sender import sap_write_client
from jobs.weekly_pipeline.pipeline import run_pipeline
from shared.config import load_config
from shared.warehouse import get_warehouse


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", default="2026-W39")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--batch-size", type=int, default=50)
    ap.add_argument("--max-retries", type=int, default=4)
    a = ap.parse_args()
    run_id = a.run_id or f"{a.week}-pipeline"
    cfg = load_config()
    with sap_write_client() as client:
        summary = run_pipeline(a.week, run_id, client, cfg.duckdb_path, get_warehouse(),
                               batch_size=a.batch_size, max_retries=a.max_retries)
    print(json.dumps({"run_id": run_id, **summary}, indent=2))
    return 1 if summary["status"] in ("BLOCKED", "FAILED") else 0


if __name__ == "__main__":
    sys.exit(main())
