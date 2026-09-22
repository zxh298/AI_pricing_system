"""Shared fixtures. `env` is the planted "VIC broken, NSW fine" scenario used by the diagnostic tests.

DuckDB comes from the seeded generator, the pricing engine runs for real, and SAP is a fake read-only HTTP
transport, so nothing here needs Postgres or the network.
"""
import os

import httpx
import pytest

os.environ["SAP_SCHEMA"] = "sap_test"
os.environ["SAP_READ_KEY"], os.environ["SAP_WRITE_KEY"] = "rk", "wk"

from jobs.data_gen.generate import WEEK, write
from jobs.pricing_engine.engine import price_week, record_send_results, save_recommendations
from services.diagnostic_api.tools import ToolContext
from shared.warehouse import DuckDBWarehouse

RUN = "run-1"
OVERLAP_MSG = "validity overlaps existing condition record (run LEGACY-PREV-WEEK, 2026-09-14 to 2099-12-31)"


def fake_sap(rows, results):
    """Read-only SAP: conditions with a VIC promotion override on `promo` items, and the submission log."""
    promo, held, log = rows["promo"], [], []
    for i, r in enumerate(results, 1):
        log.append({"id": i, "run_id": RUN, "week": WEEK, "sku": r["sku"], "pack_qty": str(r["pack_qty"]),
                    "region": r["region"], "status": r["status"], "code": r["code"], "message": r["message"]})
        if r["status"] != "ACCEPTED":
            continue
        price = rows["price"][(r["sku"], r["pack_qty"], r["region"])]
        is_promo = (r["sku"], r["pack_qty"], r["region"]) in promo
        held.append({"sku": r["sku"], "pack_qty": r["pack_qty"], "region": r["region"], "price": price,
                     "effective_price": round(price * 0.7, 2) if is_promo else price,
                     "effective_source": "PROMOTION" if is_promo else "MARKDOWN",
                     "valid_from": "2026-09-21", "valid_to": "2026-09-27"})

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method != "GET":
            return httpx.Response(403, text="write key required")           # the diagnostic client is read-only
        items = {"/pricing/conditions": held, "/pricing/submissions": log}[req.url.path]
        return httpx.Response(200, json={"week": WEEK, "count": len(items), "items": items})

    return httpx.Client(base_url="http://sap", transport=httpx.MockTransport(handler))


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("d") / "w.duckdb")
    write(path, seed=1, with_sap_tables=True)
    wh = DuckDBWarehouse(path)
    rows = price_week(WEEK, wh)
    save_recommendations(path, RUN, WEEK, rows)
    vic = [r for r in rows if r["status"] == "PRICED" and r["region"] == "VIC" and r["pack_qty"] == 1]
    promo = {(r["sku"], 1, "VIC") for r in vic[:3]}
    rejected = {(r["sku"], 1, "VIC") for r in vic[3:5]}
    results = [{"sku": r["sku"], "pack_qty": r["pack_qty"], "region": r["region"],
                "status": "REJECTED" if (r["sku"], r["pack_qty"], r["region"]) in rejected else "ACCEPTED",
                "code": "VALIDITY_OVERLAP" if (r["sku"], r["pack_qty"], r["region"]) in rejected else "OK",
                "message": OVERLAP_MSG if (r["sku"], r["pack_qty"], r["region"]) in rejected else "accepted"}
               for r in rows if r["status"] == "PRICED"]
    record_send_results(path, RUN, WEEK, results)
    price = {(r["sku"], r["pack_qty"], r["region"]): r["recommended_price"] for r in rows if r["status"] == "PRICED"}
    sap = fake_sap({"promo": promo, "price": price}, results)
    return {"path": path, "wh": wh, "ctx": ToolContext("tester", wh, sap), "sap": sap, "rows": rows,
            "skus": sorted({r["sku"] for r in rows}), "promo": promo, "rejected": rejected,
            "run": RUN, "overlap_msg": OVERLAP_MSG}
