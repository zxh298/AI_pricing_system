"""Run state in Postgres: pipeline_runs, pipeline_steps, pipeline_records."""
from __future__ import annotations

import json

from shared import db
from shared.pipeline_schema import ddl, schema


class RunState:
    def __init__(self, run_id: str, week: str, database_url: str | None = None):
        self.run_id, self.week, self.url, self.s = run_id, week, database_url, schema()
        with db.connect(self.url) as con:
            con.execute(ddl())
            con.execute(f"""INSERT INTO {self.s}.pipeline_runs (run_id, week, status) VALUES (%s,%s,'RUNNING')
                ON CONFLICT (run_id) DO UPDATE SET status = 'RUNNING', attempt = {self.s}.pipeline_runs.attempt + 1,
                    started_at = now(), finished_at = NULL, error = NULL, summary = NULL""", (run_id, week))
            con.execute(f"DELETE FROM {self.s}.pipeline_steps WHERE run_id = %s", (run_id,))

    def step(self, name: str, status: str, detail: dict | None = None, finished: bool = True) -> None:
        with db.connect(self.url) as con:
            con.execute(f"""INSERT INTO {self.s}.pipeline_steps (run_id, step, status, detail, finished_at)
                VALUES (%s,%s,%s,%s, CASE WHEN %s THEN now() END)
                ON CONFLICT (run_id, step) DO UPDATE SET status = EXCLUDED.status, detail = EXCLUDED.detail,
                    finished_at = EXCLUDED.finished_at""",
                        (self.run_id, name, status, json.dumps(detail or {}, default=str), finished))

    def finish(self, status: str, summary: dict | None = None, error: str | None = None) -> None:
        with db.connect(self.url) as con:
            con.execute(f"""UPDATE {self.s}.pipeline_runs SET status = %s, finished_at = now(), error = %s, summary = %s
                            WHERE run_id = %s""", (status, error, json.dumps(summary or {}, default=str), self.run_id))

    # ---- per-record send / reconcile status ----
    def accepted_keys(self) -> set[tuple]:
        """Records SAP already accepted for this run_id (a rerun resends only the others)."""
        with db.connect(self.url) as con:
            rows = con.execute(f"""SELECT sku, pack_qty, region FROM {self.s}.pipeline_records
                                   WHERE run_id = %s AND send_status = 'ACCEPTED'""", (self.run_id,)).fetchall()
        return {(r["sku"], r["pack_qty"], r["region"]) for r in rows}

    def init_records(self, priced: list[dict]) -> None:
        with db.connect(self.url) as con, con.cursor() as cur:
            cur.executemany(f"""INSERT INTO {self.s}.pipeline_records (run_id, week, sku, pack_qty, region, recommended_price)
                VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (run_id, sku, pack_qty, region)
                DO UPDATE SET recommended_price = EXCLUDED.recommended_price""",
                            [(self.run_id, self.week, r["sku"], r["pack_qty"], r["region"], r["recommended_price"])
                             for r in priced])

    def save_send_results(self, results: list[dict]) -> None:
        with db.connect(self.url) as con, con.cursor() as cur:
            cur.executemany(f"""UPDATE {self.s}.pipeline_records SET send_status=%s, send_code=%s, send_message=%s, attempts=%s
                WHERE run_id=%s AND sku=%s AND pack_qty=%s AND region=%s""",
                            [(r["status"], r["code"], r["message"], r.get("attempts"), self.run_id,
                              r["sku"], int(r["pack_qty"]), r["region"]) for r in results])

    def save_reconcile(self, results: list[dict]) -> None:
        with db.connect(self.url) as con, con.cursor() as cur:
            cur.executemany(f"""UPDATE {self.s}.pipeline_records SET reconcile_status=%s, reconcile_detail=%s
                WHERE run_id=%s AND sku=%s AND pack_qty=%s AND region=%s""",
                            [(r["reconcile_status"], r["reconcile_detail"], self.run_id,
                              r["sku"], int(r["pack_qty"]), r["region"]) for r in results])

    def records(self) -> list[dict]:
        with db.connect(self.url) as con:
            return con.execute(f"SELECT * FROM {self.s}.pipeline_records WHERE run_id = %s ORDER BY sku, pack_qty, region",
                               (self.run_id,)).fetchall()
