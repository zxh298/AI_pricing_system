"""Triage cases in Postgres: what a scan found, what an owner decided, and the full history of both.

A case closes in exactly one way: a later scan no longer finds its records. An owner's Accept records a decision, not
a fix. Only people decide (accept, waive, reject); the scan, the agent and the deadline sweep cannot. Accepting
writes findings under the person's name, so the rule that people write findings holds here too.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Callable

from psycopg.types.json import Jsonb

from services.diagnostic_api.findings import FindingsStore
from services.diagnostic_api.triage_rules import BUSINESS, DEVELOPMENT, DEVELOPMENT_TEAM, route_for
from shared import db
from shared.diagnostic_schema import ddl, schema

ACTIVE = ("open", "accepted", "escalated", "waived")
JSON_FIELDS = ("regions", "records", "evidence")
PROPOSAL_FIELDS = ("confidence", "route", "owner", "source", "severity", "doc_id", "proposed_reason", "proposed_action",
                   "do_not")
FINDING_CAUSE_LIMIT = 1000


class CaseConflict(Exception):
    """The case is not in a state that allows this decision."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _key(r: dict) -> str:
    return f"{r['sku']}|{r['pack_qty']}|{r['region']}"


class CaseStore:
    def __init__(self, database_url: str | None = None, clock: Callable[[], datetime] = _utcnow,
                 response_hours: int = 24, decision_owner: str = "Clearance Business Lead"):
        self.url, self.s, self.clock = database_url, schema(), clock
        self.response_hours, self.decision_owner = response_hours, decision_owner

    def ensure_schema(self) -> None:
        with db.connect(self.url) as con:
            con.execute(ddl())

    def _deadline(self) -> datetime:
        return self.clock() + timedelta(hours=self.response_hours)

    def _event(self, con, case_id: int, actor: str, event: str, detail: dict | None = None) -> None:
        con.execute(f"INSERT INTO {self.s}.triage_events (case_id, actor, event, detail, created_at) VALUES (%s,%s,%s,%s,%s)",
                    (case_id, actor, event, Jsonb(detail), self.clock()))

    # ---------------- what a scan writes ----------------
    def sync(self, week: str, run_id: str | None, found: list[dict], scanned_kinds: set[str], hold: bool = False,
             known: dict[str, list[int]] | None = None) -> dict[str, list[dict]]:
        """Bring the week's cases in line with what a scan found. `scanned_kinds` are the checks that actually ran: a
        case of another kind stays as it is. `hold` (circuit breaker): no deadlines start. `known` maps a case key to
        the open findings that already cover it. Returns {"new", "changed", "reopened", "resolved", "unchanged"}."""
        out = {"new": [], "changed": [], "reopened": [], "resolved": [], "unchanged": []}
        known, now = known or {}, self.clock()
        with db.connect(self.url) as con:
            rows = con.execute(f"SELECT * FROM {self.s}.triage_cases WHERE week = %s", (week,)).fetchall()
            existing = {r["case_key"]: r for r in rows}
            for case in found:
                row = existing.get(case["case_key"])
                if row is None:
                    out["new"].append(self._insert(con, case, run_id, hold, known.get(case["case_key"])))
                    continue
                kind, status, change = self._next_state(row, case, hold)
                row = con.execute(f"""UPDATE {self.s}.triage_cases SET records = %s, record_count = %s, regions = %s,
                        impact_units = %s, evidence = evidence || %s, status = %s, change = %s, last_run_id = %s,
                        deadline_at = CASE WHEN %s THEN %s::timestamptz ELSE deadline_at END,
                        decision = CASE WHEN %s THEN NULL ELSE decision END, resolved_at = NULL, updated_at = %s
                        WHERE case_id = %s RETURNING *""",
                                  (Jsonb(case["records"]), case["record_count"], Jsonb(case["regions"]),
                                   case["impact_units"], Jsonb(case["evidence"]), status, change, run_id,
                                   kind in ("reopened", "released"), None if hold else self._deadline(),
                                   kind == "reopened", now, row["case_id"])).fetchone()
                if kind != "unchanged":
                    self._event(con, row["case_id"], "scan", kind, {"run_id": run_id, "records": case["record_count"]})
                out["reopened" if kind == "reopened" else "changed" if kind in ("changed", "released") else "unchanged"].append(row)
            seen = {c["case_key"] for c in found}
            for key, row in existing.items():
                if key in seen or row["status"] == "resolved" or row["kind"] not in scanned_kinds:
                    continue
                closed = con.execute(f"""UPDATE {self.s}.triage_cases SET status = 'resolved', change = 'resolved',
                        resolved_at = %s, updated_at = %s, last_run_id = %s WHERE case_id = %s RETURNING *""",
                                     (now, now, run_id, row["case_id"])).fetchone()
                self._event(con, row["case_id"], "scan", "resolved", {"run_id": run_id})
                out["resolved"].append(closed)
        return out

    def _insert(self, con, case: dict, run_id: str | None, hold: bool, finding_ids: list[int] | None) -> dict:
        now, status = self.clock(), "accepted" if finding_ids else "open"
        fields = {k: case[k] for k in ("case_key", "week", "kind", "error_code", "reason", "brand", "record_count",
                                       "impact_units") + PROPOSAL_FIELDS}
        fields |= {k: Jsonb(case[k]) for k in JSON_FIELDS}
        fields |= {"status": status, "change": "new", "first_run_id": run_id, "last_run_id": run_id, "created_at": now,
                   "updated_at": now, "finding_ids": Jsonb(finding_ids or []),
                   "deadline_at": None if hold or finding_ids else self._deadline(),
                   "decision": "known_finding" if finding_ids else None}
        row = con.execute(f"""INSERT INTO {self.s}.triage_cases ({', '.join(fields)})
                              VALUES ({', '.join(['%s'] * len(fields))}) RETURNING *""", list(fields.values())).fetchone()
        self._event(con, row["case_id"], "scan", "known_finding" if finding_ids else "opened", {"run_id": run_id})
        return row

    @staticmethod
    def _next_state(row: dict, case: dict, hold: bool) -> tuple[str, str, str]:
        """(event, status, change) for a case the scan found again."""
        keys = {_key(r) for r in case["records"]}
        if row["status"] == "resolved":
            return "reopened", "open", "reopened"
        if row["status"] == "waived":
            if keys <= set(row["waived_records"]):
                return "unchanged", "waived", "still_failing"
            return "reopened", "open", "reopened"                # a waiver covers the records it named, no others
        if keys != {_key(r) for r in row["records"]}:
            return "changed", row["status"], "changed"
        if row["status"] == "open" and row["deadline_at"] is None and not hold:
            return "released", "open", "changed"                 # the incident is over; the clock starts now
        return "unchanged", row["status"], "still_failing"

    def resolve_earlier_outcomes(self, week: str, flagged: set[tuple]) -> list[dict]:
        """An outcome case cannot go away on a rerun of its own week: its sales are final. It closes when a later week's
        scan, which did look at sales, no longer flags that brand in that region."""
        now, closed = self.clock(), []
        with db.connect(self.url) as con:
            rows = con.execute(f"""SELECT * FROM {self.s}.triage_cases WHERE kind = 'outcome' AND week < %s
                                   AND status <> 'resolved'""", (week,)).fetchall()
            for row in rows:
                if (row["brand"], row["regions"][0]) not in flagged:
                    done = con.execute(f"""UPDATE {self.s}.triage_cases SET status = 'resolved', change = 'resolved',
                            resolved_at = %s, updated_at = %s WHERE case_id = %s RETURNING *""",
                                       (now, now, row["case_id"])).fetchone()
                    self._event(con, row["case_id"], "scan", "resolved", {"week": week})
                    closed.append(done)
        return closed

    def save_proposal(self, case_id: int, investigation: dict, **proposal) -> dict:
        """Store what the LLM step produced. Code picked every field except the texts, and `investigation` keeps the
        full trace for audit."""
        sets = ", ".join(f"{k} = %s" for k in proposal)
        with db.connect(self.url) as con:
            row = con.execute(f"""UPDATE {self.s}.triage_cases SET {sets}{', ' if sets else ''}investigation = %s, updated_at = %s
                                  WHERE case_id = %s RETURNING *""",
                              [*proposal.values(), Jsonb(json.loads(json.dumps(investigation, default=str))), self.clock(),
                               case_id]).fetchone()
            self._event(con, case_id, "scan", "proposal", {"status": investigation.get("status")})
        return row

    # ---------------- reading ----------------
    def get(self, case_id: int) -> dict | None:
        with db.connect(self.url) as con:
            return con.execute(f"SELECT * FROM {self.s}.triage_cases WHERE case_id = %s", (case_id,)).fetchone()

    def list(self, week: str | None = None, status: str | None = None) -> list[dict]:
        where, args = [], []
        for column, value in (("week", week), ("status", status)):
            if value is not None:
                where.append(f"{column} = %s")
                args.append(value)
        with db.connect(self.url) as con:
            return con.execute(f"""SELECT * FROM {self.s}.triage_cases {'WHERE ' + ' AND '.join(where) if where else ''}
                ORDER BY CASE severity WHEN 'blocking' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, impact_units DESC,
                         record_count DESC, case_id""", args).fetchall()

    def events(self, case_id: int) -> list[dict]:
        with db.connect(self.url) as con:
            return con.execute(f"SELECT * FROM {self.s}.triage_events WHERE case_id = %s ORDER BY event_id", (case_id,)).fetchall()

    # ---------------- what people decide ----------------
    def _decide(self, con, case_id: int, user: str, decision: str, note: str | None, **sets) -> dict:
        row = con.execute(f"SELECT * FROM {self.s}.triage_cases WHERE case_id = %s FOR UPDATE", (case_id,)).fetchone()
        if row is None:
            raise KeyError(case_id)
        if row["status"] not in ("open", "escalated"):
            raise CaseConflict(f"case {case_id} is {row['status']}")
        fields = {"decided_by": user, "decision": decision, "decision_note": note, "updated_at": self.clock()} | sets
        row = con.execute(f"""UPDATE {self.s}.triage_cases SET {', '.join(f'{k} = %s' for k in fields)}
                              WHERE case_id = %s RETURNING *""", [*fields.values(), case_id]).fetchone()
        self._event(con, case_id, user, decision, {"note": note})
        return row

    def accept(self, case_id: int, user: str, findings: FindingsStore, note: str | None = None,
               ticket: str | None = None) -> dict:
        """The cause and action are agreed. One finding per region, because a finding has one region."""
        case = self.get(case_id)
        if case is None:
            raise KeyError(case_id)
        cause = (case["proposed_reason"] or note or "accepted without a stated cause")[:FINDING_CAUSE_LIMIT]
        with db.connect(self.url) as con:
            row = self._decide(con, case_id, user, "accept", note, status="accepted", deadline_at=None)
            ids = [findings.create(week=case["week"], region=region, error_code=case["error_code"], reason=case["reason"],
                                   skus=[r["sku"] for r in case["records"] if r["region"] == region], brand=case["brand"],
                                   root_cause=cause, owner=case["owner"], ticket=ticket, confirmed_by=user,
                                   evidence={"case_id": case_id, "confidence": case["confidence"]})["finding_id"]
                   for region in case["regions"]]
            return con.execute(f"UPDATE {self.s}.triage_cases SET finding_ids = %s WHERE case_id = %s RETURNING *",
                               (Jsonb(ids), case_id)).fetchone()

    def waive(self, case_id: int, user: str, note: str) -> dict:
        """An accepted exception. Covers exactly today's records, so new ones still alert."""
        with db.connect(self.url) as con:
            case = con.execute(f"SELECT records FROM {self.s}.triage_cases WHERE case_id = %s", (case_id,)).fetchone()
            if case is None:
                raise KeyError(case_id)
            return self._decide(con, case_id, user, "waive", note, status="waived", deadline_at=None,
                                waived_records=Jsonb([_key(r) for r in case["records"]]))

    def reject(self, case_id: int, user: str, reason: str, note: str | None = None, new_owner: str | None = None) -> dict:
        with db.connect(self.url) as con:
            if reason == "wrong_cause":
                return self._decide(con, case_id, user, reason, note, status="escalated", route=DEVELOPMENT,
                                    owner=DEVELOPMENT_TEAM, deadline_at=None)
            if reason == "wrong_owner":
                if not new_owner:
                    raise ValueError("wrong_owner needs a new owner")
                return self._decide(con, case_id, user, reason, note, status="open", owner=new_owner,
                                    route=route_for(new_owner), deadline_at=self._deadline())
            if reason == "action_refused":
                return self._decide(con, case_id, user, reason, note, status="escalated", route=BUSINESS,
                                    owner=self.decision_owner, deadline_at=None)
        raise ValueError(f"unknown reason {reason!r}")

    def sweep(self) -> list[dict]:
        """Escalate cases past their deadline. A business case goes to the decision owner, a development case stays with
        its team; either way it stops waiting silently."""
        now, out = self.clock(), []
        with db.connect(self.url) as con:
            rows = con.execute(f"""SELECT * FROM {self.s}.triage_cases WHERE status = 'open' AND deadline_at < %s
                                   FOR UPDATE""", (now,)).fetchall()
            for row in rows:
                owner = self.decision_owner if row["route"] == BUSINESS else row["owner"]
                done = con.execute(f"""UPDATE {self.s}.triage_cases SET status = 'escalated', owner = %s, deadline_at = NULL,
                        change = 'escalated', updated_at = %s WHERE case_id = %s RETURNING *""",
                                   (owner, now, row["case_id"])).fetchone()
                self._event(con, row["case_id"], "sweep", "deadline_missed", {"previous_owner": row["owner"]})
                out.append(done)
        return out

    # ---------------- scans and the outbox ----------------
    def log_scan(self, week: str, run_id: str | None, data_version: str | None, outcome_week: str | None, status: str,
                 summary: dict) -> None:
        with db.connect(self.url) as con:
            con.execute(f"""INSERT INTO {self.s}.triage_runs (week, run_id, data_version, outcome_week, status, summary, created_at)
                            VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                        (week, run_id, data_version, outcome_week, status, Jsonb(summary), self.clock()))

    def scans(self, week: str | None = None) -> list[dict]:
        with db.connect(self.url) as con:
            return con.execute(f"""SELECT * FROM {self.s}.triage_runs {'WHERE week = %s' if week else ''}
                                   ORDER BY triage_id DESC""", (week,) if week else ()).fetchall()

    def notify(self, week: str, run_id: str | None, team: str, kind: str, subject: str, body: str,
               case_ids: list[int]) -> dict:
        with db.connect(self.url) as con:
            return con.execute(f"""INSERT INTO {self.s}.triage_notifications
                (week, run_id, team, kind, subject, body, case_ids, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                               (week, run_id, team, kind, subject, body, Jsonb(case_ids), self.clock())).fetchone()

    def notifications(self, week: str | None = None) -> list[dict]:
        with db.connect(self.url) as con:
            return con.execute(f"""SELECT * FROM {self.s}.triage_notifications {'WHERE week = %s' if week else ''}
                                   ORDER BY notification_id DESC""", (week,) if week else ()).fetchall()
