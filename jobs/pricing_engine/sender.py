"""Send recommended prices to SAP in batches. Per-record results come back from SAP.

Retry by error type:
    429 / 5xx / network errors  -> exponential backoff (honours Retry-After), up to max_retries
    400 / 401 / 403 / 413 / 422 -> data or auth problem: no retry, recorded as ERROR for escalation
A batch that still fails is reported as ERROR for each of its records; other batches carry on
(partial failure is tracked per record, never as all-pass / all-fail).
"""
from __future__ import annotations

import time
from datetime import date, timedelta

import httpx

from shared.config import load_config

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


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


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return float(resp.headers["Retry-After"])
    except (KeyError, ValueError):
        return None


def send(client: httpx.Client, week: str, run_id: str, prices: list[dict], batch_size: int = 50,
         max_retries: int = 4, base_delay: float = 0.5, max_delay: float = 30.0,
         sleep=time.sleep) -> list[dict]:
    """POST prices in batches. Every input record gets exactly one result dict with
    status ACCEPTED / REJECTED (from SAP) or ERROR (request failed), plus `attempts`."""
    results: list[dict] = []
    for i in range(0, len(prices), batch_size):
        batch = prices[i:i + batch_size]
        attempts = 0
        while True:
            attempts += 1
            status, retry_after, err = None, None, None
            try:
                resp = client.post("/pricing/markdown-prices",
                                   json={"run_id": run_id, "week": week, "prices": batch})
            except httpx.TransportError as e:
                code, err = "TRANSPORT", f"{type(e).__name__}: {e}"
            else:
                if resp.status_code == 200:
                    results.extend({**r, "attempts": attempts} for r in resp.json()["results"])
                    break
                status, retry_after = resp.status_code, _retry_after(resp)
                code, err = f"HTTP_{status}", resp.text[:200]
            retryable = status is None or status in RETRYABLE_STATUS
            if retryable and attempts <= max_retries:
                sleep(min(max_delay, retry_after if retry_after is not None else base_delay * 2 ** (attempts - 1)))
                continue
            results.extend({"sku": r["sku"], "pack_qty": r["pack_qty"], "region": r["region"],
                            "status": "ERROR", "code": code, "message": err, "duplicate": False,
                            "attempts": attempts} for r in batch)
            break
    return results


def sap_write_client() -> httpx.Client:
    cfg = load_config()
    return httpx.Client(base_url=cfg.sap_base_url, headers={"X-API-Key": cfg.sap_write_key}, timeout=30)
