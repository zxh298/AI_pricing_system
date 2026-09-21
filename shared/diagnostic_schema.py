"""State of the diagnostic-api in Postgres (Cloud SQL on GCP). Schema name from DIAGNOSTIC_SCHEMA.

sessions          one investigation per row; `state` and `history` are JSON, `version` is for optimistic locking
turn_transcripts  append-only audit log: every turn's full tool traffic, kept after a session expires
                  (BigQuery takes this role on GCP)
tool_cache        tool results shared between users with the same access. Snapshot results never expire but are
                  keyed on data_version, so a new pipeline run makes them unreachable; live (SAP) results carry a TTL
findings          issues a person has confirmed, shared by everyone; the model can read them but has no way to write
"""
import os


def schema() -> str:
    return os.environ.get("DIAGNOSTIC_SCHEMA", "diagnostic")


def ddl() -> str:
    s = schema()
    return f"""
CREATE SCHEMA IF NOT EXISTS {s};
CREATE TABLE IF NOT EXISTS {s}.sessions (
    session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, week TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL, last_active_at TIMESTAMPTZ NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
    state JSONB NOT NULL DEFAULT '{{}}', history JSONB NOT NULL DEFAULT '[]', version INT NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS sessions_user ON {s}.sessions (user_id);
CREATE TABLE IF NOT EXISTS {s}.turn_transcripts (
    session_id TEXT NOT NULL, turn_no INT NOT NULL, user_id TEXT NOT NULL,
    question TEXT NOT NULL, answer TEXT NOT NULL, tool_calls JSONB NOT NULL, messages JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL, PRIMARY KEY (session_id, turn_no));
CREATE TABLE IF NOT EXISTS {s}.tool_cache (
    cache_key TEXT PRIMARY KEY, tool TEXT NOT NULL, data_version TEXT, result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL, expires_at TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS {s}.findings (
    finding_id BIGSERIAL PRIMARY KEY, week TEXT NOT NULL, brand TEXT, region TEXT NOT NULL,
    error_code TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', skus JSONB NOT NULL,
    root_cause TEXT NOT NULL, owner TEXT, ticket TEXT, status TEXT NOT NULL DEFAULT 'open',
    confirmed_by TEXT NOT NULL, evidence JSONB, created_at TIMESTAMPTZ NOT NULL,
    resolved_at TIMESTAMPTZ, resolved_by TEXT);
CREATE INDEX IF NOT EXISTS findings_week_status ON {s}.findings (week, status);
"""
