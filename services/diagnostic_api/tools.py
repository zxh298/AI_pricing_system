"""Diagnostic tools: thin, read-only, parameterised wrappers over the deterministic core.

Rules (docs/PROJECT_CONTEXT.md section 3.4):
  * The LLM never writes SQL and never compares prices; every verdict comes from core.diagnose
    or the pipeline's validation rules.
  * User and permissions arrive in a ToolContext injected by the service, never in LLM arguments.
  * Batch tools collapse identical per-SKU results into patterns (count + a few sample skus).
  * Tools take `skus` or a `group_id` (a handle the session state holds); groups say which products, never
    what their status is, so status is re-queried on every call.
  * Which pipeline run is diagnosed is decided by the service (pinned run, else the newest finished run for the
    week), never by the model.
  * Dispatch is allow-listed; every call writes one audit log line; failures come back as
    is_error results for the LLM instead of raising.
  * SAP is only ever read (GET, read key); this module has no way to write a price.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from jobs.weekly_pipeline.validate import validate_rows
from services.diagnostic_api.core import diagnose
from services.diagnostic_api.runs import RunResolver
from services.diagnostic_api.state import SessionState
from shared.config import load_config
from shared.warehouse import WarehouseClient, get_warehouse

audit = logging.getLogger("diagnostic.audit")

MAX_SKUS = 500
SAMPLE = 5
MAX_ITEMS = 100
WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")


# Shown to the model with get_sap_conditions results, so it cannot misread a field.
CONDITION_FIELDS = {
    "clearance_price_sent": "the markdown price we sent to SAP; not the shelf (full) price",
    "effective_price": "the price shoppers pay in SAP right now",
    "effective_source": "MARKDOWN (our price applies) or PROMOTION (a promotion overrides it)",
    "not available": "promotion dates, priorities and names: no tool returns them, so do not state them",
}


class ToolError(Exception):
    """A problem with the request itself; the message goes back to the LLM."""


@dataclass
class ToolContext:
    user: str
    wh: WarehouseClient
    sap: httpx.Client                                  # read-only key
    allowed_regions: frozenset[str] | None = None      # None = all regions
    run_id: str | None = None                          # pins the pipeline run; the model cannot override it
    state: SessionState | None = None                  # the session's groups and scope (None: no session)
    runs: RunResolver | None = None                    # finds the newest finished run of a week


def make_context(user: str, allowed_regions=None, run_id: str | None = None) -> ToolContext:
    """Warehouse opened read-only; SAP client carries the READ key only (the write key is never loaded)."""
    cfg = load_config()
    sap = httpx.Client(base_url=cfg.sap_base_url, headers={"X-API-Key": cfg.sap_read_key}, timeout=30)
    return ToolContext(user, get_warehouse(cfg, read_only=True), sap,
                       None if allowed_regions is None else frozenset(allowed_regions), run_id, runs=RunResolver())


# ---------------- helpers ----------------
def _as_of() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _week(week) -> str:
    if not isinstance(week, str) or not WEEK_RE.match(week):
        raise ToolError("week must look like 2026-W39")
    return week


def _skus(skus) -> list[str]:
    if not isinstance(skus, list) or not skus or not all(isinstance(s, str) for s in skus):
        raise ToolError("give skus (a non-empty list of strings) or a group_id")
    if len(set(skus)) > MAX_SKUS:
        raise ToolError(f"at most {MAX_SKUS} skus per call; narrow the scope")
    return sorted(set(skus))


def _regions(ctx: ToolContext, cands: list[dict], requested) -> list[str]:
    """Regions to look at: those on the week's list that the user may see, optionally narrowed."""
    present = {c["region"] for c in cands}
    allowed = sorted(present if ctx.allowed_regions is None else present & ctx.allowed_regions)
    if requested is None:
        return allowed
    if not isinstance(requested, list) or not all(isinstance(r, str) for r in requested):
        raise ToolError("regions must be a list of strings")
    bad = sorted(set(requested) - set(allowed))
    if bad:
        raise ToolError(f"regions not available: {bad}")
    return sorted(set(requested))


def _scope(ctx: ToolContext, week: str, skus, group_id, requested_regions, cands: list[dict]) -> tuple[list[str], list[str]]:
    """The skus and regions a call is about: an explicit list, or a group resolved earlier in the session."""
    if group_id is not None and skus is not None:
        raise ToolError("give skus or group_id, not both")
    if group_id is not None:
        if ctx.state is None:
            raise ToolError("there are no session groups here; pass skus instead of group_id")
        group = ctx.state.get_group(group_id)
        if group is None:
            raise ToolError(f"unknown group {group_id!r}; call resolve_products first")
        if group["week"] != week:
            raise ToolError(f"group {group_id} was resolved for {group['week']}, not {week}")
        skus = group["skus"]
        if requested_regions is None:
            requested_regions = group["regions"]
    return _skus(skus), _regions(ctx, cands, requested_regions)


def _run(ctx: ToolContext, requested: str | None, week: str) -> tuple[str | None, str | None]:
    """(run_id, data_version) to diagnose: the run the service pinned, else the newest finished run of the week,
    else what the model asked for (a warehouse with no pipeline state)."""
    latest = ctx.runs.latest(week) if ctx.runs else None
    if ctx.run_id:
        return ctx.run_id, latest["data_version"] if latest and latest["run_id"] == ctx.run_id else ctx.run_id
    if latest:
        return latest["run_id"], latest["data_version"]
    return requested, requested


def _patterns(records: list[dict], keys: list[str]) -> list[dict]:
    groups: dict[tuple, list[str]] = defaultdict(list)
    for r in records:
        groups[tuple(r[k] for k in keys)].append(r["sku"])
    out = [{**dict(zip(keys, k)), "count": len(v), "sample_skus": sorted(set(v))[:SAMPLE]} for k, v in groups.items()]
    return sorted(out, key=lambda p: (-p["count"], *[str(p[k]) for k in keys]))


def _sap_get(ctx: ToolContext, path: str, **params) -> dict:
    try:
        r = ctx.sap.get(path, params=params)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        raise ToolError(f"SAP request failed: {e}") from e


# ---------------- tools ----------------
def resolve_products(ctx: ToolContext, week: str, brand: str | None = None, category: str | None = None,
                     name_contains: str | None = None, regions: list[str] | None = None) -> dict:
    week = _week(week)
    cands = ctx.wh.get_candidates(week)
    regions = _regions(ctx, cands, regions)
    rows = [c for c in cands if c["region"] in regions and c["name"] is not None]
    if brand:
        rows = [c for c in rows if c["brand"].lower() == brand.lower()]
    if category:
        rows = [c for c in rows if c["category"].lower() == category.lower()]
    if name_contains:
        rows = [c for c in rows if name_contains.lower() in c["name"].lower()]
    skus = sorted({c["sku"] for c in rows})
    result = {"as_of": _as_of(), "week": week, "regions": regions, "count": len(skus),
              "skus": skus[:MAX_SKUS], "truncated": len(skus) > MAX_SKUS,
              "sample": [{"sku": c["sku"], "name": c["name"], "brand": c["brand"], "category": c["category"]}
                         for c in {r["sku"]: r for r in rows}.values()][:SAMPLE]}
    if ctx.state is not None:
        ctx.state.set_scope(week=week, brand=brand, category=category, name_contains=name_contains,
                            regions="/".join(regions))
        if skus:
            label = ", ".join(f"{k} {v}" for k, v in (("brand", brand), ("category", category),
                                                      ("name containing", name_contains)) if v) or "all products"
            result["group_id"] = ctx.state.add_group(skus, label, week, regions, result["as_of"], _run(ctx, None, week)[1])
    return result


def diagnose_batch(ctx: ToolContext, week: str, skus: list[str] | None = None, regions: list[str] | None = None,
                   run_id: str | None = None, group_id: str | None = None) -> dict:
    week = _week(week)
    cands = ctx.wh.get_candidates(week)
    skus, regions = _scope(ctx, week, skus, group_id, regions, cands)
    run, version = _run(ctx, run_id, week)
    recs = ctx.wh.get_recommendations(week, run)
    conditions = _sap_get(ctx, "/pricing/conditions", week=week)["items"]
    res = diagnose(skus, regions, cands, recs, ctx.wh.get_rules(week), conditions)
    return {"as_of": _as_of(), "week": week, "run_id": recs[0]["run_id"] if recs else None, "data_version": version,
            "skus_checked": len(skus), "regions": regions,
            "records_per_region": dict(Counter(x["region"] for x in res)),
            "totals": dict(Counter(x["code"] for x in res)),
            "by_region": {r: dict(Counter(x["code"] for x in res if x["region"] == r)) for r in regions},
            "patterns": _patterns([x for x in res if x["code"] != "OK"], ["region", "code", "reason"])}


def get_sap_conditions(ctx: ToolContext, week: str, skus: list[str] | None = None, regions: list[str] | None = None,
                       group_id: str | None = None) -> dict:
    """Live read of the prices SAP holds. Lists only items whose effective price is not our markdown."""
    week = _week(week)
    skus, regions = _scope(ctx, week, skus, group_id, regions, ctx.wh.get_candidates(week))
    wanted = set(skus)
    items = [c for c in _sap_get(ctx, "/pricing/conditions", week=week)["items"]
             if c["sku"] in wanted and c["region"] in regions]
    overridden = [c for c in items if c["effective_source"] != "MARKDOWN"]
    return {"as_of": _as_of(), "week": week, "regions": regions, "conditions_found": len(items),
            "by_effective_source": dict(Counter(f"{c['region']}:{c['effective_source']}" for c in items)),
            "overridden": [{"sku": c["sku"], "pack_qty": c["pack_qty"], "region": c["region"],
                            "clearance_price_sent": c["price"], "effective_price": c["effective_price"],
                            "effective_source": c["effective_source"]}
                           for c in overridden[:MAX_ITEMS]],
            "overridden_truncated": len(overridden) > MAX_ITEMS,
            "fields": CONDITION_FIELDS}


def get_api_log(ctx: ToolContext, week: str, skus: list[str] | None = None, regions: list[str] | None = None,
                run_id: str | None = None, group_id: str | None = None) -> dict:
    """What SAP answered for each record (latest answer per record), problems only, as patterns."""
    week = _week(week)
    skus, regions = _scope(ctx, week, skus, group_id, regions, ctx.wh.get_candidates(week))
    run_id = _run(ctx, run_id, week)[0]
    params = {"week": week} | ({"run_id": run_id} if run_id else {})
    wanted = set(skus)
    latest: dict[tuple, dict] = {}
    for row in _sap_get(ctx, "/pricing/submissions", **params)["items"]:        # oldest first: last wins
        if row["sku"] in wanted and row["region"] in regions:
            latest[(row["sku"], str(row["pack_qty"]), row["region"])] = row
    problems = [r for r in latest.values() if r["status"] != "ACCEPTED"]
    return {"as_of": _as_of(), "week": week, "records_logged": len(latest), "problems": len(problems),
            "patterns": _patterns(problems, ["region", "status", "code", "message"])}


def check_rules(ctx: ToolContext, week: str, skus: list[str] | None = None, regions: list[str] | None = None,
                run_id: str | None = None, group_id: str | None = None) -> dict:
    """Re-run the hard price rules (floor, max markdown, ladder, ...) on the engine's recommended prices."""
    week = _week(week)
    skus, regions = _scope(ctx, week, skus, group_id, regions, ctx.wh.get_candidates(week))
    run, version = _run(ctx, run_id, week)
    recs = ctx.wh.get_recommendations(week, run)
    rules = {(r["sku"], r["pack_qty"]): r for r in ctx.wh.get_rules(week)}
    wanted = set(skus)
    found = [v for v in validate_rows(recs, rules)                      # whole week: ladder needs the Single
             if v["sku"] in wanted and v["region"] in regions]
    return {"as_of": _as_of(), "week": week, "run_id": recs[0]["run_id"] if recs else None, "data_version": version,
            "priced_checked": sum(r["status"] == "PRICED" and r["sku"] in wanted and r["region"] in regions
                                  for r in recs),
            "violations": len(found), "patterns": _patterns(found, ["region", "code"])}


def search_docs(ctx: ToolContext, query: str) -> dict:
    """Playbook / incident search. Stub until the rag-ingest job exists (build step 6)."""
    return {"as_of": _as_of(), "results": [], "note": "document search is not available yet"}


# ---------------- registry: allow-listed dispatch ----------------
TOOLS = {f.__name__: f for f in (resolve_products, diagnose_batch, get_sap_conditions, get_api_log,
                                 check_rules, search_docs)}

_WEEK = {"type": "string", "description": "ISO week id such as 2026-W39"}
_SKUS = {"type": "array", "items": {"type": "string"}, "description": f"sku ids, at most {MAX_SKUS}"}
_REGIONS = {"type": "array", "items": {"type": "string"}, "description": "regions to include; default all"}
_RUN = {"type": "string", "description": "pipeline run id; leave unset unless the user names a run"}
_GROUP = {"type": "string", "description": "a group handle such as G1 from resolve_products; use it instead of skus"}


def _schema(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": props, "required": required}}


TOOL_SCHEMAS = [
    _schema("resolve_products", "Find the skus on this week's clearance list matching a brand, category or name. "
            "When the session keeps state it also returns a group_id handle for the skus; pass that to the other tools.",
            {"week": _WEEK, "brand": {"type": "string"}, "category": {"type": "string"},
             "name_contains": {"type": "string"}, "regions": _REGIONS}, ["week"]),
    _schema("diagnose_batch", "Deterministic verdict for every sku x region: OK, NOT_IN_LIST, MISSING_PRICE, "
            "RULE_VIOLATION, API_REJECTED, NOT_EFFECTIVE, ... Returns counts and patterns, not per-sku rows.",
            {"week": _WEEK, "skus": _SKUS, "group_id": _GROUP, "regions": _REGIONS, "run_id": _RUN}, ["week"]),
    _schema("get_sap_conditions", "Live read of the prices SAP holds; lists items whose effective price is "
            "overridden (e.g. by a promotion), with our clearance price sent and the effective price. It has no "
            "dates, priorities or names for promotions.", {"week": _WEEK, "skus": _SKUS, "group_id": _GROUP, "regions": _REGIONS},
            ["week"]),
    _schema("get_api_log", "What SAP answered for each submitted record; problems only, grouped by code and message.",
            {"week": _WEEK, "skus": _SKUS, "group_id": _GROUP, "regions": _REGIONS, "run_id": _RUN}, ["week"]),
    _schema("check_rules", "Re-check the hard price rules (floor, max markdown, ladder, max weeks) on the "
            "recommended prices. Use it for any 'would this break a rule' question.",
            {"week": _WEEK, "skus": _SKUS, "group_id": _GROUP, "regions": _REGIONS, "run_id": _RUN}, ["week"]),
    _schema("search_docs", "Search playbooks and incident notes for why something happens and who owns it.",
            {"query": {"type": "string"}}, ["query"]),
]


def execute_tool(name: str, args: dict | None, ctx: ToolContext) -> tuple[dict, bool]:
    """Run one allow-listed tool. Returns (result, is_error); never raises."""
    t0, args = time.perf_counter(), args or {}
    is_error = False
    try:
        fn = TOOLS.get(name)
        if fn is None:
            raise ToolError(f"unknown tool {name!r}")
        if "ctx" in args:
            raise ToolError("ctx is not a tool argument")
        result = fn(ctx, **args)
    except ToolError as e:
        result, is_error = {"error": str(e)}, True
    except Exception as e:                                          # noqa: BLE001 - reported to the LLM
        result, is_error = {"error": f"{type(e).__name__}: {e}"}, True
    audit.info(json.dumps({"user": ctx.user, "tool": name, "args": args, "is_error": is_error,
                           "ms": round((time.perf_counter() - t0) * 1000)}, default=str))
    return result, is_error
