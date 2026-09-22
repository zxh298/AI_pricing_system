"""Step 5, slice 3c: tool cache, repeat-question diffs and findings.

The pure logic needs nothing. The rest needs the Postgres container (docker compose up -d postgres) and uses the
throwaway schema `diagnostic_test`. SAP is a fake whose answers a test can change ("the promotion ended").
"""
import copy
import dataclasses
import inspect
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

os.environ["SAP_SCHEMA"], os.environ["DIAGNOSTIC_SCHEMA"] = "sap_test", "diagnostic_test"

from jobs.data_gen.generate import WEEK
from services.diagnostic_api import tools
from services.diagnostic_api.app import create_app
from services.diagnostic_api.cache import ToolCache
from services.diagnostic_api.findings import FindingsStore, render_block
from services.diagnostic_api.llm import ScriptedLLM, text, tool_use
from services.diagnostic_api.sessions import SessionStore
from services.diagnostic_api.state import MAX_CHECKS, SAMPLE, SessionState, diff_checks
from services.diagnostic_api.tools import TOOLS, execute_tool
from shared import db

try:
    db.connect().close()
    HAVE_PG = True
except Exception:
    HAVE_PG = False
pg = pytest.mark.skipif(not HAVE_PG, reason="Postgres not reachable (docker compose up -d postgres)")

ALICE = {"X-User": "alice"}
ANALYST = {"X-User": "ana", "X-Role": "analyst"}
PROMO = "NOT_EFFECTIVE:PROMOTION"
BLOCK = "Open findings for this week (each was confirmed by a person"       # the block's header, not the prompt's rule


# ======================= diffs between two diagnoses (pure) =======================
def test_diff_classifies_every_record():
    before = {"1|1|VIC": PROMO, "2|1|VIC": PROMO, "3|1|NSW": "MISSING_PRICE:MISSING_RULE", "4|None|VIC": "NOT_IN_LIST:NOT_ON_LIST"}
    now = {"2|1|VIC": PROMO, "3|1|NSW": "API_REJECTED:VALIDITY_OVERLAP", "5|6|QLD": "RULE_VIOLATION:BELOW_FLOOR"}
    d = diff_checks(before, now)
    assert (d["resolved"], d["new_failures"], d["changed"], d["still_failing"]) == (2, 1, 1, 1)
    assert d["examples"]["resolved"] == [{"sku": "1", "pack_qty": 1, "region": "VIC", "was": PROMO},
                                         {"sku": "4", "pack_qty": None, "region": "VIC", "was": "NOT_IN_LIST:NOT_ON_LIST"}]
    assert d["examples"]["new_failures"] == [{"sku": "5", "pack_qty": 6, "region": "QLD", "now": "RULE_VIOLATION:BELOW_FLOOR"}]
    assert d["examples"]["changed"] == [{"sku": "3", "pack_qty": 1, "region": "NSW", "was": "MISSING_PRICE:MISSING_RULE",
                                         "now": "API_REJECTED:VALIDITY_OVERLAP"}]


def test_diff_examples_are_capped_but_counts_are_not():
    before = {f"{i}|1|VIC": PROMO for i in range(SAMPLE + 4)}
    d = diff_checks(before, {})
    assert d["resolved"] == SAMPLE + 4 and len(d["examples"]["resolved"]) == SAMPLE


def test_the_first_check_of_a_scope_has_no_diff_and_the_second_does():
    st = SessionState()
    scope = st.scope_id(WEEK, ["2", "1"])
    assert scope == st.scope_id(WEEK, ["1", "2", "2"])                             # order and repeats do not matter
    assert scope != st.scope_id(WEEK, ["1"]) and scope != st.scope_id("2026-W40", ["1", "2"])
    assert st.record_check(scope, "t1", "v1", ["VIC"], {"1|1|VIC": PROMO}) is None
    d = st.record_check(scope, "t2", "v2", ["VIC"], {})
    assert d["previous_as_of"] == "t1" and d["previous_data_version"] == "v1" and d["resolved"] == 1
    assert d["headline"] == "Since the last check (t1), in VIC: 1 resolved, 0 new, 0 changed, 0 still failing."
    quiet = st.record_check(scope, "t3", "v3", ["VIC"], {})
    assert quiet["headline"] == ("Since the last check (t2), in VIC: 0 resolved, 0 new, 0 changed, 0 still failing. "
                                 "Nothing has changed.")
    mixed = st.record_check(scope, "t4", "v4", ["VIC"], {"9|1|VIC": PROMO})
    assert "1 new" in mixed["headline"] and "Nothing has changed" not in mixed["headline"]


def test_a_diff_compares_only_the_regions_both_checks_covered():
    st, scope = SessionState(), SessionState.scope_id(WEEK, ["1", "2"])
    st.record_check(scope, "t1", "v1", ["NSW", "VIC"], {"1|1|VIC": PROMO, "2|1|NSW": "MISSING_PRICE:MISSING_RULE"})
    # the model now names other regions: QLD is new, NSW is gone, VIC is common. Only VIC is compared
    d = st.record_check(scope, "t2", "v2", ["QLD", "VIC"], {"1|1|VIC": PROMO, "3|1|QLD": PROMO})
    assert d["regions_compared"] == ["VIC"] and (d["resolved"], d["new_failures"], d["still_failing"]) == (0, 0, 1)
    assert d["headline"].startswith("Since the last check (t1), in VIC:")
    assert st.record_check(scope, "t3", "v3", ["NSW"], {}) is None                 # no region in common with the last check
    assert st.record_check(scope, "t4", "v4", ["NSW"], {})["regions_compared"] == ["NSW"]   # and it is compared from then on


def test_only_the_most_recent_scopes_are_remembered_and_they_survive_json():
    st = SessionState()
    for i in range(MAX_CHECKS + 2):
        st.record_check(f"scope{i}", f"2026-09-21T09:0{i}:00", None, ["VIC"], {})
    assert sorted(st.last_checks) == [f"scope{i}" for i in range(2, MAX_CHECKS + 2)]
    again = SessionState(json.loads(json.dumps(st.to_json())))
    assert again.last_checks == st.last_checks


# ======================= fixtures =======================
class Runs:
    """The pipeline run the service sees. Change `stamp` to simulate a new run finishing."""

    def __init__(self, run_id="run-1", stamp="10:00"):
        self.run_id, self.stamp = run_id, stamp

    def latest(self, week):
        return None if self.run_id is None else {"run_id": self.run_id, "status": "SUCCEEDED", "finished_at": None,
                                                 "data_version": f"{self.run_id}@{self.stamp}"}


def live_sap(env):
    """A read-only SAP whose answers can change: the fixture scenario, plus counters and `end_promotions()`."""
    box = SimpleNamespace(conditions=copy.deepcopy(env["sap"].get("/pricing/conditions", params={"week": WEEK}).json()["items"]),
                          submissions=env["sap"].get("/pricing/submissions", params={"week": WEEK}).json()["items"], gets=[])

    def handler(req):
        if req.method != "GET":
            return httpx.Response(403, text="write key required")
        box.gets.append(req.url.path)
        items = {"/pricing/conditions": box.conditions, "/pricing/submissions": box.submissions}[req.url.path]
        return httpx.Response(200, json={"week": WEEK, "count": len(items), "items": items})

    def end_promotions():
        for c in box.conditions:
            c["effective_price"], c["effective_source"] = c["price"], "MARKDOWN"
    box.client, box.end_promotions = httpx.Client(base_url="http://sap", transport=httpx.MockTransport(handler)), end_promotions
    return box


@pytest.fixture
def rig(env, monkeypatch):
    monkeypatch.setenv("DEFAULT_WEEK", WEEK)
    monkeypatch.delenv("USER_REGIONS", raising=False)
    with db.connect() as c:
        c.execute("DROP SCHEMA IF EXISTS diagnostic_test CASCADE")
    SessionStore().ensure_schema()
    clock = [datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)]
    now = lambda: clock[0]                                                        # noqa: E731
    r = SimpleNamespace(env=env, clock=clock, box=live_sap(env), runs=Runs(), monkeypatch=monkeypatch, steps=[], llms=[],
                        cache=ToolCache(clock=now), findings=FindingsStore(clock=now), now=now)

    def mk(user="alice", allowed=None, state=True):
        return dataclasses.replace(env["ctx"], user=user, sap=r.box.client, allowed_regions=allowed, runs=r.runs,
                                   cache=r.cache, findings=r.findings, state=SessionState() if state else None)
    r.mk = mk
    return r


def call(ctx, name, **args):
    result, is_error = execute_tool(name, args, ctx)
    assert not is_error, result
    return result


def cache_rows():
    with db.connect() as c:
        return c.execute("SELECT tool, expires_at FROM diagnostic_test.tool_cache").fetchall()


def promo_skus(env):
    return sorted({k[0] for k in env["promo"]})


# ======================= the tool cache =======================
@pg
def test_live_results_are_reused_until_the_ttl_then_read_again(rig):
    ctx = rig.mk(state=False)
    first = call(ctx, "diagnose_batch", week=WEEK, skus=rig.env["skus"])
    second = call(ctx, "diagnose_batch", week=WEEK, skus=rig.env["skus"])
    assert "cached" not in first and second.pop("cached") is True and second == first    # same answer, original as_of
    assert rig.box.gets == ["/pricing/conditions"]                                       # SAP was asked once
    rig.clock[0] += timedelta(seconds=299)
    assert call(ctx, "diagnose_batch", week=WEEK, skus=rig.env["skus"])["cached"] is True
    rig.clock[0] += timedelta(seconds=2)                                                 # past the 5 minute TTL
    third = call(ctx, "diagnose_batch", week=WEEK, skus=rig.env["skus"])
    assert "cached" not in third and rig.box.gets == ["/pricing/conditions"] * 2


@pg
def test_snapshot_results_never_expire_but_follow_the_data_version(rig):
    ctx = rig.mk(state=False)
    call(ctx, "check_rules", week=WEEK, skus=rig.env["skus"])
    call(ctx, "get_api_log", week=WEEK, skus=rig.env["skus"])
    assert [r["expires_at"] for r in cache_rows()] == [None, None]                       # no TTL
    rig.clock[0] += timedelta(days=1)
    assert call(ctx, "check_rules", week=WEEK, skus=rig.env["skus"])["cached"] is True
    assert call(ctx, "get_api_log", week=WEEK, skus=rig.env["skus"])["cached"] is True
    assert rig.box.gets == ["/pricing/submissions"]                                      # the log was read once
    rig.runs.stamp = "11:30"                                                             # a new pipeline run finished
    assert "cached" not in call(ctx, "check_rules", week=WEEK, skus=rig.env["skus"])
    assert "cached" not in call(ctx, "get_api_log", week=WEEK, skus=rig.env["skus"])
    assert rig.box.gets == ["/pricing/submissions"] * 2


@pg
def test_a_different_run_or_argument_is_a_different_entry(rig):
    ctx = rig.mk(state=False)
    call(ctx, "check_rules", week=WEEK, skus=rig.env["skus"])
    assert "cached" not in call(ctx, "check_rules", week=WEEK, skus=rig.env["skus"][:5])          # other skus
    assert "cached" not in call(ctx, "check_rules", week=WEEK, skus=rig.env["skus"], regions=["VIC"])   # other regions
    rig.runs.run_id = "run-2"
    assert "cached" not in call(ctx, "check_rules", week=WEEK, skus=rig.env["skus"])            # other run
    assert call(rig.mk(state=False), "check_rules", week=WEEK, skus=list(reversed(rig.env["skus"])))["cached"] is True


@pg
def test_users_with_the_same_access_share_entries_and_narrower_access_is_never_served_wider_results(rig):
    call(rig.mk("alice"), "diagnose_batch", week=WEEK, skus=rig.env["skus"])
    assert call(rig.mk("bob"), "diagnose_batch", week=WEEK, skus=rig.env["skus"])["cached"] is True   # same access: shared
    nsw = rig.mk("nsw-analyst", allowed=frozenset({"NSW"}))
    narrow = call(nsw, "diagnose_batch", week=WEEK, skus=rig.env["skus"])
    assert "cached" not in narrow and narrow["regions"] == ["NSW"] and set(narrow["by_region"]) == {"NSW"}
    assert call(rig.mk("nsw-2", allowed=frozenset({"NSW"})), "diagnose_batch", week=WEEK, skus=rig.env["skus"])["cached"]
    result, is_error = execute_tool("diagnose_batch", {"week": WEEK, "skus": rig.env["skus"], "regions": ["VIC"]}, nsw)
    assert is_error and "not available" in result["error"]                               # VIC is cached for alice, not for nsw


@pg
def test_a_group_and_the_same_explicit_list_share_an_entry(rig):
    ctx = rig.mk()
    group = call(ctx, "resolve_products", week=WEEK, brand=next(c["brand"] for c in rig.env["wh"].get_candidates(WEEK)
                                                                  if c["name"] is not None))
    call(ctx, "diagnose_batch", week=WEEK, group_id=group["group_id"])
    again = call(rig.mk(state=False), "diagnose_batch", week=WEEK, skus=group["skus"], regions=group["regions"])
    assert again["cached"] is True


@pg
def test_errors_are_not_cached_and_neither_is_anything_without_a_data_version(rig):
    ctx = rig.mk(state=False)
    for _ in range(2):
        result, is_error = execute_tool("diagnose_batch", {"week": WEEK, "skus": []}, ctx)
        assert is_error and "cached" not in result
    assert cache_rows() == []
    rig.runs.run_id = None                                                              # no pipeline state at all
    call(ctx, "check_rules", week=WEEK, skus=rig.env["skus"])
    assert "cached" not in call(ctx, "check_rules", week=WEEK, skus=rig.env["skus"])    # a snapshot with no version is not kept
    assert cache_rows() == []


@pg
def test_resolve_products_is_never_cached_so_every_session_gets_its_group(rig):
    for _ in range(2):
        ctx = rig.mk()
        r = call(ctx, "resolve_products", week=WEEK)
        assert r["group_id"] == "G1" and "cached" not in r
    assert cache_rows() == []


@pg
def test_a_broken_cache_never_fails_a_tool_call(rig):
    class Down(ToolCache):
        def get(self, key):
            raise RuntimeError("cache is down")

        def save(self, *a, **k):
            raise RuntimeError("cache is down")
    ctx = dataclasses.replace(rig.mk(state=False), cache=Down())
    assert call(ctx, "check_rules", week=WEEK, skus=rig.env["skus"])["priced_checked"] == 126


@pg
def test_private_bookkeeping_never_reaches_the_model(rig):
    ctx = rig.mk()
    for _ in range(2):                                                                  # a miss, then a hit
        assert "_private" not in json.dumps(call(ctx, "diagnose_batch", week=WEEK, skus=rig.env["skus"]))


@pg
def test_old_entries_are_purged_when_new_ones_are_written(rig):
    ctx = rig.mk(state=False)
    call(ctx, "diagnose_batch", week=WEEK, skus=rig.env["skus"])
    call(ctx, "check_rules", week=WEEK, skus=rig.env["skus"])
    rig.clock[0] += timedelta(days=8)
    call(ctx, "check_rules", week=WEEK, skus=rig.env["skus"][:3])                       # a write triggers the purge
    assert [r["tool"] for r in cache_rows()] == ["check_rules"]                          # only the new entry is left


# ======================= repeated questions in a session =======================
@pg
def test_a_repeated_diagnosis_says_what_changed_since_the_last_one(rig):
    ctx = rig.mk()
    group = call(ctx, "resolve_products", week=WEEK, brand=next(c["brand"] for c in rig.env["wh"].get_candidates(WEEK)
                                                                 if c["sku"] == promo_skus(rig.env)[0] and c["name"] is not None))
    first = call(ctx, "diagnose_batch", week=WEEK, group_id=group["group_id"])
    assert "changes_since_last_check" not in first
    n = next(p["count"] for p in first["patterns"] if p["code"] == "NOT_EFFECTIVE")
    rig.box.end_promotions()                                                            # the promotions end in SAP
    rig.clock[0] += timedelta(minutes=6)                                                # long enough to see it
    second = call(ctx, "diagnose_batch", week=WEEK, group_id=group["group_id"])
    changes = second["changes_since_last_check"]
    assert changes["previous_as_of"] == first["as_of"] and changes["resolved"] == n and changes["new_failures"] == 0
    assert changes["headline"].startswith(f"Since the last check ({first['as_of']}), in NSW/QLD/VIC: {n} resolved, 0 new, 0 changed,")
    assert changes["still_failing"] == sum(p["count"] for p in first["patterns"]) - n
    assert {e["was"] for e in changes["examples"]["resolved"]} == {PROMO}
    assert "NOT_EFFECTIVE" not in second["totals"]
    third = call(ctx, "diagnose_batch", week=WEEK, group_id=group["group_id"])           # asked again straight away
    assert third["cached"] is True and third["changes_since_last_check"]["resolved"] == 0   # nothing changed since
    assert third["changes_since_last_check"]["headline"].endswith("Nothing has changed.")


@pg
def test_the_diff_survives_the_model_naming_regions_differently_but_not_other_skus(rig):
    ctx = rig.mk()
    call(ctx, "diagnose_batch", week=WEEK, skus=rig.env["skus"], regions=["NSW", "VIC"])
    again = call(ctx, "diagnose_batch", week=WEEK, skus=rig.env["skus"])                           # all regions this time
    assert again["changes_since_last_check"]["regions_compared"] == ["NSW", "VIC"]
    assert again["changes_since_last_check"]["resolved"] == 0 and again["changes_since_last_check"]["still_failing"] > 0
    only_vic = call(ctx, "diagnose_batch", week=WEEK, skus=rig.env["skus"], regions=["VIC"])
    assert only_vic["changes_since_last_check"]["regions_compared"] == ["VIC"]
    assert "changes_since_last_check" not in call(ctx, "diagnose_batch", week=WEEK, skus=rig.env["skus"][:5])   # other skus


@pg
def test_no_session_means_no_diff(rig):
    bare = rig.mk(state=False)
    call(bare, "diagnose_batch", week=WEEK, skus=rig.env["skus"])
    assert "changes_since_last_check" not in call(bare, "diagnose_batch", week=WEEK, skus=rig.env["skus"])


# ======================= findings =======================
def finding(rig, **over):
    base = dict(week=WEEK, region="VIC", error_code="NOT_EFFECTIVE", reason="PROMOTION", skus=promo_skus(rig.env),
                root_cause="VIC-only catalogue promotion overrides the markdown", confirmed_by="ana",
                brand="Larkspur", owner="Promotions", ticket="PRC-1")
    return rig.findings.create(**(base | over))


@pg
def test_findings_can_be_created_listed_filtered_and_resolved(rig):
    a, b = finding(rig), finding(rig, region="NSW", error_code="MISSING_PRICE", reason="MISSING_RULE", brand=None)
    assert a["status"] == "open" and a["skus"] == promo_skus(rig.env) and a["confirmed_by"] == "ana"
    assert [f["finding_id"] for f in rig.findings.list()] == [b["finding_id"], a["finding_id"]]      # newest first
    assert [f["finding_id"] for f in rig.findings.list(region="NSW")] == [b["finding_id"]]
    assert [f["finding_id"] for f in rig.findings.list(error_code="NOT_EFFECTIVE", brand="Larkspur")] == [a["finding_id"]]
    assert rig.findings.list(week="2026-W40") == []
    assert [f["finding_id"] for f in rig.findings.list(allowed_regions=frozenset({"NSW"}))] == [b["finding_id"]]
    done = rig.findings.resolve(a["finding_id"], "ana")
    assert done["status"] == "resolved" and done["resolved_by"] == "ana" and done["resolved_at"] is not None
    assert rig.findings.resolve(a["finding_id"], "ana") is None and rig.findings.resolve(9999, "ana") is None
    assert [f["finding_id"] for f in rig.findings.list()] == [b["finding_id"]]                        # open only, by default
    assert len(rig.findings.list(status=None)) == 2


@pg
def test_a_finding_closes_only_when_a_check_covering_it_finds_no_such_failure(rig):
    skus = promo_skus(rig.env)
    still = {f"{skus[0]}|1|VIC": PROMO}
    f = finding(rig)
    assert rig.findings.auto_resolve(WEEK, ["VIC"], skus, still) == []                                # still failing
    assert rig.findings.auto_resolve(WEEK, ["NSW"], skus, {}) == []                                   # region not checked
    assert rig.findings.auto_resolve(WEEK, ["VIC"], skus[:-1], {}) == []                              # only part of its skus
    assert rig.findings.auto_resolve("2026-W40", ["VIC"], skus, {}) == []                             # another week
    assert rig.findings.auto_resolve(WEEK, ["VIC"], skus, {f"{skus[0]}|1|NSW": PROMO}) == [f["finding_id"]]   # other region's failure is not its own
    closed = rig.findings.get(f["finding_id"])
    assert closed["status"] == "resolved" and closed["resolved_by"] == "auto"


@pg
def test_a_different_failure_on_the_same_records_does_not_keep_a_finding_open(rig):
    skus = promo_skus(rig.env)
    f = finding(rig)
    assert rig.findings.auto_resolve(WEEK, ["VIC"], skus, {f"{skus[0]}|1|VIC": "API_REJECTED:VALIDITY_OVERLAP"}) == [f["finding_id"]]


@pg
def test_get_findings_shows_open_issues_the_user_may_see(rig):
    a, b = finding(rig), finding(rig, region="NSW", brand=None, owner=None, ticket=None, skus=[str(i) for i in range(9)])
    got = call(rig.mk(), "get_findings", week=WEEK)
    assert got["count"] == 2 and "not by a tool" in got["note"]
    one = next(f for f in got["findings"] if f["finding_id"] == a["finding_id"])
    assert one["owning_team"] == "Promotions" and one["ticket"] == "PRC-1" and one["confirmed_by"] == "ana"
    assert "owner" not in one                                                                          # no bare "owner" to misread
    assert set(got["fields"]) == {"owning_team", "confirmed_by", "ticket"}
    assert "not the owning team" in got["fields"]["confirmed_by"]
    assert one["root_cause"].startswith("VIC-only") and one["sku_count"] == len(promo_skus(rig.env))
    big = next(f for f in got["findings"] if f["finding_id"] == b["finding_id"])
    assert big["sku_count"] == 9 and len(big["sample_skus"]) == SAMPLE                                 # short, not the whole list
    nsw = call(rig.mk(allowed=frozenset({"NSW"})), "get_findings", week=WEEK)
    assert [f["region"] for f in nsw["findings"]] == ["NSW"]                                          # VIC is not theirs to see
    assert [f["finding_id"] for f in call(rig.mk(), "get_findings", week=WEEK, region="VIC")["findings"]] == [a["finding_id"]]
    rig.findings.resolve(a["finding_id"], "ana")
    assert call(rig.mk(), "get_findings", week=WEEK)["count"] == 1                                    # resolved ones are hidden
    assert call(rig.mk(), "get_findings", week="2026-W40")["count"] == 0


@pg
def test_without_a_findings_store_get_findings_is_simply_empty(rig):
    ctx = dataclasses.replace(rig.mk(), findings=None)
    assert call(ctx, "get_findings", week=WEEK)["findings"] == []


def test_the_model_has_no_way_to_write_findings():
    assert not {"create_finding", "resolve_finding", "update_finding", "delete_finding"} & set(TOOLS)
    source = inspect.getsource(tools)
    assert ".create(" not in source and ".resolve(" not in source                                      # only reads and auto_resolve
    ctx = SimpleNamespace(user="x", findings=None, cache=None)
    for name in ("create_finding", "resolve_finding"):
        result, is_error = execute_tool(name, {"week": WEEK}, ctx)
        assert is_error and "unknown tool" in result["error"]


@pg
def test_diagnosing_closes_findings_whose_failures_are_gone(rig):
    skus = promo_skus(rig.env)
    ctx = rig.mk()
    f = finding(rig)
    r = call(ctx, "diagnose_batch", week=WEEK, skus=skus, regions=["VIC"])
    assert "findings_resolved" not in r and rig.findings.get(f["finding_id"])["status"] == "open"     # promotions still on
    call(ctx, "diagnose_batch", week=WEEK, skus=rig.env["skus"][:2])                                  # a check not covering it
    rig.box.end_promotions()
    rig.clock[0] += timedelta(minutes=6)
    r = call(ctx, "diagnose_batch", week=WEEK, skus=skus, regions=["VIC"])
    assert r["findings_resolved"] == [f["finding_id"]]
    assert rig.findings.get(f["finding_id"])["resolved_by"] == "auto"
    assert "findings_resolved" not in call(ctx, "diagnose_batch", week=WEEK, skus=skus, regions=["VIC"])   # nothing left to close


# ======================= the HTTP API =======================
@pytest.fixture
def client(rig):
    from fastapi.testclient import TestClient
    def llm_factory(week):
        llm = ScriptedLLM(rig.steps)
        rig.llms.append(llm)
        return llm
    app = create_app(SessionStore(clock=rig.now), llm_factory,
                     lambda user, regions: rig.mk(user, regions, state=False), rig.runs, rig.cache, rig.findings)
    with TestClient(app) as c:
        yield c


def body(rig, **over):
    return {"week": WEEK, "region": "VIC", "error_code": "NOT_EFFECTIVE", "reason": "PROMOTION",
            "skus": promo_skus(rig.env), "root_cause": "catalogue promotion", "owner": "Promotions", "ticket": "PRC-7"} | over


@pg
def test_only_analysts_write_findings_and_the_writer_is_taken_from_the_header(rig, client):
    assert client.post("/findings", json=body(rig)).status_code == 401                                 # no identity
    assert client.post("/findings", json=body(rig), headers=ALICE).status_code == 403                  # not an analyst
    assert client.post("/findings", json=body(rig), headers={"X-Role": "analyst"}).status_code == 401  # role alone is not identity
    r = client.post("/findings", json=body(rig) | {"confirmed_by": "mallory"}, headers=ANALYST)
    assert r.status_code == 201 and r.json()["confirmed_by"] == "ana" and r.json()["status"] == "open"
    assert client.get("/findings", headers=ALICE).json()[0]["ticket"] == "PRC-7"                       # anyone may read
    assert client.get("/findings").status_code == 401


@pg
def test_finding_input_is_validated(rig, client):
    for bad in ({"skus": []}, {"week": "W39"}, {"root_cause": ""}, {"region": ""}, {"skus": ["1"] * 501}):
        assert client.post("/findings", json=body(rig) | bad, headers=ANALYST).status_code == 422, bad
    missing = body(rig)
    del missing["skus"]
    assert client.post("/findings", json=missing, headers=ANALYST).status_code == 422


@pg
def test_findings_respect_the_users_regions(rig, client):
    rig.monkeypatch.setenv("USER_REGIONS", '{"nsw-analyst": ["NSW"]}')
    nsw = {"X-User": "nsw-analyst", "X-Role": "analyst"}
    assert client.post("/findings", json=body(rig), headers=nsw).status_code == 403                    # VIC is not theirs
    assert client.post("/findings", json=body(rig, region="NSW", error_code="MISSING_PRICE", reason="MISSING_RULE"),
                       headers=nsw).status_code == 201
    vic = client.post("/findings", json=body(rig), headers=ANALYST).json()                             # ana sees every region
    assert [f["region"] for f in client.get("/findings", headers=nsw).json()] == ["NSW"]
    assert {f["region"] for f in client.get("/findings", headers=ALICE).json()} == {"VIC", "NSW"}
    assert client.post(f"/findings/{vic['finding_id']}/resolve", headers=nsw).status_code == 404       # cannot even see it


@pg
def test_resolving_a_finding(rig, client):
    fid = client.post("/findings", json=body(rig), headers=ANALYST).json()["finding_id"]
    assert client.post(f"/findings/{fid}/resolve", headers=ALICE).status_code == 403
    done = client.post(f"/findings/{fid}/resolve", headers=ANALYST)
    assert done.status_code == 200 and done.json()["status"] == "resolved" and done.json()["resolved_by"] == "ana"
    assert client.post(f"/findings/{fid}/resolve", headers=ANALYST).status_code == 409
    assert client.post("/findings/9999/resolve", headers=ANALYST).status_code == 404
    assert client.get("/findings", headers=ALICE).json() == []
    assert [f["status"] for f in client.get("/findings?status=all", headers=ALICE).json()] == ["resolved"]


@pg
def test_a_turn_cannot_create_or_close_findings(rig, client):
    sid = client.post("/sessions", headers=ALICE).json()["session_id"]
    fid = client.post("/findings", json=body(rig), headers=ANALYST).json()["finding_id"]
    rig.steps = [[tool_use("create_finding", body(rig)), tool_use("resolve_finding", {"finding_id": fid})], [text("done")]]
    out = client.post(f"/sessions/{sid}/messages", json={"text": "record and close a finding"}, headers=ALICE).json()
    assert [c["is_error"] for c in out["tool_calls"]] == [True, True]
    assert [f["finding_id"] for f in client.get("/findings", headers=ALICE).json()] == [fid]           # untouched


@pg
def test_code_written_sentences_lead_the_answer_and_the_history_keeps_the_models_text(rig, client):
    sid = client.post("/sessions", headers=ALICE).json()["session_id"]

    def ask(*steps):
        rig.steps = list(steps)
        r = client.post(f"/sessions/{sid}/messages", json={"text": "how are they?"}, headers=ALICE)
        assert r.status_code == 200, r.text
        return r.json()

    def db_one(sql):
        with db.connect() as c:
            return c.execute(sql, (sid,)).fetchone()

    first = ask([tool_use("resolve_products", {"week": WEEK})], [tool_use("diagnose_batch", {"week": WEEK, "group_id": "G1"})],
                [text("First look.")])
    assert first["answer"] == "First look."                                                 # nothing to compare with yet
    second = ask([tool_use("diagnose_batch", {"week": WEEK, "group_id": "G1"})], [text("Same as before.")])
    head, _, rest = second["answer"].partition("\n\n")
    assert head.startswith("Since the last check (") and head.endswith("still failing. Nothing has changed.")
    assert rest == "Same as before."                                                        # the model's words follow
    turn2 = db_one("SELECT answer FROM diagnostic_test.turn_transcripts WHERE session_id = %s AND turn_no = 2")
    assert turn2["answer"] == second["answer"]                                              # the audit log has what the user saw
    history = db_one("SELECT history FROM diagnostic_test.sessions WHERE session_id = %s")["history"]
    assert history[-1]["content"] == [text("Same as before.")]                             # the model's context stays its own text
    failed = ask([tool_use("diagnose_batch", {"week": WEEK, "group_id": "G9"})], [text("Cannot.")])
    assert failed["answer"] == "Cannot." and failed["tool_calls"][0]["is_error"]            # a failed call adds no sentence


# ======================= the section 3.3 investigation, end to end =======================
def tool_results(sid, turn):
    with db.connect() as c:
        row = c.execute("SELECT messages FROM diagnostic_test.turn_transcripts WHERE session_id = %s AND turn_no = %s",
                        (sid, turn)).fetchone()
    return [json.loads(b["content"]) for m in row["messages"] if m["role"] == "user" and isinstance(m["content"], list)
            for b in m["content"] if b["type"] == "tool_result"]


@pg
def test_the_section_3_3_investigation_end_to_end(rig, client):
    brand = next(c["brand"] for c in rig.env["wh"].get_candidates(WEEK)
                 if c["sku"] == promo_skus(rig.env)[0] and c["name"] is not None)
    sid = client.post("/sessions", headers=ALICE).json()["session_id"]

    def ask(question, *steps):
        rig.steps = list(steps)
        r = client.post(f"/sessions/{sid}/messages", json={"text": question}, headers=ALICE)
        assert r.status_code == 200, r.text
        assert not any(c["is_error"] for c in r.json()["tool_calls"])
        return r.json()

    # 1. nothing is known yet, so the assistant investigates: brand -> skus -> verdicts
    assert not rig.llms                                                                                # nothing asked yet
    t1 = ask("VIC prices did not drop, NSW is fine. Why?",
             [tool_use("get_findings", {"week": WEEK})], [tool_use("resolve_products", {"week": WEEK, "brand": brand})],
             [tool_use("diagnose_batch", {"week": WEEK, "group_id": "G1"})], [text("A VIC promotion overrides the markdown.")])
    findings, resolved, diagnosis = tool_results(sid, 1)
    assert findings["count"] == 0 and resolved["group_id"] == "G1"
    vic = next(p for p in diagnosis["patterns"] if p["code"] == "NOT_EFFECTIVE")
    assert vic["region"] == "VIC" and vic["reason"] == "PROMOTION" and "changes_since_last_check" not in diagnosis
    assert t1["answer"] == "A VIC promotion overrides the markdown."
    assert BLOCK not in rig.llms[0].calls[0]["system"]                                                 # none recorded yet

    # 2. a person confirms the cause; the model cannot do this
    made = client.post("/findings", json=body(rig, skus=vic["sample_skus"], brand=brand, owner="Promotions team",
                                              ticket="PRC-42"), headers=ANALYST)
    assert made.status_code == 201
    fid = made.json()["finding_id"]

    # 3. the next question finds the known issue first
    ask("Is this a known issue?", [tool_use("get_findings", {"week": WEEK, "region": "VIC"})], [text("Yes, PRC-42.")])
    prompt = rig.llms[-1].calls[0]["system"]
    assert BLOCK in prompt and "PRC-42" in prompt and "owning team Promotions team" in prompt
    known = tool_results(sid, 2)[0]["findings"][0]
    assert (known["finding_id"], known["owning_team"], known["ticket"]) == (fid, "Promotions team", "PRC-42")

    # 4. "if we end the promotion, would they go below the floor?" is answered by the rules, not the model
    ask("If we end the promotion, would they go below the floor?",
        [tool_use("check_rules", {"week": WEEK, "group_id": "G1"})], [text("No rule would be broken.")])
    rules = tool_results(sid, 3)[0]
    assert rules["violations"] == 0 and rules["priced_checked"] > 0

    # 5. the promotion ends; asking again reports what changed and closes the finding
    rig.box.end_promotions()
    rig.clock[0] += timedelta(minutes=6)
    t4 = ask("Check again.", [tool_use("diagnose_batch", {"week": WEEK, "group_id": "G1"})], [text("Fixed.")])
    again = tool_results(sid, 4)[0]
    assert t4["answer"] == (again["changes_since_last_check"]["headline"] + "\n"
                            f"Findings closed automatically because their problem is gone: #{fid}.\n\nFixed.")   # code first, then the model
    assert again["changes_since_last_check"]["resolved"] == vic["count"]
    assert again["changes_since_last_check"]["new_failures"] == 0 and "NOT_EFFECTIVE" not in again["totals"]
    assert again["findings_resolved"] == [fid]
    assert client.get("/findings", headers=ALICE).json() == []
    closed = client.get("/findings?status=all", headers=ALICE).json()[0]
    assert closed["status"] == "resolved" and closed["resolved_by"] == "auto"


# ======================= open findings are put in front of the model by the service =======================
def fake_row(i=1, **over):
    return {"finding_id": i, "region": "VIC", "error_code": "NOT_EFFECTIVE", "reason": "PROMOTION", "brand": "Larkspur",
            "skus": ["1", "2", "3", "4", "5"], "root_cause": "a VIC promotion overrides the markdown", "owner": "Promotions team",
            "ticket": "PRC-42", "confirmed_by": "ana"} | over


def test_findings_render_as_a_short_block():
    assert render_block([]) == ""
    block = render_block([fake_row()])
    assert block.splitlines()[0].startswith("Open findings for this week (each was confirmed by a person; verify")
    assert ("- #1 VIC NOT_EFFECTIVE:PROMOTION, 5 skus (1, 2, 3 and 2 more), brand Larkspur: a VIC promotion overrides the "
            "markdown [owning team Promotions team, ticket PRC-42] [confirmed by ana]") in block


def test_findings_block_copes_with_missing_fields_long_text_and_many_findings():
    bare = render_block([fake_row(reason="", brand=None, owner=None, ticket=None, skus=["9"], root_cause="x" * 300)])
    line = bare.splitlines()[1]
    assert line.startswith("- #1 VIC NOT_EFFECTIVE, 1 skus (9): xxx") and line.count("x") == 197 and "..." in line
    assert "owning team" not in line and "ticket" not in line and line.endswith("[confirmed by ana]")
    many = render_block([fake_row(i) for i in range(1, 9)])
    lines = many.splitlines()                                                                          # header + 5 items + a note
    assert len(lines) == 1 + 5 + 1 and lines[-1] == "- ... and 3 more; call get_findings to see them"


@pg
def test_open_findings_reach_the_model_without_it_asking(rig, client):
    open_one = client.post("/findings", json=body(rig, ticket="PRC-7"), headers=ANALYST).json()
    closed = client.post("/findings", json=body(rig, ticket="PRC-8", error_code="MISSING_PRICE"), headers=ANALYST).json()
    client.post(f"/findings/{closed['finding_id']}/resolve", headers=ANALYST)
    client.post("/findings", json=body(rig, week="2026-W38", ticket="PRC-OLD"), headers=ANALYST)      # another week
    sid = client.post("/sessions", headers=ALICE).json()["session_id"]
    rig.steps = [[text("ok")]]
    client.post(f"/sessions/{sid}/messages", json={"text": "hi"}, headers=ALICE)
    prompt = rig.llms[-1].calls[0]["system"]
    assert BLOCK in prompt and f"#{open_one['finding_id']}" in prompt and "PRC-7" in prompt
    assert "PRC-8" not in prompt and "PRC-OLD" not in prompt                                          # resolved / other week: hidden


@pg
def test_the_findings_in_the_prompt_respect_the_users_regions(rig, client):
    client.post("/findings", json=body(rig, region="VIC", ticket="PRC-VIC"), headers=ANALYST)
    client.post("/findings", json=body(rig, region="NSW", error_code="MISSING_PRICE", reason="MISSING_RULE", ticket="PRC-NSW"),
                headers=ANALYST)
    rig.monkeypatch.setenv("USER_REGIONS", '{"nsw-analyst": ["NSW"]}')
    nsw = {"X-User": "nsw-analyst"}
    sid = client.post("/sessions", headers=nsw).json()["session_id"]
    rig.steps = [[text("ok")]]
    client.post(f"/sessions/{sid}/messages", json={"text": "hi"}, headers=nsw)
    prompt = rig.llms[-1].calls[0]["system"]
    assert "PRC-NSW" in prompt and "PRC-VIC" not in prompt                                            # VIC is not theirs to see
    sid = client.post("/sessions", headers=ALICE).json()["session_id"]
    client.post(f"/sessions/{sid}/messages", json={"text": "hi"}, headers=ALICE)
    assert "PRC-NSW" in rig.llms[-1].calls[0]["system"] and "PRC-VIC" in rig.llms[-1].calls[0]["system"]
