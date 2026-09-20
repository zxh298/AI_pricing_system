"""Send recommended prices to SAP in batches. Per-record results come back from SAP.

Retry by error type, record-level status persistence and reconciliation are added later
(with fault injection in mock-sap).
"""
from __future__ import annotations

from datetime import date, timedelta

import httpx

from shared.config import load_config


def week_dates(week: str) -> tuple[date, date]:
    y, w = week.split("-W")
    monday = date.fromisocalendar(int(y), int(w), 1)
    return monday, monday + timedelta(days=6)


def to_payload(priced_rows: list[dict], week: str) -> list[dict]:
    valid_from, valid_to = week_dates(week)
    return [{"sku": r["sku"], "pack_qty": r["pack_qty"], "region": r["region"],
             "markdown_price": r["recommended_price"],
             "valid_from": valid_from.isoformat(), "valid_to": valid_to.isoformat()}
            for r in priced_rows]


def send(client: httpx.Client, week: str, run_id: str, prices: list[dict], batch_size: int = 50) -> list[dict]:
    results = []
    for i in range(0, len(prices), batch_size):
        r = client.post("/pricing/markdown-prices",
                        json={"run_id": run_id, "week": week, "prices": prices[i:i + batch_size]})
        r.raise_for_status()
        results.extend(r.json()["results"])
    return results


def sap_write_client() -> httpx.Client:
    cfg = load_config()
    return httpx.Client(base_url=cfg.sap_base_url, headers={"X-API-Key": cfg.sap_write_key}, timeout=30)
