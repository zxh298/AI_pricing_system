"""Run state of the weekly pipeline, in Postgres (Cloud SQL on GCP). Schema name from PIPELINE_SCHEMA."""
import os


def schema() -> str:
    return os.environ.get("PIPELINE_SCHEMA", "pipeline")


def ddl() -> str:
    s = schema()
    return f"""
CREATE SCHEMA IF NOT EXISTS {s};
CREATE TABLE IF NOT EXISTS {s}.pipeline_runs (
    run_id TEXT PRIMARY KEY, week TEXT NOT NULL, status TEXT NOT NULL, attempt INT NOT NULL DEFAULT 1,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(), finished_at TIMESTAMPTZ,
    error TEXT, summary JSONB);
CREATE TABLE IF NOT EXISTS {s}.pipeline_steps (
    run_id TEXT NOT NULL, step TEXT NOT NULL, status TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(), finished_at TIMESTAMPTZ, detail JSONB,
    PRIMARY KEY (run_id, step));
CREATE TABLE IF NOT EXISTS {s}.pipeline_records (
    run_id TEXT NOT NULL, week TEXT NOT NULL, sku TEXT NOT NULL, pack_qty INT NOT NULL, region TEXT NOT NULL,
    recommended_price NUMERIC(10,2) NOT NULL,
    send_status TEXT, send_code TEXT, send_message TEXT, attempts INT,
    reconcile_status TEXT, reconcile_detail TEXT,
    PRIMARY KEY (run_id, sku, pack_qty, region));
"""
