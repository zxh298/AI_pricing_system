"""Step 4: retrying sender, pre-send validation, reconciliation, fault injection, weekly pipeline.

Unit tests need nothing. Integration tests need the Postgres container (docker compose up -d postgres)
and use throwaway schemas sap_test / pipeline_test.
"""
import os

import httpx
import pytest

os.environ["SAP_SCHEMA"], os.environ["PIPELINE_SCHEMA"] = "sap_test", "pipeline_test"
os.environ["SAP_READ_KEY"], os.environ["SAP_WRITE_KEY"] = "rk", "wk"

from jobs.data_gen.generate import SAP_TABLES, WEEK, build, write
from jobs.pricing_engine.sender import send
from jobs.weekly_pipeline.reconcile import reconcile
from jobs.weekly_pipeline.validate import validate_rows
from shared import db
from shared.warehouse import DuckDBWarehouse

REC = {"sku": "1", "pack_qty": 1, "region": "VIC", "markdown_price": 9.99, "valid_from": "2026-09-21", "valid_to": "2026-09-27"}


# ======================= sender: retry by error type (no database) =======================
def mock_client(handler):
    return httpx.Client(base_url="http://sap", transport=httpx.MockTransport(handler))


def ok_response(req):
    recs = __import__("json").loads(req.content)["prices"]
    return httpx.Response(200, json={"results": [{"sku": r["sku"], "pack_qty": r["pack_qty"], "region": r["region"],
                                                  "status": "ACCEPTED", "code": "OK", "message": "accepted", "duplicate": False} for r in recs]})


def test_retries_429_with_retry_after_then_succeeds():
    calls, sleeps = [], []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "3"}) if len(calls) <= 2 else ok_response(req)

    res = send(mock_client(handler), WEEK, "r", [REC], sleep=sleeps.append)
    assert res[0]["status"] == "ACCEPTED" and res[0]["attempts"] == 3 and sleeps == [3.0, 3.0]


def test_exponential_backoff_on_5xx_then_gives_up():
    sleeps = []
    res = send(mock_client(lambda req: httpx.Response(503, text="down")), WEEK, "r", [REC], max_retries=3, sleep=sleeps.append)
    assert sleeps == [0.5, 1.0, 2.0]                                     # base * 2**n
    assert res[0]["status"] == "ERROR" and res[0]["code"] == "HTTP_503" and res[0]["attempts"] == 4


@pytest.mark.parametrize("status", [400, 401, 403, 413, 422])
def test_no_retry_on_data_or_auth_errors(status):
    sleeps = []
    res = send(mock_client(lambda req: httpx.Response(status, text="bad")), WEEK, "r", [REC], sleep=sleeps.append)
    assert sleeps == [] and res[0]["code"] == f"HTTP_{status}" and res[0]["attempts"] == 1


def test_transport_error_is_retried():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("boom")
        return ok_response(req)

    res = send(mock_client(handler), WEEK, "r", [REC], sleep=lambda s: None)
    assert res[0]["status"] == "ACCEPTED" and res[0]["attempts"] == 2


def test_partial_failure_is_per_batch():
    def handler(req):
        body = __import__("json").loads(req.content)
        return httpx.Response(400, text="bad") if body["prices"][0]["sku"] == "1" else ok_response(req)

    prices = [REC, REC | {"sku": "2"}]
    res = send(mock_client(handler), WEEK, "r", prices, batch_size=1, sleep=lambda s: None)
    assert [(r["sku"], r["status"]) for r in res] == [("1", "ERROR"), ("2", "ACCEPTED")]


# ======================= pre-send validation (no database) =======================
def row(**kw):
    base = dict(week=WEEK, sku="1", pack_type="Single", pack_qty=1, region="VIC", week_no=3, shelf_price=100.0,
                current_price=95.0, recommended_price=90.0, status="PRICED")
    return base | kw


RULES = {("1", 1): dict(price_floor=40.0, max_markdown_pct=0.5, max_clearance_weeks=10),
         ("1", 6): dict(price_floor=200.0, max_markdown_pct=0.5, max_clearance_weeks=10)}


def codes(rows):
    return sorted(v["code"] for v in validate_rows(rows, RULES))


def test_validation_passes_clean_rows():
    assert codes([row()]) == []


@pytest.mark.parametrize("kw,code", [
    (dict(recommended_price=96.0), "PRICE_ABOVE_LAST_WEEK"),
    (dict(recommended_price=39.0, current_price=95.0), "BELOW_FLOOR"),
    (dict(recommended_price=45.0, current_price=95.0), "BELOW_MAX_MARKDOWN"),
    (dict(week_no=11), "EXCEEDS_MAX_WEEKS"),
    (dict(recommended_price=89.999), "BAD_PRICE_FORMAT"),
    (dict(recommended_price=-1.0), "BAD_PRICE_FORMAT"),
])
def test_validation_catches_each_rule(kw, code):
    assert code in codes([row(**kw)])


def test_validation_duplicate_and_missing_rule_and_ladder():
    assert "DUPLICATE_KEY" in codes([row(), row()])
    assert "MISSING_RULE" in codes([row(sku="9")])
    single, pack = row(recommended_price=50.0, current_price=95.0), row(pack_type="Multipack", pack_qty=6, shelf_price=600.0,
                                                                        current_price=500.0, recommended_price=270.0)
    assert "LADDER_VIOLATION" in codes([single, pack])                  # 270/6 = 45 < Single 50
    assert "LADDER_VIOLATION" not in codes([single, pack | {"recommended_price": 300.0}])


def test_validation_ignores_skipped_rows():
    assert codes([row(status="SKIPPED", recommended_price=None)]) == []


# ======================= reconciliation (no database) =======================
def test_reconcile_statuses():
    sent = [dict(sku=str(i), pack_qty=1, region="VIC", recommended_price=10.0) for i in range(4)]
    held = [dict(sku="0", pack_qty=1, region="VIC", price=10.0, effective_price=10.0, effective_source="MARKDOWN"),
            dict(sku="1", pack_qty=1, region="VIC", price=10.0, effective_price=7.0, effective_source="PROMOTION"),
            dict(sku="2", pack_qty=1, region="VIC", price=11.0, effective_price=11.0, effective_source="MARKDOWN")]
    out = {r["sku"]: r["reconcile_status"] for r in reconcile(sent, held)}
    assert out == {"0": "CONFIRMED", "1": "NOT_EFFECTIVE", "2": "PRICE_MISMATCH", "3": "MISSING_IN_SAP"}


# ======================= integration: mock-sap faults + full pipeline =======================
try:
    db.connect().close()
    HAVE_PG = True
except Exception:
    HAVE_PG = False
pg = pytest.mark.skipif(not HAVE_PG, reason="Postgres not reachable (docker compose up -d postgres)")

FAULT_VARS = ("SAP_FAULT_FAIL_FIRST_N", "SAP_FAULT_429_RATE", "SAP_FAULT_5XX_RATE", "SAP_FAULT_SEED", "SAP_FAULT_RETRY_AFTER")


@pytest.fixture
def stack(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from jobs.data_gen.seed_sap import seed_sap
    from services.mock_sap import main as sapmain
    for v in FAULT_VARS:
        monkeypatch.delenv(v, raising=False)
    sapmain.reset_faults()
    seed_sap({k: v for k, v in build(1).items() if k in SAP_TABLES})
    with db.connect() as c:
        c.execute("DROP SCHEMA IF EXISTS pipeline_test CASCADE")
    path = str(tmp_path / "w.duckdb")
    write(path, seed=1)
    with TestClient(sapmain.app, headers={"X-API-Key": "wk"}) as client:
        yield client, path, DuckDBWarehouse(path), monkeypatch, sapmain


def run(stack, run_id="run-1", **kw):
    from jobs.weekly_pipeline.pipeline import run_pipeline
    client, path, wh, *_ = stack
    return run_pipeline(WEEK, run_id, client, path, wh, sleep=kw.pop("sleep", lambda s: None), **kw)


def q(sql, args=()):
    with db.connect() as c:
        return c.execute(sql, args).fetchall()


def step_detail(run_id, step):
    r = q("SELECT status, detail FROM pipeline_test.pipeline_steps WHERE run_id = %s AND step = %s", (run_id, step))[0]
    return r["status"], r["detail"]


@pg
def test_mock_sap_429_carries_retry_after(stack):
    client, *_, monkeypatch, _ = stack
    monkeypatch.setenv("SAP_FAULT_429_RATE", "1")
    monkeypatch.setenv("SAP_FAULT_RETRY_AFTER", "7")
    r = client.post("/pricing/markdown-prices", json={"run_id": "x", "week": WEEK, "prices": []})
    assert r.status_code == 429 and r.headers["Retry-After"] == "7"
    sleeps = []
    res = send(client, WEEK, "x", [REC], max_retries=1, sleep=sleeps.append)
    assert sleeps == [7.0] and res[0]["code"] == "HTTP_429"


@pg
def test_pipeline_happy_path(stack):
    s = run(stack)
    assert s["status"] == "SUCCEEDED"
    assert (s["priced"], s["skipped"], s["accepted"], s["confirmed"]) == (126, 12, 126, 126)
    assert q("SELECT status, attempt FROM pipeline_test.pipeline_runs WHERE run_id = 'run-1'")[0] == {"status": "SUCCEEDED", "attempt": 1}
    steps = {r["step"]: r["status"] for r in q("SELECT step, status FROM pipeline_test.pipeline_steps WHERE run_id = 'run-1'")}
    assert steps == {k: "OK" for k in ("ingest", "price", "validate", "send", "reconcile", "history")}
    assert step_detail("run-1", "price")[1]["skipped"] == {"MISSING_RULE": 6, "FLOOR_ABOVE_PRICE": 3, "NOT_IN_PRODUCTS": 3}
    recs = q("SELECT send_status, reconcile_status, attempts FROM pipeline_test.pipeline_records WHERE run_id = 'run-1'")
    assert len(recs) == 126 and {(r["send_status"], r["reconcile_status"]) for r in recs} == {("ACCEPTED", "CONFIRMED")}


@pg
def test_scenarios_promo_and_overlap_show_up(stack):
    from jobs.data_gen.scenarios import apply_promo_override, apply_validity_overlap
    apply_promo_override(WEEK)
    apply_validity_overlap(WEEK)
    s = run(stack)
    assert s["status"] == "COMPLETED_WITH_ISSUES"
    assert (s["accepted"], s["rejected"], s["confirmed"], s["not_effective"]) == (117, 9, 114, 3)
    codes = q("SELECT DISTINCT send_code FROM pipeline_test.pipeline_records WHERE send_status = 'REJECTED'")
    assert [c["send_code"] for c in codes] == ["VALIDITY_OVERLAP"]
    ne = q("SELECT DISTINCT region FROM pipeline_test.pipeline_records WHERE reconcile_status = 'NOT_EFFECTIVE'")
    assert [r["region"] for r in ne] == ["VIC"]                          # VIC-only promotion; other regions fine


@pg
def test_rerun_resends_only_records_sap_has_not_accepted(stack):
    from jobs.data_gen.scenarios import apply_promo_override, apply_validity_overlap, clear_scenarios
    apply_promo_override(WEEK)
    apply_validity_overlap(WEEK)
    run(stack)
    clear_scenarios()                                                    # the SAP-side problems are fixed
    s = run(stack)                                                       # same run_id
    detail = step_detail("run-1", "send")[1]
    assert detail["sent"] == 9 and detail["already_accepted"] == 117     # only the 9 rejected records resent
    assert s["status"] == "SUCCEEDED" and s["accepted"] == 126 and s["confirmed"] == 126
    assert q("SELECT attempt FROM pipeline_test.pipeline_runs WHERE run_id = 'run-1'")[0]["attempt"] == 2
    assert len(q("SELECT 1 FROM sap_test.price_conditions WHERE run_id = 'run-1'")) == 126   # nothing duplicated


@pg
def test_rerun_with_nothing_left_to_send(stack):
    run(stack)
    s = run(stack)                                                       # every record already accepted
    detail = step_detail("run-1", "send")[1]
    assert detail["sent"] == 0 and detail["already_accepted"] == 126
    assert s["status"] == "SUCCEEDED" and s["confirmed"] == 126


@pg
def test_transient_faults_are_retried_with_backoff(stack):
    _, _, _, monkeypatch, _ = stack
    monkeypatch.setenv("SAP_FAULT_FAIL_FIRST_N", "2")                    # first 2 POSTs answer 503
    sleeps = []
    s = run(stack, sleep=sleeps.append)
    assert s["status"] == "SUCCEEDED" and sleeps == [0.5, 1.0]
    assert q("SELECT max(attempts) AS m FROM pipeline_test.pipeline_records WHERE run_id = 'run-1'")[0]["m"] == 3


@pg
def test_persistent_faults_then_recovery_on_rerun(stack):
    _, _, _, monkeypatch, sapmain = stack
    monkeypatch.setenv("SAP_FAULT_FAIL_FIRST_N", "1000")
    s = run(stack, max_retries=1)
    assert s["status"] == "COMPLETED_WITH_ISSUES" and (s["errors"], s["accepted"], s["confirmed"]) == (126, 0, 0)
    assert q("SELECT DISTINCT send_code FROM pipeline_test.pipeline_records")[0]["send_code"] == "HTTP_503"
    monkeypatch.delenv("SAP_FAULT_FAIL_FIRST_N")                         # SAP recovers
    sapmain.reset_faults()
    s = run(stack, max_retries=1)
    assert s["status"] == "SUCCEEDED" and s["accepted"] == 126 and s["confirmed"] == 126


@pg
def test_validation_gate_blocks_send(stack, monkeypatch):
    import jobs.weekly_pipeline.pipeline as pl
    real = pl.price_week

    def bad_engine(week, wh):
        rows = real(week, wh)
        r = next(x for x in rows if x["status"] == "PRICED")
        r["recommended_price"] = 0.01                                    # far below the SAP floor
        return rows

    monkeypatch.setattr(pl, "price_week", bad_engine)
    s = run(stack)
    assert s["status"] == "BLOCKED" and s["violations"] >= 1
    assert q("SELECT status FROM pipeline_test.pipeline_runs WHERE run_id = 'run-1'")[0]["status"] == "BLOCKED"
    assert step_detail("run-1", "validate")[0] == "BLOCKED"
    assert q("SELECT count(*) AS n FROM sap_test.price_conditions WHERE week = %s", (WEEK,))[0]["n"] == 0   # nothing reached SAP


@pg
def test_failed_step_is_recorded(stack, tmp_path):
    from jobs.weekly_pipeline.pipeline import run_pipeline

    def down(req):
        raise httpx.ConnectError("SAP unreachable")

    _, path, wh, *_ = stack
    with pytest.raises(httpx.ConnectError):
        run_pipeline(WEEK, "run-f", mock_client(down), path, wh)
    r = q("SELECT status, error FROM pipeline_test.pipeline_runs WHERE run_id = 'run-f'")[0]
    assert r["status"] == "FAILED" and r["error"].startswith("ingest:")


def duck(path, sql, args=()):
    import duckdb
    con = duckdb.connect(path, read_only=True)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


@pg
def test_history_step_appends_the_week_and_keeps_old_weeks(stack):
    _, path, *_ = stack
    before = duck(path, "SELECT count(*) FROM price_history WHERE week <= '2026-W38'")[0][0]
    assert duck(path, "SELECT status FROM weeks WHERE week = ?", [WEEK]) == [("open",)]
    s = run(stack)
    assert s["status"] == "SUCCEEDED"
    assert duck(path, "SELECT count(*) FROM price_history WHERE week <= '2026-W38'")[0][0] == before      # old weeks untouched
    assert duck(path, "SELECT count(*) FROM price_history WHERE week = ?", [WEEK])[0][0] == 128 * 3       # every item and region
    assert duck(path, "SELECT status FROM weeks WHERE week = ?", [WEEK]) == [("published",)]
    d = step_detail("run-1", "history")[1]
    assert d["rows"] == 384 and d["from_sap"] == 126 and d["carried_forward"] == 258
    # listed items are clearance with a week_no; unlisted items are carried forward from W38
    assert duck(path, "SELECT count(*) FROM price_history WHERE week = ? AND is_clearance", [WEEK])[0][0] == 135
    assert duck(path, "SELECT count(*) FROM price_history WHERE is_clearance <> (week_no IS NOT NULL)")[0][0] == 0
    assert duck(path, """SELECT count(*) FROM price_history a JOIN price_history b
        ON a.sku = b.sku AND a.pack_qty = b.pack_qty AND a.region = b.region AND a.week = ? AND b.week = '2026-W38'
        WHERE a.price > b.price + 1e-9""", [WEEK])[0][0] == 0                                          # never above last week
    run(stack)                                                                                          # rerun replaces the week
    assert duck(path, "SELECT count(*) FROM price_history WHERE week = ?", [WEEK])[0][0] == 384


@pg
def test_history_records_effective_price_and_keeps_last_price_for_rejected(stack):
    from jobs.data_gen.scenarios import apply_promo_override, apply_validity_overlap
    _, path, *_ = stack
    promo = apply_promo_override(WEEK)
    overlap = apply_validity_overlap(WEEK)
    run(stack)
    for r in promo:                       # promotion price is what shoppers pay in VIC
        eff = duck(path, "SELECT price FROM price_history WHERE week = ? AND sku = ? AND pack_qty = ? AND region = ?",
                   [WEEK, r["sku"], r["pack_qty"], r["region"]])[0][0]
        assert eff == round(r["current_price"] * 0.7, 2)
    for r in overlap:                     # rejected by SAP: price stays what it was last week
        p = duck(path, "SELECT price FROM price_history WHERE week = ? AND sku = ? AND pack_qty = ? AND region = ?",
                 [WEEK, r["sku"], r["pack_qty"], r["region"]])[0][0]
        assert p == r["current_price"]
