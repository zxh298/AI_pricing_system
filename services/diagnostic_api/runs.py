"""Which pipeline run is the current one for a week.

Reads the weekly pipeline's run table in Postgres (read-only). The newest run that finished with prices to
look at is the current one: a run that is re-run finishes again and becomes the newest, which run ids
alone (alphabetical, or by creation) cannot tell. `data_version` names that run and its finish time; it
changes whenever a new run finishes, which is what invalidates cached tool results (part 3c).
"""
from __future__ import annotations

import psycopg.errors

from shared import db
from shared.pipeline_schema import schema

# SUCCEEDED / COMPLETED_WITH_ISSUES sent prices; BLOCKED produced recommendations but sent nothing (still
# worth diagnosing). FAILED runs and runs still in progress are not a stable picture of the week.
FINISHED = ("SUCCEEDED", "COMPLETED_WITH_ISSUES", "BLOCKED")


class RunResolver:
    def __init__(self, database_url: str | None = None):
        self.url = database_url

    def latest(self, week: str) -> dict | None:
        """{"run_id", "status", "finished_at", "data_version"} of the newest finished run, or None."""
        try:
            with db.connect(self.url) as con:
                row = con.execute(f"""SELECT run_id, status, finished_at FROM {schema()}.pipeline_runs
                    WHERE week = %s AND status = ANY(%s) AND finished_at IS NOT NULL
                    ORDER BY finished_at DESC, run_id DESC LIMIT 1""", (week, list(FINISHED))).fetchone()
        except psycopg.errors.UndefinedTable:                       # the pipeline has never run
            return None
        if row is None:
            return None
        return {**row, "data_version": f"{row['run_id']}@{row['finished_at'].isoformat()}"}
