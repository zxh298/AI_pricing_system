#!/usr/bin/env bash
# One week: price, send and reconcile, then scan. The scan runs even if the pipeline is BLOCKED or FAILED: that is
# exactly when someone needs to hear about it. Exits with the pipeline's status, so a scheduler can alert on it.
#
#   infra/run_week.sh 2026-W39 [run-id]
# Cloud Scheduler plays this role on GCP, triggering the two Cloud Run jobs in the same order.
set -u
week="${1:?usage: run_week.sh WEEK [RUN_ID]}"
run_id="${2:-${week}-pipeline}"

docker compose run --rm weekly-pipeline --week "$week" --run-id "$run_id"
pipeline_status=$?
docker compose run --rm weekly-triage --week "$week" || echo "weekly-triage could not scan $week" >&2
exit "$pipeline_status"
