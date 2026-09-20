"""mock-sap: stands in for the external SAP system of record.

    GET  /clearance/candidates?week=   weekly clearance list + business rules  (read or write key)
    POST /pricing/markdown-prices      receive recommended prices, per-record result (write key)
    GET  /pricing/conditions?week=     prices SAP currently holds                (read or write key)
    GET  /healthz

SAP-side validation is deliberately thin (format, article on list, validity overlap). Business
rules (floor, markdown cap, ladder) are the pricing engine's job; reconciliation catches the rest.
"""
from __future__ import annotations

import os
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request

from shared import db
from shared.sap_schema import ddl, schema

MAX_BATCH = int(os.environ.get("SAP_MAX_BATCH", "500"))

app = FastAPI(title="mock-sap")


@app.on_event("startup")
def _init_schema() -> None:
    with db.connect() as con:
        con.execute(ddl())


# ---------------- auth: read key for GET, write key for POST (write key also reads) ----------
def _keys() -> tuple[str, str]:
    return os.environ.get("SAP_READ_KEY", "dev-read-key"), os.environ.get("SAP_WRITE_KEY", "dev-write-key")


def require_read(x_api_key: str | None = Header(default=None)) -> str:
    read, write = _keys()
    if x_api_key not in (read, write):
        raise HTTPException(401, "invalid or missing API key")
    return x_api_key


def require_write(x_api_key: str | None = Header(default=None)) -> str:
    _, write = _keys()
    if x_api_key is None:
        raise HTTPException(401, "invalid or missing API key")
    if x_api_key != write:
        raise HTTPException(403, "write key required")
    return x_api_key


@app.get("/healthz")
def healthz():
    with db.connect() as con:
        con.execute("SELECT 1")
    return {"status": "ok"}


# ---------------- GET candidates + rules ----------------
@app.get("/clearance/candidates", dependencies=[Depends(require_read)])
def candidates(week: str = Query(..., pattern=r"^\d{4}-W\d{2}$")):
    s = schema()
    with db.connect() as con:
        rows = con.execute(f"""
            SELECT c.week, c.sku, c.pack_type, c.pack_qty, c.region,
                   c.shelf_price::float AS shelf_price, c.week_no,
                   c.current_price::float AS current_price,
                   r.price_floor::float AS price_floor,
                   r.max_markdown_pct::float AS max_markdown_pct,
                   r.max_clearance_weeks, r.rule_version
            FROM {s}.clearance_candidates c
            LEFT JOIN {s}.business_rules r USING (week, sku, pack_qty)
            WHERE c.week = %s ORDER BY c.sku, c.pack_qty, c.region""", (week,)).fetchall()
    if not rows:
        raise HTTPException(404, f"no clearance list for {week}")
    return {"week": week, "count": len(rows), "items": rows}


# ---------------- POST prices ----------------
def _validate(rec: dict) -> tuple[dict | None, tuple[str, str] | None]:
    """Format checks only. Returns (clean record, None) or (None, (code, message))."""
    try:
        sku = str(rec["sku"]).strip()
        pack_qty = int(rec["pack_qty"])
        region = str(rec["region"]).strip()
        price = Decimal(str(rec["markdown_price"]))
        valid_from = date.fromisoformat(str(rec["valid_from"]))
        valid_to = date.fromisoformat(str(rec["valid_to"]))
    except KeyError as e:
        return None, ("BAD_FORMAT", f"missing field {e.args[0]}")
    except (ValueError, TypeError, InvalidOperation):
        return None, ("BAD_FORMAT", "unparseable field value")
    if not sku or not region or pack_qty < 1:
        return None, ("BAD_FORMAT", "sku, region and pack_qty must be set")
    if price <= 0 or price != price.quantize(Decimal("0.01")):
        return None, ("BAD_FORMAT", "markdown_price must be positive with at most 2 decimals")
    if valid_from > valid_to:
        return None, ("BAD_FORMAT", "valid_from is after valid_to")
    return dict(sku=sku, pack_qty=pack_qty, region=region, price=price,
                valid_from=valid_from, valid_to=valid_to), None


@app.post("/pricing/markdown-prices", dependencies=[Depends(require_write)])
async def post_prices(request: Request):
    try:
        body = await request.json()
        run_id, week, prices = str(body["run_id"]), str(body["week"]), body["prices"]
        assert isinstance(prices, list)
    except Exception:
        raise HTTPException(400, "body must be JSON with run_id, week and a prices list")
    if len(prices) > MAX_BATCH:
        raise HTTPException(413, f"batch too large (max {MAX_BATCH})")

    s, results = schema(), []
    with db.connect() as con:
        for rec in prices:
            clean, err = _validate(rec) if isinstance(rec, dict) else (None, ("BAD_FORMAT", "record is not an object"))
            ident = {k: (rec.get(k) if isinstance(rec, dict) else None) for k in ("sku", "pack_qty", "region")}
            if err:
                results.append({**ident, "status": "REJECTED", "code": err[0], "message": err[1], "duplicate": False})
                continue
            c = clean
            idem = f"{run_id}|{c['sku']}|{c['pack_qty']}|{c['region']}|{c['valid_from']}"
            ident = {"sku": c["sku"], "pack_qty": c["pack_qty"], "region": c["region"]}

            # idempotent replay: same key already accepted
            prev = con.execute(f"SELECT price FROM {s}.price_conditions WHERE idem_key = %s", (idem,)).fetchone()
            if prev:
                if prev["price"] == c["price"]:
                    results.append({**ident, "status": "ACCEPTED", "code": "OK", "message": "already accepted", "duplicate": True})
                else:
                    results.append({**ident, "status": "REJECTED", "code": "IDEMPOTENCY_CONFLICT",
                                    "message": "same key was accepted with a different price", "duplicate": True})
                continue
            # article must be on SAP's own list for that week and region
            on_list = con.execute(
                f"SELECT 1 FROM {s}.clearance_candidates WHERE week=%s AND sku=%s AND pack_qty=%s AND region=%s",
                (week, c["sku"], c["pack_qty"], c["region"])).fetchone()
            if not on_list:
                results.append({**ident, "status": "REJECTED", "code": "NOT_ON_LIST",
                                "message": f"not on the {week} clearance list", "duplicate": False})
                continue
            # validity period must not overlap an existing condition record
            clash = con.execute(
                f"""SELECT run_id, valid_from, valid_to FROM {s}.price_conditions
                    WHERE sku=%s AND pack_qty=%s AND region=%s AND valid_from <= %s AND valid_to >= %s""",
                (c["sku"], c["pack_qty"], c["region"], c["valid_to"], c["valid_from"])).fetchone()
            if clash:
                results.append({**ident, "status": "REJECTED", "code": "VALIDITY_OVERLAP",
                                "message": f"validity overlaps existing condition record (run {clash['run_id']}, "
                                           f"{clash['valid_from']} to {clash['valid_to']})", "duplicate": False})
                continue
            con.execute(
                f"""INSERT INTO {s}.price_conditions
                    (idem_key, run_id, week, sku, pack_qty, region, price, valid_from, valid_to)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (idem, run_id, week, c["sku"], c["pack_qty"], c["region"], c["price"], c["valid_from"], c["valid_to"]))
            results.append({**ident, "status": "ACCEPTED", "code": "OK", "message": "accepted", "duplicate": False})

    accepted = sum(r["status"] == "ACCEPTED" for r in results)
    return {"run_id": run_id, "week": week,
            "summary": {"received": len(results), "accepted": accepted,
                        "rejected": len(results) - accepted,
                        "duplicates": sum(r["duplicate"] for r in results)},
            "results": results}


# ---------------- GET conditions ----------------
@app.get("/pricing/conditions", dependencies=[Depends(require_read)])
def conditions(week: str = Query(..., pattern=r"^\d{4}-W\d{2}$")):
    with db.connect() as con:
        rows = con.execute(f"""
            SELECT run_id, week, sku, pack_qty, region, price::float AS price, valid_from, valid_to
            FROM {schema()}.price_conditions WHERE week = %s ORDER BY sku, pack_qty, region""", (week,)).fetchall()
    return {"week": week, "count": len(rows), "items": rows}
