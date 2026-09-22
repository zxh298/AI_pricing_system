"""Postgres connection (local Docker / Cloud SQL). Only DATABASE_URL changes between them.

On Cloud Run with Cloud SQL, DATABASE_URL can use the unix socket form:
    postgresql://user:pass@/dbname?host=/cloudsql/PROJECT:REGION:INSTANCE
"""
import psycopg
from psycopg.rows import dict_row

from shared.config import load_config


def connect(url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(url or load_config().database_url, row_factory=dict_row)
