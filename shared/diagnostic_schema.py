"""State of the diagnostic-api in Postgres (Cloud SQL on GCP). Schema name from DIAGNOSTIC_SCHEMA.

sessions          one investigation per row; `state` and `history` are JSON, `version` is for optimistic locking
turn_transcripts  append-only audit log: every turn's full tool traffic, kept after a session expires
                  (BigQuery takes this role on GCP); `trace` holds a router turn's classification, slots and results
tool_cache        tool results shared between users with the same access. Snapshot results never expire but are
                  keyed on data_version, so a new pipeline run makes them unreachable; live (SAP) results carry a TTL
findings          issues a person has confirmed, shared by everyone; the model can read them but has no way to write
triage_runs       one row per weekly scan: what it covered and whether the circuit breaker opened
triage_cases      one grouped problem of a week, its proposal and the owner's decision; closed only by a re-scan
triage_events     append-only history of each case: who or what changed it, and why
triage_notifications  the outbox: one message per team per scan; a real sender would read from here
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
ALTER TABLE {s}.turn_transcripts ADD COLUMN IF NOT EXISTS trace JSONB;
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
CREATE TABLE IF NOT EXISTS {s}.triage_runs (
    triage_id BIGSERIAL PRIMARY KEY, week TEXT NOT NULL, run_id TEXT, data_version TEXT, outcome_week TEXT,
    status TEXT NOT NULL, summary JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL);
CREATE TABLE IF NOT EXISTS {s}.triage_cases (
    case_id BIGSERIAL PRIMARY KEY, case_key TEXT NOT NULL UNIQUE, week TEXT NOT NULL, kind TEXT NOT NULL,
    error_code TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', brand TEXT, regions JSONB NOT NULL,
    records JSONB NOT NULL, record_count INT NOT NULL, impact_units INT NOT NULL DEFAULT 0, severity TEXT,
    confidence TEXT NOT NULL, route TEXT NOT NULL, owner TEXT NOT NULL, source TEXT NOT NULL,
    proposed_reason TEXT NOT NULL DEFAULT '', proposed_action TEXT NOT NULL DEFAULT '', do_not TEXT NOT NULL DEFAULT '',
    doc_id TEXT, evidence JSONB NOT NULL DEFAULT '{{}}', investigation JSONB,
    status TEXT NOT NULL, change TEXT NOT NULL, deadline_at TIMESTAMPTZ, decided_by TEXT, decision TEXT,
    decision_note TEXT, finding_ids JSONB NOT NULL DEFAULT '[]', waived_records JSONB NOT NULL DEFAULT '[]',
    first_run_id TEXT, last_run_id TEXT, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
    resolved_at TIMESTAMPTZ);
CREATE INDEX IF NOT EXISTS triage_cases_week_status ON {s}.triage_cases (week, status);
CREATE TABLE IF NOT EXISTS {s}.triage_events (
    event_id BIGSERIAL PRIMARY KEY, case_id BIGINT NOT NULL, actor TEXT NOT NULL, event TEXT NOT NULL,
    detail JSONB, created_at TIMESTAMPTZ NOT NULL);
CREATE TABLE IF NOT EXISTS {s}.triage_notifications (
    notification_id BIGSERIAL PRIMARY KEY, week TEXT NOT NULL, run_id TEXT, team TEXT NOT NULL, kind TEXT NOT NULL,
    subject TEXT NOT NULL, body TEXT NOT NULL, case_ids JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL);
"""
