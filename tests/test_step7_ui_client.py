"""Step 7, part 1: services/ui/client.py, the typed HTTP client the Streamlit app uses. No server, no database:
a fake transport plays diagnostic-api's part, the same way tests/test_step2_sap.py fakes mock-sap.
"""
import json

import httpx
import pytest

from services.ui.client import ApiClient, ApiError


def fake_api(handler):
    return ApiClient("http://api", "alice", transport=httpx.MockTransport(handler))


def echo_headers(status=200):
    def handler(req):
        return httpx.Response(status, json={"method": req.method, "path": req.url.path,
                                            "headers": {k: v for k, v in req.headers.items() if k.startswith("x-")},
                                            "body": req.content.decode() or None})
    return handler


# ======================= identity headers ======================= #
def test_user_header_is_always_sent():
    client = fake_api(echo_headers())
    assert client.healthz()["headers"]["x-user"] == "alice"


def test_role_header_is_sent_only_when_set():
    with_role = ApiClient("http://api", "ana", "analyst", transport=httpx.MockTransport(echo_headers()))
    assert with_role.healthz()["headers"]["x-role"] == "analyst"
    without_role = ApiClient("http://api", "ana", None, transport=httpx.MockTransport(echo_headers()))
    assert "x-role" not in without_role.healthz()["headers"]


# ======================= each call: method, path, body ======================= #
def test_create_session_with_and_without_a_week():
    client = fake_api(echo_headers())
    r = client.create_session("2026-W39")
    assert (r["method"], r["path"], r["headers"], json.loads(r["body"])) == ("POST", "/sessions", {"x-user": "alice"},
                                                                              {"week": "2026-W39"})
    assert client.create_session()["body"] is None                                    # no week: no body sent at all
    assert client.create_session(None)["body"] is None


def test_get_session_and_post_message():
    client = fake_api(echo_headers())
    got = client.get_session("sid-1")
    assert (got["method"], got["path"]) == ("GET", "/sessions/sid-1")
    sent = client.post_message("sid-1", "why?")
    assert (sent["method"], sent["path"], json.loads(sent["body"])) == ("POST", "/sessions/sid-1/messages", {"text": "why?"})


def test_list_findings_only_sends_filters_that_are_set():
    seen = []

    def handler(req):
        seen.append(dict(req.url.params))
        return httpx.Response(200, json=[])
    client = fake_api(handler)
    client.list_findings()
    client.list_findings("2026-W39", "resolved")
    client.list_findings(None, "")
    assert seen == [{"status": "open"}, {"week": "2026-W39", "status": "resolved"}, {}]


def test_create_and_resolve_finding():
    client = fake_api(echo_headers())
    body = {"week": "2026-W39", "region": "VIC", "error_code": "NOT_EFFECTIVE", "skus": ["1"]}
    made = client.create_finding(body)
    assert made["method"] == "POST" and made["path"] == "/findings"
    assert json.loads(made["body"]) == body
    resolved = client.resolve_finding(7)
    assert (resolved["method"], resolved["path"]) == ("POST", "/findings/7/resolve")


# ======================= errors ======================= #
def test_a_plain_detail_string_is_raised_as_apierror():
    client = fake_api(lambda req: httpx.Response(404, json={"detail": "session not found"}))
    with pytest.raises(ApiError) as e:
        client.get_session("sid-1")
    assert (e.value.status_code, e.value.detail) == (404, "session not found")


def test_a_422_validation_body_is_turned_into_one_readable_line():
    body = {"detail": [{"loc": ["body", "week"], "msg": "string does not match pattern"},
                       {"loc": ["body", "skus"], "msg": "field required"}]}
    client = fake_api(lambda req: httpx.Response(422, json=body))
    with pytest.raises(ApiError) as e:
        client.create_finding({})
    assert e.value.detail == "body.week: string does not match pattern; body.skus: field required"


def test_a_non_json_error_body_falls_back_to_raw_text():
    client = fake_api(lambda req: httpx.Response(502, text="Bad Gateway"))
    with pytest.raises(ApiError) as e:
        client.healthz()
    assert e.value.detail == "Bad Gateway"


def test_a_2xx_response_never_raises():
    client = fake_api(lambda req: httpx.Response(201, json={"ok": True}))
    assert client.healthz() == {"ok": True}


def test_str_of_apierror_includes_both_fields():
    assert str(ApiError(404, "not found")) == "404: not found"
