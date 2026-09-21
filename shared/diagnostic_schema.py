"""State of the diagnostic-api in Postgres (Cloud SQL on GCP). Schema name from DIAGNOSTIC_SCHEMA.

sessions          one investigation per row; `state` and `history` are JSON, `version` is for optimistic locking
turn_transcripts  append-only audit log: every turn's full tool traffic, kept after a session expires
                  (BigQuery takes this role on GCP)
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
"""
