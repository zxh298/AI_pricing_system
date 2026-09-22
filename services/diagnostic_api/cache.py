"""Tool-result cache in Postgres (Cloud SQL on GCP): no Redis, nothing always-on and paid.

Keys are built from a structured description of the call (tool, normalised arguments, the user's permitted
regions, the pipeline run and its data_version), never from question text or similarity, so "VIC" and "NSW"
or "this week" and "last week" can never be confused. Users with the same access share entries; a user with
narrower access has a different key and can never be served a wider result.

Two kinds of entry:
    snapshot  derived only from a pipeline run's outputs. No expiry: a new run changes data_version, so old
              entries are simply never asked for again (and are purged after a week).
    live      read from SAP, which changes outside the pipeline: reusable for a short TTL only.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Callable

from psycopg.types.json import Jsonb

from shared import db
from shared.config import load_config
from shared.diagnostic_schema import schema

SNAPSHOT_KEEP = timedelta(days=7)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ToolCache:
    def __init__(self, database_url: str | None = None, live_ttl_seconds: int | None = None,
                 clock: Callable[[], datetime] = _utcnow):
        self.url, self.s, self.clock = database_url, schema(), clock
        ttl = live_ttl_seconds if live_ttl_seconds is not None else load_config().tool_cache_ttl_seconds
        self.live_ttl = timedelta(seconds=ttl)

    @staticmethod
    def key(parts: dict) -> str:
        return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()

    def get(self, key: str) -> dict | None:
        with db.connect(self.url) as con:
            row = con.execute(f"SELECT result, expires_at FROM {self.s}.tool_cache WHERE cache_key = %s", (key,)).fetchone()
        if row is None or (row["expires_at"] is not None and row["expires_at"] <= self.clock()):
            return None
        return row["result"]

    def save(self, key: str, tool: str, data_version: str | None, result: dict, live: bool) -> None:
        now = self.clock()
        with db.connect(self.url) as con:
            con.execute(f"DELETE FROM {self.s}.tool_cache WHERE (expires_at IS NOT NULL AND expires_at <= %s) "
                        "OR created_at < %s", (now, now - SNAPSHOT_KEEP))
            con.execute(f"""INSERT INTO {self.s}.tool_cache (cache_key, tool, data_version, result, created_at, expires_at)
                VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (cache_key) DO UPDATE SET result = EXCLUDED.result,
                    data_version = EXCLUDED.data_version, created_at = EXCLUDED.created_at,
                    expires_at = EXCLUDED.expires_at""",
                        (key, tool, data_version, Jsonb(result), now, now + self.live_ttl if live else None))
