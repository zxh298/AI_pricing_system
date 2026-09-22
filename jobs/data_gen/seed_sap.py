"""Load the SAP-owned tables (candidates, rules, past weeks' price conditions) into mock-sap's Postgres schema.

Drops the whole SAP schema first (received prices and the submission log too): a full reset.
"""
from __future__ import annotations

import pandas as pd

from shared import db
from shared.sap_schema import ddl, schema


def seed_sap(tables: dict[str, pd.DataFrame], database_url: str | None = None) -> dict[str, int]:
    s = schema()
    counts = {}
    with db.connect(database_url) as con:
        con.execute(f"DROP SCHEMA IF EXISTS {s} CASCADE")
        con.execute(ddl())
        for name in ("clearance_candidates", "business_rules", "price_conditions"):
            if name not in tables:
                continue
            df = tables[name]
            cols = list(df.columns)
            sql = f"INSERT INTO {s}.{name} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})"
            with con.cursor() as cur:
                cur.executemany(sql, [tuple(v.item() if hasattr(v, "item") else v for v in r)
                                      for r in df.itertuples(index=False)])
            counts[name] = len(df)
    return counts
