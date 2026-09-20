"""Tables owned by the (mock) SAP system, kept in their own Postgres schema.

SAP is the source of truth for the weekly clearance list and business rules. The schema
name comes from SAP_SCHEMA so tests can use a throwaway schema.
"""
import os


def schema() -> str:
    return os.environ.get("SAP_SCHEMA", "sap")


def ddl() -> str:
    s = schema()
    return f"""
CREATE SCHEMA IF NOT EXISTS {s};
CREATE TABLE IF NOT EXISTS {s}.clearance_candidates (
    week TEXT NOT NULL, sku TEXT NOT NULL, pack_type TEXT NOT NULL, pack_qty INT NOT NULL,
    region TEXT NOT NULL, shelf_price NUMERIC(10,2) NOT NULL, week_no INT NOT NULL,
    current_price NUMERIC(10,2) NOT NULL,
    PRIMARY KEY (week, sku, pack_qty, region));
CREATE TABLE IF NOT EXISTS {s}.business_rules (
    week TEXT NOT NULL, sku TEXT NOT NULL, pack_type TEXT NOT NULL, pack_qty INT NOT NULL,
    price_floor NUMERIC(10,2) NOT NULL, max_markdown_pct NUMERIC(4,3) NOT NULL,
    max_clearance_weeks INT NOT NULL, rule_version TEXT NOT NULL,
    PRIMARY KEY (week, sku, pack_qty));
CREATE TABLE IF NOT EXISTS {s}.price_conditions (
    id BIGSERIAL PRIMARY KEY, idem_key TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL, week TEXT NOT NULL, sku TEXT NOT NULL, pack_qty INT NOT NULL,
    region TEXT NOT NULL, price NUMERIC(10,2) NOT NULL,
    valid_from DATE NOT NULL, valid_to DATE NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS price_conditions_item
    ON {s}.price_conditions (sku, pack_qty, region);
"""
