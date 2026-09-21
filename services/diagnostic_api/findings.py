"""Findings: issues a person has confirmed, shared by everyone (Postgres).

A finding says "this class of problem, in this region, for these SKUs, has this root cause". It is
checked before an investigation starts, so a known issue is recognised instead of being rediscovered.

Memory-poisoning guard (docs section 4.4): only people write findings. The LLM has no tool that creates
or edits one; it can only read open findings. The one automatic change is deterministic code, not the
model: a finding is marked resolved when a check that fully covers its scope finds none of its failures.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from psycopg.types.json import Jsonb

from shared import db
from shared.diagnostic_schema import schema


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FindingsStore:
    def __init__(self, database_url: str | None = None, clock: Callable[[], datetime] = _utcnow):
        self.url, self.s, self.clock = database_url, schema(), clock

    def create(self, *, week: str, region: str, error_code: str, root_cause: str, confirmed_by: str, skus: list[str],
               reason: str = "", brand: str | None = None, owner: str | None = None, ticket: str | None = None,
               evidence: dict | None = None) -> dict:
        with db.connect(self.url) as con:
            return con.execute(f"""INSERT INTO {self.s}.findings
                (week, brand, region, error_code, reason, skus, root_cause, owner, ticket, confirmed_by, evidence, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                               (week, brand, region, error_code, reason, Jsonb(sorted(set(skus))), root_cause, owner,
                                ticket, confirmed_by, Jsonb(evidence), self.clock())).fetchone()

    def get(self, finding_id: int) -> dict | None:
        with db.connect(self.url) as con:
            return con.execute(f"SELECT * FROM {self.s}.findings WHERE finding_id = %s", (finding_id,)).fetchone()

    def list(self, *, week: str | None = None, status: str | None = "open", region: str | None = None,
             error_code: str | None = None, brand: str | None = None,
             allowed_regions: frozenset[str] | None = None) -> list[dict]:
        """Findings matching the filters, newest first. `allowed_regions` None = every region."""
        where, args = [], []
        for column, value in (("week", week), ("status", status), ("region", region), ("error_code", error_code),
                              ("brand", brand)):
            if value is not None:
                where.append(f"{column} = %s")
                args.append(value)
        if allowed_regions is not None:
            where.append("region = ANY(%s)")
            args.append(sorted(allowed_regions))
        with db.connect(self.url) as con:
            return con.execute(f"SELECT * FROM {self.s}.findings" + (" WHERE " + " AND ".join(where) if where else "")
                               + " ORDER BY created_at DESC, finding_id DESC", args).fetchall()

    def resolve(self, finding_id: int, by: str) -> dict | None:
        """Close an open finding. None if it does not exist or is already closed."""
        with db.connect(self.url) as con:
            return con.execute(f"""UPDATE {self.s}.findings SET status = 'resolved', resolved_at = %s, resolved_by = %s
                                   WHERE finding_id = %s AND status = 'open' RETURNING *""",
                               (self.clock(), by, finding_id)).fetchone()

    def auto_resolve(self, week: str, regions: list[str], skus: list[str], failing: dict[str, str]) -> list[int]:
        """Close open findings that a completed check proves are gone. `failing` maps 'sku|pack|region' to
        'CODE:reason' for every failing record of the check. A finding closes only if the check covered its whole
        scope (its region and all its SKUs) and none of those records still fail with its code and reason."""
        checked, region_set, closed = set(skus), set(regions), []
        for f in self.list(week=week, status="open"):
            if f["region"] not in region_set or not set(f["skus"]) <= checked:
                continue                                              # the check did not cover this finding
            still = f"{f['error_code']}:{f['reason']}"
            if any(v == still and k.split("|")[2] == f["region"] and k.split("|")[0] in f["skus"]
                   for k, v in failing.items()):
                continue
            if self.resolve(f["finding_id"], "auto"):
                closed.append(f["finding_id"])
        return closed
