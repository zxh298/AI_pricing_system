"""Reconciliation: 'API accepted' is not 'price in effect'. Compare what we sent with what SAP holds."""
from __future__ import annotations

EPS = 0.005


def reconcile(sent: list[dict], conditions: list[dict]) -> list[dict]:
    """sent: rows with sku, pack_qty, region, recommended_price (accepted by SAP).
    conditions: items from GET /pricing/conditions. Returns one result per sent row:
        CONFIRMED      SAP holds our price and it is the effective price
        NOT_EFFECTIVE  SAP holds our price but something overrides it (e.g. a promotion)
        PRICE_MISMATCH SAP holds a different price
        MISSING_IN_SAP no condition record for this item"""
    held = {(c["sku"], c["pack_qty"], c["region"]): c for c in conditions}
    out = []
    for r in sent:
        key = (r["sku"], r["pack_qty"], r["region"])
        c = held.get(key)
        if c is None:
            status, detail = "MISSING_IN_SAP", "no condition record in SAP"
        elif abs(c["price"] - r["recommended_price"]) > EPS:
            status, detail = "PRICE_MISMATCH", f"SAP holds {c['price']}, sent {r['recommended_price']}"
        elif abs(c["effective_price"] - c["price"]) > EPS:
            status = "NOT_EFFECTIVE"
            detail = f"effective price {c['effective_price']} from {c['effective_source']}, sent {r['recommended_price']}"
        else:
            status, detail = "CONFIRMED", ""
        out.append({"sku": r["sku"], "pack_qty": r["pack_qty"], "region": r["region"],
                    "reconcile_status": status, "reconcile_detail": detail})
    return out
