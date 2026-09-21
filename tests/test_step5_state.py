"""Step 5, slice 3b: session state (group handles), history compaction, run resolution.

Everything here needs no key and no SAP; only the RunResolver tests need Postgres
(docker compose up -d postgres, throwaway schema pipeline_test). The API-level flows are in test_step5_sessions.
"""
import copy
import dataclasses
import json
import os
import shutil
from datetime import datetime, timedelta, timezone

import duckdb
import httpx
import pytest

os.environ["SAP_SCHEMA"], os.environ["PIPELINE_SCHEMA"] = "sap_test", "pipeline_test"

from jobs.data_gen.generate import WEEK
from services.diagnostic_api.llm import text, tool_use
from services.diagnostic_api.runs import RunResolver
from services.diagnostic_api.state import MAX_EARLIER, MAX_GROUPS, SessionState, compact_history
from services.diagnostic_api.tools import ToolContext, execute_tool
from shared import db
from shared.pipeline_schema import ddl
from shared.warehouse import DuckDBWarehouse

try:
    db.connect().close()
    HAVE_PG = True
except Exception:
    HAVE_PG = False
pg = pytest.mark.skipif(not HAVE_PG, reason="Postgres not reachable (docker compose up -d postgres)")


# ======================= session state =======================
def test_groups_get_handles_and_the_same_list_reuses_its_handle():
    st = SessionState()
    assert st.add_group(["2", "1"], "brand X", WEEK, ["VIC", "NSW"], "t1", "v1") == "G1"
    assert st.add_group(["1", "2"], "brand X", WEEK, ["NSW", "VIC"], "t2", "v2") == "G1"   # same list: same handle, refreshed
    g = st.get_group("G1")
    assert g["skus"] == ["1", "2"] and g["regions"] == ["NSW", "VIC"] and g["as_of"] == "t2" and g["data_version"] == "v2"
    assert st.add_group(["3"], "other", WEEK, ["VIC"], "t3", None) == "G2"
    assert st.add_group(["1", "2"], "brand X", "2026-W40", ["NSW", "VIC"], "t4", None) == "G3"   # another week: another group
    assert st.get_group("G9") is None


def test_handles_are_never_reused_and_the_oldest_groups_drop():
    st = SessionState()
    for i in range(MAX_GROUPS + 3):
        st.add_group([str(i)], "x", WEEK, ["VIC"], "t", None)
    assert len(st.groups) == MAX_GROUPS
    assert "G1" not in st.groups and "G3" not in st.groups and f"G{MAX_GROUPS + 3}" in st.groups
    assert st.add_group(["new"], "x", WEEK, ["VIC"], "t", None) == f"G{MAX_GROUPS + 4}"


def test_state_survives_a_json_round_trip_and_key_order_loss():
    st = SessionState()
    st.set_scope(week=WEEK, brand="X", regions="NSW/VIC")
    for i in range(MAX_GROUPS + 1):
        st.add_group([str(i)], "x", WEEK, ["VIC"], "t", None)
    st.note_dropped(["an old question"])
    stored = json.loads(json.dumps(st.to_json(), sort_keys=True))              # Postgres jsonb reorders keys
    again = SessionState(stored)
    assert again.to_json() == st.to_json() and again.next_group == st.next_group
    assert again.add_group(["fresh"], "x", WEEK, ["VIC"], "t", None) == f"G{MAX_GROUPS + 2}"
    assert "G2" not in again.groups                                            # the oldest was dropped by number, not key order
    assert SessionState(None).to_json() == {"scope": {}, "groups": {}, "next_group": 1, "earlier_questions": [],
                                            "last_checks": {}}


def test_scope_keeps_only_real_filters():
    st = SessionState()
    st.set_scope(week=WEEK, brand=None, category="", name_contains="Reserve")
    assert st.scope == {"week": WEEK, "name_contains": "Reserve"}


def test_render_is_empty_without_state_and_lists_groups_without_their_skus():
    assert SessionState().render() == ""
    st = SessionState()
    st.set_scope(week=WEEK, brand="Larkspur")
    st.add_group(["100012", "100013"], "brand Larkspur", WEEK, ["VIC", "NSW"], "2026-09-21T09:00:00+00:00", "run-1@t")
    st.note_dropped(["what about Stonefield?"])
    block = st.render()
    assert "G1: 2 skus, brand Larkspur, week 2026-W39, regions NSW/VIC, resolved 2026-09-21T09:00:00+00:00" in block
    assert "last product filter: week 2026-W39, brand Larkspur" in block
    assert "nothing about their current status" in block and "what about Stonefield?" in block
    assert "100012" not in block                                               # handles keep the prompt small


def test_remembered_questions_are_capped_keeping_the_newest():
    st = SessionState()
    st.note_dropped([f"q{i}" for i in range(MAX_EARLIER + 10)])
    assert len(st.earlier_questions) == MAX_EARLIER and st.earlier_questions[-1] == f"q{MAX_EARLIER + 9}"


# ======================= history compaction =======================
def turn(q, answer, tool_rounds=1):
    msgs = [{"role": "user", "content": q}]
    for i in range(tool_rounds):
        tid = f"t_{q}_{i}"
        msgs += [{"role": "assistant", "content": [text("checking"), tool_use("check_rules", {"week": WEEK}, tid)]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": '{"big": "result"}'}]}]
    return msgs + [{"role": "assistant", "content": [text(answer)]}]


def assert_valid(messages):
    assert messages[0]["role"] == "user"
    for prev, cur in zip(messages, messages[1:]):
        assert prev["role"] != cur["role"]
    for i, m in enumerate(messages):
        uses = [b["id"] for b in m["content"] if isinstance(m["content"], list) and b["type"] == "tool_use"]
        if uses:
            assert sorted(uses) == sorted(b["tool_use_id"] for b in messages[i + 1]["content"])


def test_nothing_to_compact():
    assert compact_history([]) == ([], [])


def test_every_stored_turn_shrinks_to_question_and_final_answer():
    history = turn("q1", "answer 1", 2) + turn("q2", "answer 2", 1)
    out, dropped = compact_history(history)
    assert dropped == []
    assert out == [{"role": "user", "content": "q1"}, {"role": "assistant", "content": [text("answer 1")]},
                   {"role": "user", "content": "q2"}, {"role": "assistant", "content": [text("answer 2")]}]
    assert not any(isinstance(m["content"], list) and any(b["type"] in ("tool_use", "tool_result") for b in m["content"])
                   for m in out)
    assert_valid(out)


def test_turns_beyond_the_limit_are_dropped_whole_and_their_questions_kept():
    history = [m for i in range(1, 6) for m in turn(f"q{i}", f"a{i}")]
    out, dropped = compact_history(history, max_turns=2)
    assert dropped == ["q1", "q2", "q3"]
    assert [m["content"] for m in out if m["role"] == "user"] == ["q4", "q5"]
    assert_valid(out)


def test_compaction_is_idempotent_and_never_mutates_its_input():
    history = turn("q1", "a1", 2) + turn("q2", "a2")
    before = copy.deepcopy(history)
    once, _ = compact_history(history)
    assert history == before
    assert compact_history(once) == (once, [])


def test_intermediate_commentary_is_not_mistaken_for_the_answer():
    history = turn("q1", "the real answer", 3)
    out, _ = compact_history(history)
    assert out[1]["content"] == [text("the real answer")]                     # not "checking"
    stuck = [{"role": "user", "content": "q"}, {"role": "assistant", "content": [text("hm"), tool_use("x", {}, "t")]},
             {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "{}"}]}]
    assert compact_history(stuck)[0][1]["content"] == [text("(no answer)")]     # a turn that never got a final answer


# ======================= tools: groups and run resolution (no database) =======================
def call(ctx, name, **args):
    result, is_error = execute_tool(name, args, ctx)
    assert not is_error, result
    return result


def error(ctx, name, **args):
    result, is_error = execute_tool(name, args, ctx)
    assert is_error, result
    return result["error"]


@pytest.fixture
def sctx(env):
    """A context with session state, over the shared fixture warehouse and fake SAP."""
    return dataclasses.replace(env["ctx"], state=SessionState())


def promo_brand(env):
    sku = sorted(env["promo"])[0][0]
    return next(c["brand"] for c in env["wh"].get_candidates(WEEK) if c["sku"] == sku and c["name"] is not None)


def test_resolve_products_creates_a_group_and_records_the_scope(env, sctx):
    brand = promo_brand(env)
    r = call(sctx, "resolve_products", week=WEEK, brand=brand)
    assert r["group_id"] == "G1" and sctx.state.get_group("G1")["skus"] == r["skus"]
    assert sctx.state.scope == {"week": WEEK, "brand": brand, "regions": "NSW/QLD/VIC"}
    assert call(sctx, "resolve_products", week=WEEK, brand=brand)["group_id"] == "G1"     # asked again: same handle
    assert call(sctx, "resolve_products", week=WEEK, brand=brand, regions=["VIC"])["group_id"] == "G2"
    empty = call(sctx, "resolve_products", week=WEEK, brand="Nonexistent")
    assert "group_id" not in empty and sctx.state.scope["brand"] == "Nonexistent"          # nothing to hand out


def test_without_session_state_there_are_no_groups(env):
    assert "group_id" not in call(env["ctx"], "resolve_products", week=WEEK, brand=promo_brand(env))


@pytest.mark.parametrize("tool,keys", [("diagnose_batch", ("totals", "by_region", "patterns")),
                                       ("get_sap_conditions", ("conditions_found", "overridden")),
                                       ("get_api_log", ("records_logged", "problems", "patterns")),
                                       ("check_rules", ("priced_checked", "violations", "patterns"))])
def test_a_group_gives_the_same_answer_as_its_skus(env, sctx, tool, keys):
    r = call(sctx, "resolve_products", week=WEEK, brand=promo_brand(env))
    by_group = call(sctx, tool, week=WEEK, group_id=r["group_id"])
    by_skus = call(sctx, tool, week=WEEK, skus=r["skus"])
    assert {k: by_group[k] for k in keys} == {k: by_skus[k] for k in keys}


def test_group_regions_are_the_default_and_can_be_narrowed(env, sctx):
    gid = call(sctx, "resolve_products", week=WEEK, regions=["VIC", "NSW"])["group_id"]
    assert call(sctx, "diagnose_batch", week=WEEK, group_id=gid)["regions"] == ["NSW", "VIC"]
    assert call(sctx, "diagnose_batch", week=WEEK, group_id=gid, regions=["VIC"])["regions"] == ["VIC"]


@pytest.mark.parametrize("args,needle", [
    ({"group_id": "G9"}, "unknown group"),
    ({"group_id": "G1", "skus": ["1"]}, "not both"),
    ({}, "group_id"),                                                          # neither given: the message names both options
    ({"group_id": "G1", "week": "2026-W38"}, "resolved for 2026-W39"),
])
def test_group_misuse_is_an_error_the_model_can_read(env, sctx, args, needle):
    call(sctx, "resolve_products", week=WEEK, brand=promo_brand(env))
    assert needle in error(sctx, "diagnose_batch", **({"week": WEEK} | args))


def test_group_id_without_session_state_is_an_error(env):
    assert "no session groups" in error(env["ctx"], "diagnose_batch", week=WEEK, group_id="G1")


def test_a_group_cannot_widen_what_the_user_may_see(env, sctx):
    nsw = dataclasses.replace(sctx, allowed_regions=frozenset({"NSW"}))
    gid = call(nsw, "resolve_products", week=WEEK)["group_id"]
    assert nsw.state.get_group(gid)["regions"] == ["NSW"]
    vic_only = dataclasses.replace(nsw, allowed_regions=frozenset({"VIC"}))     # e.g. permissions changed mid-session
    assert "regions not available" in error(vic_only, "diagnose_batch", week=WEEK, group_id=gid)


class Runs:
    def __init__(self, run_id):
        self.run_id = run_id

    def latest(self, week):
        return None if self.run_id is None else {"run_id": self.run_id, "status": "SUCCEEDED", "finished_at": None,
                                                 "data_version": f"{self.run_id}@10:00"}


def with_older_run_sorting_last(env, tmp_path):
    """The fixture warehouse plus a second run 'zzz-old' (nothing sent), which sorts after 'run-1'."""
    path = str(tmp_path / "t.duckdb")
    shutil.copy(env["path"], path)
    con = duckdb.connect(path)
    con.execute("INSERT INTO price_recommendations "
                "SELECT * REPLACE ('zzz-old' AS run_id, NULL AS sap_status) FROM price_recommendations")
    con.close()
    return dataclasses.replace(env["ctx"], wh=DuckDBWarehouse(path))


def test_the_newest_finished_run_is_diagnosed_not_the_alphabetically_last(env, tmp_path):
    ctx = dataclasses.replace(with_older_run_sorting_last(env, tmp_path), runs=Runs("run-1"))
    d = call(ctx, "diagnose_batch", week=WEEK, skus=env["skus"], regions=["VIC"])
    assert d["run_id"] == "run-1" and d["data_version"] == "run-1@10:00"
    assert d["by_region"]["VIC"]["NOT_EFFECTIVE"] == 3                          # run-1's own picture, not zzz-old's
    d = call(ctx, "diagnose_batch", week=WEEK, skus=env["skus"], regions=["VIC"], run_id="zzz-old")
    assert d["run_id"] == "run-1"                                               # the model cannot pick another run
    assert call(ctx, "check_rules", week=WEEK, skus=env["skus"])["data_version"] == "run-1@10:00"


def test_a_pinned_run_beats_the_resolver_and_a_matching_pin_keeps_the_data_version(env, tmp_path):
    base = dataclasses.replace(with_older_run_sorting_last(env, tmp_path), runs=Runs("run-1"))
    pinned_other = dataclasses.replace(base, run_id="zzz-old")
    d = call(pinned_other, "diagnose_batch", week=WEEK, skus=env["skus"], regions=["VIC"])
    assert d["run_id"] == "zzz-old" and d["data_version"] == "zzz-old"
    pinned_same = dataclasses.replace(base, run_id="run-1")
    assert call(pinned_same, "diagnose_batch", week=WEEK, skus=env["skus"])["data_version"] == "run-1@10:00"


def test_without_any_pipeline_run_the_model_may_name_one(env, tmp_path):
    ctx = dataclasses.replace(with_older_run_sorting_last(env, tmp_path), runs=Runs(None))
    assert call(ctx, "diagnose_batch", week=WEEK, skus=env["skus"], regions=["VIC"])["run_id"] == "zzz-old"   # old behaviour
    d = call(ctx, "diagnose_batch", week=WEEK, skus=env["skus"], regions=["VIC"], run_id="run-1")
    assert d["run_id"] == "run-1" and d["data_version"] == "run-1"


def test_the_api_log_is_read_for_the_resolved_run(env):
    asked = []

    def handler(req):
        asked.append(dict(req.url.params))
        return httpx.Response(200, json={"items": []})
    ctx = dataclasses.replace(env["ctx"], runs=Runs("run-1"),
                              sap=httpx.Client(base_url="http://sap", transport=httpx.MockTransport(handler)))
    call(ctx, "get_api_log", week=WEEK, skus=env["skus"], run_id="something-else")
    assert asked == [{"week": WEEK, "run_id": "run-1"}]


def test_schemas_offer_group_id_and_no_longer_require_skus():
    from services.diagnostic_api.tools import TOOL_SCHEMAS
    for name in ("diagnose_batch", "get_sap_conditions", "get_api_log", "check_rules"):
        s = next(t for t in TOOL_SCHEMAS if t["name"] == name)["input_schema"]
        assert "group_id" in s["properties"] and s["required"] == ["week"]


# ======================= RunResolver on the pipeline's run table (Postgres) =======================
@pytest.fixture
def pipeline():
    with db.connect() as c:
        c.execute("DROP SCHEMA IF EXISTS pipeline_test CASCADE")
        c.execute(ddl())

    def add(run_id, status, finished, week=WEEK):
        with db.connect() as c:
            c.execute("INSERT INTO pipeline_test.pipeline_runs (run_id, week, status, started_at, finished_at) "
                      "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (run_id) DO UPDATE SET finished_at = EXCLUDED.finished_at, "
                      "status = EXCLUDED.status", (run_id, week, status, T0, finished))
    return add


T0 = datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


@pg
def test_the_newest_finished_run_wins_whatever_the_run_ids_say(pipeline):
    pipeline("zzz-old", "SUCCEEDED", at(10))
    pipeline("aaa-new", "COMPLETED_WITH_ISSUES", at(20))
    latest = RunResolver().latest(WEEK)
    assert latest["run_id"] == "aaa-new" and latest["status"] == "COMPLETED_WITH_ISSUES"
    assert latest["data_version"] == f"aaa-new@{at(20).isoformat()}"


@pg
def test_failed_and_running_runs_are_ignored_but_blocked_runs_count(pipeline):
    pipeline("good", "SUCCEEDED", at(10))
    pipeline("broke", "FAILED", at(30))
    pipeline("busy", "RUNNING", None)
    assert RunResolver().latest(WEEK)["run_id"] == "good"
    pipeline("held", "BLOCKED", at(40))
    assert RunResolver().latest(WEEK)["run_id"] == "held"


@pg
def test_other_weeks_do_not_count_and_no_run_or_no_table_is_none(pipeline):
    pipeline("w38", "SUCCEEDED", at(10), week="2026-W38")
    assert RunResolver().latest(WEEK) is None
    with db.connect() as c:
        c.execute("DROP SCHEMA pipeline_test CASCADE")                          # the pipeline has never run
    assert RunResolver().latest(WEEK) is None


@pg
def test_rerunning_an_older_run_makes_it_current_and_changes_the_data_version(pipeline):
    pipeline("run-a", "SUCCEEDED", at(10))
    pipeline("run-b", "SUCCEEDED", at(20))
    before = RunResolver().latest(WEEK)
    assert before["run_id"] == "run-b"
    pipeline("run-a", "SUCCEEDED", at(30))                                      # run-a finishes again
    after = RunResolver().latest(WEEK)
    assert after["run_id"] == "run-a" and after["data_version"] != before["data_version"]


@pg
def test_a_tie_on_finish_time_is_broken_deterministically(pipeline):
    pipeline("run-1", "SUCCEEDED", at(10))
    pipeline("run-2", "SUCCEEDED", at(10))
    assert RunResolver().latest(WEEK)["run_id"] == "run-2"
