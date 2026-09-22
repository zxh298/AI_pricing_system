"""Step 7, part 2: the Streamlit app (services/ui/app.py), driven headlessly with Streamlit's AppTest.

No real HTTP call and no diagnostic-api needed: services.ui.client.ApiClient is monkeypatched to FakeApiClient
before the app script runs, so every test controls exactly what the "backend" returns. st.cache_resource is a
process-wide cache, so it is cleared before every test to stop one test's fake client leaking into another's.
"""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from services.ui.client import ApiError

APP = str(Path(__file__).resolve().parents[1] / "services" / "ui" / "app.py")

SESSION = {"session_id": "sid-123456789012345678901234", "week": "2026-W39", "expires_at": "2026-09-22T12:00:00+00:00"}


class FakeApiClient:
    """Records every call it receives; each test configures the class attributes it needs before .run()."""
    last_instance = None
    healthz_ok = True
    create_session_result = SESSION
    create_session_error = None
    get_session_result = {**SESSION, "turns": 0, "groups": {}, "conversation": []}
    get_session_error = None
    post_message_result = {"turn": 1, "answer": "an answer", "tool_calls": [], "steps": 1,
                           "escalated": False, "truncated": False}
    post_message_error = None
    findings_result = []
    findings_error = None
    create_finding_result = {"finding_id": 1}
    create_finding_error = None
    resolve_finding_error = None

    def __init__(self, base_url, user, role=None):
        self.base_url, self.user, self.role = base_url, user, role
        self.calls = []
        FakeApiClient.last_instance = self

    def healthz(self):
        self.calls.append(("healthz",))
        if not FakeApiClient.healthz_ok:
            raise RuntimeError("down")
        return {"status": "ok"}

    def create_session(self, week=None):
        self.calls.append(("create_session", week))
        if FakeApiClient.create_session_error:
            raise FakeApiClient.create_session_error
        return FakeApiClient.create_session_result

    def get_session(self, session_id):
        self.calls.append(("get_session", session_id))
        if FakeApiClient.get_session_error:
            raise FakeApiClient.get_session_error
        return FakeApiClient.get_session_result

    def post_message(self, session_id, text):
        self.calls.append(("post_message", session_id, text))
        if FakeApiClient.post_message_error:
            raise FakeApiClient.post_message_error
        return FakeApiClient.post_message_result

    def list_findings(self, week=None, status="open"):
        self.calls.append(("list_findings", week, status))
        if FakeApiClient.findings_error:
            raise FakeApiClient.findings_error
        return FakeApiClient.findings_result

    def create_finding(self, body):
        self.calls.append(("create_finding", body))
        if FakeApiClient.create_finding_error:
            raise FakeApiClient.create_finding_error
        return FakeApiClient.create_finding_result

    def resolve_finding(self, finding_id):
        self.calls.append(("resolve_finding", finding_id))
        if FakeApiClient.resolve_finding_error:
            raise FakeApiClient.resolve_finding_error
        return {"finding_id": finding_id, "status": "resolved"}


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    import streamlit as st
    st.cache_resource.clear()                                       # a stale cached client must never leak into a test
    for name, default in [("healthz_ok", True), ("create_session_error", None), ("get_session_error", None),
                          ("post_message_error", None), ("findings_result", []), ("findings_error", None),
                          ("create_finding_error", None), ("resolve_finding_error", None)]:
        setattr(FakeApiClient, name, default)
    FakeApiClient.create_session_result, FakeApiClient.get_session_result = SESSION, {**SESSION, "turns": 0, "groups": {}, "conversation": []}
    FakeApiClient.last_instance = None
    monkeypatch.setattr("services.ui.client.ApiClient", FakeApiClient)


def run():
    at = AppTest.from_file(APP)
    at.run()
    return at


def start_session(at):
    at.button(key="new_session_button").click().run()
    return at


# ======================= identity and the client ======================= #
def test_no_session_prompts_to_start_one(reset):
    at = run()
    assert not at.exception
    assert any("Start a session" in i.value for i in at.info)


def test_a_viewer_client_carries_no_role(reset):
    run()
    assert FakeApiClient.last_instance.role is None and FakeApiClient.last_instance.user == "alice"


def test_switching_to_analyst_changes_the_client_role(reset):
    at = run()
    at.selectbox(key="role_select").select("analyst").run()
    assert FakeApiClient.last_instance.role == "analyst"


def test_changing_the_user_creates_a_different_cached_client(reset):
    at = run()
    first = FakeApiClient.last_instance
    at.text_input(key="user_input").set_value("bob").run()
    assert FakeApiClient.last_instance is not first and FakeApiClient.last_instance.user == "bob"


def test_healthz_failure_is_shown_without_crashing_the_page(reset):
    FakeApiClient.healthz_ok = False
    at = run()
    assert not at.exception
    assert any("not reachable" in e.value for e in at.error)


# ======================= starting and ending a session ======================= #
def test_new_session_button_creates_one_and_shows_it(reset):
    at = start_session(run())
    assert not at.exception
    assert ("create_session", None) in FakeApiClient.last_instance.calls
    assert at.session_state["session"] == SESSION
    assert any(SESSION["session_id"][:10] in c.value for c in at.caption)


def test_the_entered_week_is_sent_and_blank_means_server_default(reset):
    at = run()
    at.text_input(key="new_session_week").set_value("2026-W40").run()
    start_session(at)
    assert ("create_session", "2026-W40") in FakeApiClient.last_instance.calls
    at2 = start_session(run())                                       # blank week
    assert ("create_session", None) in FakeApiClient.last_instance.calls


def test_a_session_creation_failure_is_shown_and_nothing_is_stored(reset):
    FakeApiClient.create_session_error = ApiError(502, "diagnostic-api unavailable")
    at = start_session(run())
    assert at.session_state["session"] is None
    assert any("diagnostic-api unavailable" in e.value for e in at.error)


def test_forget_session_clears_local_state(reset):
    at = start_session(run())
    at.button(key="forget_session_button").click().run()
    assert at.session_state["session"] is None
    assert any("Start a session" in i.value for i in at.info)


# ======================= chat ======================= #
def test_conversation_is_rendered_from_get_session_not_kept_locally(reset):
    FakeApiClient.get_session_result = {**SESSION, "turns": 1, "groups": {},
                                        "conversation": [{"question": "why?", "answer": "because"}]}
    at = start_session(run())
    users = [m.value for cm in at.chat_message if cm.name == "user" for m in cm.markdown]
    assistants = [m.value for cm in at.chat_message if cm.name == "assistant" for m in cm.markdown]
    assert users == ["why?"] and assistants == ["because"]


def test_product_groups_are_shown_in_an_expander(reset):
    FakeApiClient.get_session_result = {**SESSION, "turns": 0, "conversation": [],
                                        "groups": {"G1": {"skus": 5, "label": "brand Larkspur", "week": "2026-W39",
                                                          "regions": ["NSW", "VIC"]}}}
    at = start_session(run())
    assert any("1 product group" in e.label for e in at.expander)
    assert any("G1" in m.value and "5 skus" in m.value for m in at.markdown)


def test_asking_a_question_posts_it_and_shows_the_tool_calls(reset):
    FakeApiClient.post_message_result = {"turn": 1, "answer": "found it", "steps": 2, "escalated": False,
                                         "truncated": False, "tool_calls": [{"tool": "diagnose_batch", "input": {"week": "2026-W39"},
                                                                            "is_error": False}]}
    at = start_session(run())
    at.chat_input[0].set_value("why did VIC not drop?").run()
    assert ("post_message", SESSION["session_id"], "why did VIC not drop?") in FakeApiClient.last_instance.calls
    assert at.session_state["last_tool_calls"] == FakeApiClient.post_message_result["tool_calls"]
    shown = " ".join(e.label for e in at.expander) + " ".join(m.value for e in at.expander for m in e.markdown)
    assert "diagnose_batch" in shown


def test_escalated_and_truncated_are_shown_as_warnings(reset):
    FakeApiClient.post_message_result = {"turn": 1, "answer": "...", "steps": 8, "tool_calls": [],
                                         "escalated": True, "truncated": True}
    at = start_session(run())
    at.chat_input[0].set_value("a hard question").run()
    warnings = " ".join(w.value for w in at.warning)
    assert "escalated" in warnings and "cut off" in warnings


def test_a_409_turn_in_progress_gets_a_specific_message(reset):
    FakeApiClient.post_message_error = ApiError(409, "another message is already being processed")
    at = start_session(run())
    at.chat_input[0].set_value("again?").run()
    assert at.session_state["session"] == SESSION                     # a 409 does not clear the session
    assert any("already being processed" in i.value for i in at.info)


def test_an_expired_or_missing_session_is_cleared_on_get(reset):
    FakeApiClient.get_session_error = ApiError(410, "session expired")
    at = start_session(run())
    assert at.session_state["session"] is None
    assert any("session expired" in e.value for e in at.error)


def test_an_expired_session_is_cleared_when_posting_a_message(reset):
    at = start_session(run())
    FakeApiClient.post_message_error = ApiError(404, "session not found")
    at.chat_input[0].set_value("hi").run()
    assert at.session_state["session"] is None


# ======================= findings ======================= #
FINDING = {"finding_id": 1, "week": "2026-W39", "region": "VIC", "error_code": "NOT_EFFECTIVE", "reason": "PROMOTION",
          "brand": "Larkspur", "owner": "Promotions team", "ticket": "PRC-42", "status": "open",
          "confirmed_by": "ana", "created_at": "2026-09-21T09:00:00+00:00"}


def test_findings_are_listed_and_the_filters_are_sent(reset):
    FakeApiClient.findings_result = [FINDING]
    at = run()
    assert ("list_findings", None, "open") in FakeApiClient.last_instance.calls
    assert not at.dataframe[0].value.empty and at.dataframe[0].value.iloc[0]["ticket"] == "PRC-42"
    at.selectbox(key="f_status").select("all").run()
    assert ("list_findings", None, "all") in FakeApiClient.last_instance.calls


def test_no_findings_shows_a_caption_not_an_empty_table(reset):
    at = run()
    assert at.dataframe == []
    assert any("No findings match" in c.value for c in at.caption)


def test_a_viewer_cannot_see_write_controls(reset):
    FakeApiClient.findings_result = [FINDING]
    at = run()
    labels = [b.label for b in at.button]
    assert not any("Resolve" in x or x == "Record finding" for x in labels)
    assert any('"analyst"' in c.value for c in at.caption)


def test_an_analyst_can_resolve_an_open_finding_but_not_a_resolved_one(reset):
    FakeApiClient.findings_result = [FINDING, {**FINDING, "finding_id": 2, "status": "resolved"}]
    at = run()
    at.selectbox(key="role_select").select("analyst").run()
    labels = [b.label for b in at.button]
    assert any("Resolve #1" in x for x in labels) and not any("Resolve #2" in x for x in labels)
    at.button(key="resolve-1").click().run()
    assert ("resolve_finding", 1) in FakeApiClient.last_instance.calls


def test_an_analyst_can_record_a_finding_and_the_form_clears(reset):
    at = run()
    at.selectbox(key="role_select").select("analyst").run()
    at.text_input(key="nf_week").set_value("2026-W39").run()
    at.text_input(key="nf_region").set_value("VIC").run()
    at.text_input(key="nf_code").set_value("NOT_EFFECTIVE").run()
    at.text_input(key="nf_reason").set_value("PROMOTION").run()
    at.text_input(key="nf_skus").set_value("100012, 100013 ,100014").run()
    at.text_area(key="nf_cause").set_value("a VIC promotion overrides the markdown").run()
    at.button(key="nf_submit").click().run()
    tool, body = next(c for c in FakeApiClient.last_instance.calls if c[0] == "create_finding")
    assert tool == "create_finding"
    assert body == {"week": "2026-W39", "region": "VIC", "error_code": "NOT_EFFECTIVE", "reason": "PROMOTION",
                    "brand": None, "skus": ["100012", "100013", "100014"],
                    "root_cause": "a VIC promotion overrides the markdown", "owner": None, "ticket": None}
    # st.rerun() drives a fresh script pass within this same .click().run() call, so by the time it returns the
    # flash message has already been shown once and cleared (never displayed twice, never lost)
    assert any("recorded" in s.value for s in at.success)
    assert at.session_state["flash"] is None
    # clear_on_submit is a frontend-only behaviour AppTest does not simulate; not checked here


def test_a_finding_creation_error_is_shown(reset):
    FakeApiClient.create_finding_error = ApiError(422, "root_cause: field required")
    at = run()
    at.selectbox(key="role_select").select("analyst").run()
    at.button(key="nf_submit").click().run()
    assert any("root_cause: field required" in e.value for e in at.error)


def test_a_findings_list_error_does_not_crash_the_page(reset):
    FakeApiClient.findings_error = ApiError(502, "diagnostic-api unavailable")
    at = run()
    assert not at.exception
    assert any("diagnostic-api unavailable" in e.value for e in at.error)
