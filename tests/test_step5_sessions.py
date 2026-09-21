"""Step 5, slice 3a: sessions and the HTTP API. Needs the Postgres container (docker compose up -d postgres);
uses a throwaway schema `diagnostic_test`. The LLM is scripted and SAP is the fake from tests/conftest.py.
"""
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

os.environ["SAP_SCHEMA"] = "sap_test"
os.environ["DIAGNOSTIC_SCHEMA"] = "diagnostic_test"

from jobs.data_gen.generate import WEEK
from services.diagnostic_api.app import create_app, regions_for
from services.diagnostic_api.llm import ScriptedLLM, text, tool_use
from services.diagnostic_api.sessions import NotFound, SessionStore, TurnInProgress, VersionConflict, transcript_pairs
from shared import db

try:
    db.connect().close()
    HAVE_PG = True
except Exception:
    HAVE_PG = False
pg = pytest.mark.skipif(not HAVE_PG, reason="Postgres not reachable (docker compose up -d postgres)")

ALICE, BOB = {"X-User": "alice"}, {"X-User": "bob"}


# ======================= pure helpers (no database) =======================
def test_regions_for_reads_the_user_map():
    raw = '{"nsw-analyst": ["NSW"], "both": ["NSW", "VIC"]}'
    assert regions_for("nsw-analyst", raw) == frozenset({"NSW"})
    assert regions_for("both", raw) == frozenset({"NSW", "VIC"})
    assert regions_for("someone-else", raw) is None                     # not listed: every region
    assert regions_for("anyone", "") is None


def test_transcript_pairs_keep_only_questions_and_final_answers():
    history = [{"role": "user", "content": "q1"},
               {"role": "assistant", "content": [text("checking"), tool_use("check_rules", {}, "t1")]},
               {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "{}"}]},
               {"role": "assistant", "content": [text("all clear")]},
               {"role": "user", "content": "q2"},
               {"role": "assistant", "content": [text("second answer")]}]
    assert transcript_pairs(history) == [{"question": "q1", "answer": "all clear"},
                                         {"question": "q2", "answer": "second answer"}]
    assert transcript_pairs([]) == []


# ======================= the HTTP API =======================
@pytest.fixture
def api(env, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("DEFAULT_WEEK", WEEK)
    monkeypatch.delenv("USER_REGIONS", raising=False)
    monkeypatch.delenv("SESSION_IDLE_MINUTES", raising=False)
    with db.connect() as c:
        c.execute("DROP SCHEMA IF EXISTS diagnostic_test CASCADE")
    clock = [datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)]
    store = SessionStore(clock=lambda: clock[0])
    rig = SimpleNamespace(store=store, clock=clock, steps=[[text("hello")]], llms=[], ctx_calls=[], monkeypatch=monkeypatch)

    def llm_factory(week):
        llm = ScriptedLLM(rig.steps)
        rig.llms.append(llm)
        return llm

    def ctx_factory(user, regions):
        rig.ctx_calls.append((user, regions))
        return env["ctx"]

    with TestClient(create_app(store, llm_factory, ctx_factory)) as client:
        rig.client, rig.env = client, env
        yield rig


def start(api, headers=ALICE, **body):
    r = api.client.post("/sessions", json=body or None, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["session_id"]


def ask(api, sid, question="hi", headers=ALICE):
    return api.client.post(f"/sessions/{sid}/messages", json={"text": question}, headers=headers)


def rows(sql, args=()):
    with db.connect() as c:
        return c.execute(sql, args).fetchall()


@pg
def test_health_and_identity_required(api):
    assert api.client.get("/healthz").json() == {"status": "ok"}
    assert api.client.post("/sessions").status_code == 401                       # no X-User
    assert api.client.post("/sessions", headers={"X-User": "  "}).status_code == 401


@pg
def test_create_ask_and_read_back(api):
    sid = start(api)
    assert len(sid) >= 32 and sid != start(api)                                   # server-generated and unguessable
    r = ask(api, sid, "how are prices?")
    assert r.status_code == 200
    assert r.json() == {"turn": 1, "answer": "hello", "tool_calls": [], "steps": 1, "escalated": False, "truncated": False}
    s = api.client.get(f"/sessions/{sid}", headers=ALICE).json()
    assert s["week"] == WEEK and s["turns"] == 1
    assert s["conversation"] == [{"question": "how are prices?", "answer": "hello"}]


@pg
def test_a_turn_uses_tools_and_the_second_turn_sees_the_first(api):
    sid = start(api)
    api.steps = [[tool_use("check_rules", {"week": WEEK, "skus": api.env["skus"]})], [text("no violations")]]
    first = ask(api, sid, "any rule problems?").json()
    assert first["answer"] == "no violations" and [c["tool"] for c in first["tool_calls"]] == ["check_rules"]
    api.steps = [[text("still none")]]
    assert ask(api, sid, "and now?").json()["turn"] == 2
    seen = api.llms[-1].calls[0]["messages"]
    assert [m["role"] for m in seen] == ["user", "assistant", "user", "assistant", "user"]   # turn 1 in full, then turn 2
    assert seen[0]["content"] == "any rule problems?" and seen[-1]["content"] == "and now?"


@pg
def test_someone_elses_session_looks_like_it_does_not_exist(api):
    sid = start(api)
    unknown = api.client.get("/sessions/does-not-exist", headers=BOB)
    for r in (api.client.get(f"/sessions/{sid}", headers=BOB), ask(api, sid, headers=BOB)):
        assert r.status_code == 404 and r.json() == unknown.json()                # same answer as an unknown id
    assert api.llms == []                                                         # and the model was never called


@pg
def test_identity_comes_from_the_header_only(api):
    r = api.client.post("/sessions?user=mallory", json={"user": "mallory", "user_id": "mallory"}, headers=ALICE)
    sid = r.json()["session_id"]
    assert rows("SELECT user_id FROM diagnostic_test.sessions WHERE session_id = %s", (sid,))[0]["user_id"] == "alice"
    assert api.client.get(f"/sessions/{sid}?user=alice", headers={"X-User": "mallory"}).status_code == 404
    assert ask(api, sid, headers={"X-User": "mallory"}).status_code == 404


@pg
def test_permissions_come_from_env_and_are_injected_into_the_tools(api):
    api.monkeypatch.setenv("USER_REGIONS", '{"nsw-analyst": ["NSW"]}')
    ask(api, start(api, {"X-User": "nsw-analyst"}), headers={"X-User": "nsw-analyst"})
    ask(api, start(api), "hi")
    assert api.ctx_calls == [("nsw-analyst", frozenset({"NSW"})), ("alice", None)]


@pg
def test_message_validation(api):
    sid = start(api)
    assert api.client.post(f"/sessions/{sid}/messages", json={"text": ""}, headers=ALICE).status_code == 422
    assert api.client.post(f"/sessions/{sid}/messages", json={"text": "x" * 4001}, headers=ALICE).status_code == 422
    assert api.client.post(f"/sessions/{sid}/messages", json={}, headers=ALICE).status_code == 422
    assert api.client.post("/sessions", json={"week": "W39"}, headers=ALICE).status_code == 422


@pg
def test_idle_expiry_and_activity_extends_the_session(api):
    sid = start(api)
    api.clock[0] += timedelta(minutes=100)
    assert ask(api, sid).status_code == 200                                       # activity restarts the idle timer
    api.clock[0] += timedelta(minutes=100)
    assert ask(api, sid).status_code == 200                                       # 200 min after creation, still alive
    api.clock[0] += timedelta(minutes=121)
    for r in (ask(api, sid), api.client.get(f"/sessions/{sid}", headers=ALICE)):
        assert r.status_code == 410 and "expired" in r.json()["detail"]
    assert len(api.llms) == 2                                                     # the expired message never reached the model
    assert len(rows("SELECT 1 FROM diagnostic_test.turn_transcripts WHERE session_id = %s", (sid,))) == 2   # audit stays


@pg
def test_session_expires_when_the_pricing_week_moves_on(api):
    sid = start(api)
    api.monkeypatch.setenv("DEFAULT_WEEK", "2026-W40")
    r = ask(api, sid)
    assert r.status_code == 410 and "2026-W39" in r.json()["detail"]
    assert start(api)                                                             # a new session is for the new week


@pg
def test_only_one_turn_at_a_time_per_session(api):
    sid, other = start(api), start(api)
    with api.store.turn_lock(sid):
        assert ask(api, sid).status_code == 409                                   # busy
        assert ask(api, other).status_code == 200                                 # other sessions are unaffected
    assert ask(api, sid).status_code == 200                                       # lock released


@pg
def test_turn_lock_raises_and_releases(api):
    sid = start(api)
    with api.store.turn_lock(sid):
        with pytest.raises(TurnInProgress):
            with api.store.turn_lock(sid):
                pass
    with api.store.turn_lock(sid):
        pass


@pg
def test_a_concurrent_save_wins_and_is_not_overwritten(api):
    sid = start(api)
    competing = [{"role": "user", "content": "competitor"}]

    def race(messages):                                                           # another writer lands mid-turn
        s = api.store.get(sid, "alice")
        api.store.save_turn(s, state={}, history=competing, question="competitor", answer="x", tool_calls=[], messages=[])
        return [text("late answer")]
    api.steps = [race]
    r = ask(api, sid)
    assert r.status_code == 409 and "changed" in r.json()["detail"]
    row = rows("SELECT version, history FROM diagnostic_test.sessions WHERE session_id = %s", (sid,))[0]
    assert row["version"] == 1 and row["history"] == competing                    # the competitor's save is intact


@pg
def test_save_turn_checks_the_version(api):
    s = api.store.get(start(api), "alice")
    api.store.save_turn(s, state={}, history=[], question="q", answer="a", tool_calls=[], messages=[])
    with pytest.raises(VersionConflict):
        api.store.save_turn(s, state={}, history=[], question="q", answer="a", tool_calls=[], messages=[])   # stale copy


@pg
def test_a_failed_turn_saves_nothing(api):
    sid = start(api)
    assert ask(api, sid, "first").status_code == 200
    api.steps = []                                                                # the model call fails
    r = ask(api, sid, "this one fails")
    assert r.status_code == 502 and "nothing was saved" in r.json()["detail"]
    assert "ScriptedLLM" not in r.text                                            # no internals leak to the client
    s = api.client.get(f"/sessions/{sid}", headers=ALICE).json()
    assert s["turns"] == 1 and s["conversation"] == [{"question": "first", "answer": "hello"}]
    assert len(rows("SELECT 1 FROM diagnostic_test.turn_transcripts WHERE session_id = %s", (sid,))) == 1
    api.steps = [[text("recovered")]]
    assert ask(api, sid, "retry").json()["turn"] == 2                             # the session is still usable


@pg
def test_every_turn_is_audited_in_full(api):
    sid = start(api)
    api.steps = [[tool_use("check_rules", {"week": WEEK, "skus": api.env["skus"]})], [text("no violations")]]
    ask(api, sid, "any rule problems?")
    api.steps = [[text("second")]]
    ask(api, sid, "and now?")
    t = rows("SELECT * FROM diagnostic_test.turn_transcripts WHERE session_id = %s ORDER BY turn_no", (sid,))
    assert [(r["turn_no"], r["user_id"], r["question"], r["answer"]) for r in t] == [
        (1, "alice", "any rule problems?", "no violations"), (2, "alice", "and now?", "second")]
    assert [c["tool"] for c in t[0]["tool_calls"]] == ["check_rules"] and t[1]["tool_calls"] == []
    kinds = [(m["role"], m["content"] if isinstance(m["content"], str) else m["content"][0]["type"]) for m in t[0]["messages"]]
    assert kinds == [("user", "any rule problems?"), ("assistant", "tool_use"), ("user", "tool_result"), ("assistant", "text")]
    result = json.loads(t[0]["messages"][2]["content"][0]["content"])           # the full tool output is kept
    assert result["violations"] == 0 and result["priced_checked"] == 126
    assert len(t[1]["messages"]) == 2                                             # a turn stores only its own messages


@pg
def test_store_get_reports_not_found_for_the_wrong_owner(api):
    sid = start(api)
    with pytest.raises(NotFound):
        api.store.get(sid, "bob")
