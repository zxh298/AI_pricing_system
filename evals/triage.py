"""Does the investigation agent find the cause planted in the data? Needs ANTHROPIC_API_KEY for the agent; no Postgres.

    python -m evals.triage --check              the deterministic part only: is each scenario flagged? (no key, no cost)
    python -m evals.triage --runs 2 --write     run the agent, write evals/TRIAGE_RESULTS.md

Each scenario plants one known cause in a throwaway copy of the warehouse; the true cause lives only in the planter's
return value, never in anything the agent can read. A cause counts only if code's own re-check kept it, so "right"
means the agent named it and the signal is really there.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from types import SimpleNamespace

import httpx

from jobs.data_gen.outcome_scenarios import SCENARIOS
from jobs.weekly_triage.scan import _outcome_flags
from services.diagnostic_api.investigate import investigate
from services.diagnostic_api.llm import get_triage_llm
from services.diagnostic_api.tools import ToolContext
from services.diagnostic_api.triage_rules import outcome_cases
from shared.config import load_config
from shared.envfile import load_dotenv
from shared.warehouse import DuckDBWarehouse

SOURCE = "data/warehouse.duckdb"
WEEK, SOLD = "2026-W39", "2026-W38"
RESULTS = "evals/TRIAGE_RESULTS.md"


def fake_sap(wh: DuckDBWarehouse, week: str, promotions: list[dict] = (), extra: list[dict] = ()) -> httpx.Client:
    """Read-only SAP for a past week: what price_history says was held, with `promotions` on top. A POST is refused,
    as it would be for the read key."""
    promo = {(p["sku"], p["pack_qty"], p["region"]): p["promo_price"] for p in promotions}
    rows = wh._rows("SELECT sku, pack_qty, region, price FROM price_history WHERE week = ? AND is_clearance", [week])
    held = [{"sku": r["sku"], "pack_qty": r["pack_qty"], "region": r["region"], "price": r["price"],
             "effective_price": promo.get((r["sku"], r["pack_qty"], r["region"]), r["price"]),
             "effective_source": "PROMOTION" if (r["sku"], r["pack_qty"], r["region"]) in promo else "MARKDOWN"} for r in rows]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method != "GET":
            return httpx.Response(403, text="write key required")
        items = (held if req.url.params.get("week") == week else list(extra)) if req.url.path == "/pricing/conditions" else []
        return httpx.Response(200, json={"week": week, "count": len(items), "items": items})

    return httpx.Client(base_url="http://sap", transport=httpx.MockTransport(handler))


def flagged_cases(path: str, promotions: list[dict], cfg=None) -> list[dict]:
    """The outcome check on one copy, exactly as the scan runs it. Returns the cases it would open."""
    cfg = cfg or load_config()
    wh = DuckDBWarehouse(path)
    ctx = ToolContext("eval", wh, fake_sap(wh, SOLD, promotions))
    flagged, _, sold, _ = _outcome_flags(SimpleNamespace(ctx=ctx, cfg=cfg), WEEK)
    return outcome_cases(WEEK, sold, flagged)


def check(tmp: str) -> dict:
    """For every scenario: the planted records, and whether the check flagged exactly them."""
    out = {}
    for name, plant in SCENARIOS.items():
        path = os.path.join(tmp, f"{name}.duckdb")
        shutil.copy(SOURCE, path)
        truth = plant(path, SOLD)
        cases = flagged_cases(path, truth["promotions"])
        got = {(r["sku"], r["pack_qty"], r["region"]) for c in cases for r in c["records"]}
        planted = {tuple(r) for r in truth["records"]}
        out[name] = {"planted": len(planted), "flagged": len(got), "missed": sorted(planted - got, key=str),
                     "extra": sorted(got - planted, key=str)}
    return out


def expected(truth: dict) -> dict[str, set[str]]:
    return ({k: set(v) for k, v in truth["causes"].items()} if "causes" in truth
            else {truth["cause"]: {r[0] for r in truth["records"]}})


def score(truth: dict, result: dict) -> dict:
    want = expected(truth)
    got: dict[str, set[str]] = {}
    for c in result["causes"]:
        got.setdefault(c["cause"], set()).update(c["skus"])
    rival = truth.get("rival")
    followed = None if rival is None else any(t["tool"] == "get_sap_conditions" and rival[0] in (t["input"].get("skus") or [])
                                              for t in result["trace"])
    return {"cause_right": set(got) == set(want), "skus_right": got == want, "status": result["status"], "steps": result["steps"],
            "followed_evidence": followed,
            "tools": [t["tool"] for t in result["trace"]], "got": sorted(got), "dropped": [d["cause"] for d in result["dropped"]],
            "unverified": result.get("unverified", [])}


def run_all(tmp: str, runs: int, names: list[str]) -> dict:
    cfg, out = load_config(), {}
    for name in names:
        out[name] = []
        for i in range(runs):
            path = os.path.join(tmp, f"{name}-{i}.duckdb")
            shutil.copy(SOURCE, path)
            truth = SCENARIOS[name](path, SOLD)
            wh = DuckDBWarehouse(path)
            cases = flagged_cases(path, truth["promotions"], cfg)
            ctx = ToolContext("eval", wh, fake_sap(wh, SOLD, truth["promotions"]))
            for case in cases:
                result = investigate(case, ctx, get_triage_llm(), cfg.triage_max_steps)
                out[name].append(score(truth, result))
                print(name, i, json.dumps(out[name][-1]), flush=True)
    return out


def report(results: dict, model: str) -> str:
    rows = [r for v in results.values() for r in v]
    right = sum(r["cause_right"] for r in rows)
    lines = ["# Triage agent evaluation", "", f"Model `{model}`, regenerated by `python -m evals.triage --write`.", "",
             f"Cause right in {right} of {len(rows)} investigations; "
             f"cause and skus both right in {sum(r['skus_right'] for r in rows)}.", "",
             "`substitute` is the one scenario where the agent must learn something from a result before it can ask the next "
             "question: the product to check is not in the case. `followed` says whether it did.", "",
             "| scenario | run | cause right | skus right | model calls | followed | found | dropped by code | tools |",
             "|---|---|---|---|---:|---|---|---|---|"]
    for name, runs in results.items():
        for i, r in enumerate(runs, 1):
            lines.append(f"| {name} | {i} | {'yes' if r['cause_right'] else 'NO'} | {'yes' if r['skus_right'] else 'NO'} | {r['steps']} | "
                         f"{'-' if r['followed_evidence'] is None else 'yes' if r['followed_evidence'] else 'NO'} | {', '.join(r['got']) or '-'} | {', '.join(r['dropped']) or '-'} | {' → '.join(r['tools'])} |")
    lines += ["", "Planted scenarios on synthetic data, in a small set. This shows the loop works on causes it was told "
              "about; it does not measure a cause nobody has seen, which is what the agent is for in production."]
    return "\n".join(lines) + "\n"


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--only", nargs="*", default=sorted(SCENARIOS))
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        if a.check:
            print(json.dumps(check(tmp), indent=2, default=str))
            return 0
        results = run_all(tmp, a.runs, a.only)
    text = report(results, load_config().triage_model)
    print(text)
    if a.write:
        with open(RESULTS, "w") as f:
            f.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
