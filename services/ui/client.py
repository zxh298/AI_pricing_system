"""A thin, typed client for diagnostic-api. No DuckDB, Postgres or SAP access here: the ui talks to
diagnostic-api only, over HTTP, exactly like any other caller would.
"""
from __future__ import annotations

import httpx


class ApiError(Exception):
    """A non-2xx response. `detail` is diagnostic-api's own message, suitable to show a user directly."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(f"{status_code}: {detail}")
        self.status_code, self.detail = status_code, detail


def _detail(resp: httpx.Response) -> str:
    try:
        d = resp.json().get("detail")
    except ValueError:
        return resp.text[:300]
    if isinstance(d, list):                                        # FastAPI validation errors (422)
        return "; ".join(f"{'.'.join(str(x) for x in e.get('loc', []))}: {e.get('msg')}" for e in d)
    return d or resp.text[:300]


class ApiClient:
    """One instance per (base_url, user, role). `role` is sent as X-Role only when set (write endpoints need it)."""

    def __init__(self, base_url: str, user: str, role: str | None = None, transport: httpx.BaseTransport | None = None,
                 timeout: float = 30):
        headers = {"X-User": user}
        if role:
            headers["X-Role"] = role
        self._http = httpx.Client(base_url=base_url, headers=headers, timeout=timeout, transport=transport)

    def close(self) -> None:
        self._http.close()

    def _call(self, method: str, path: str, **kw) -> dict:
        r = self._http.request(method, path, **kw)
        if r.status_code >= 400:
            raise ApiError(r.status_code, _detail(r))
        return r.json()

    def healthz(self) -> dict:
        return self._call("GET", "/healthz")

    def create_session(self, week: str | None = None) -> dict:
        return self._call("POST", "/sessions", json=({"week": week} if week else None))

    def get_session(self, session_id: str) -> dict:
        return self._call("GET", f"/sessions/{session_id}")

    def post_message(self, session_id: str, text: str) -> dict:
        return self._call("POST", f"/sessions/{session_id}/messages", json={"text": text})

    def list_findings(self, week: str | None = None, status: str = "open") -> list[dict]:
        params = {k: v for k, v in {"week": week, "status": status}.items() if v}
        return self._call("GET", "/findings", params=params)

    def create_finding(self, body: dict) -> dict:
        return self._call("POST", "/findings", json=body)

    def resolve_finding(self, finding_id: int) -> dict:
        return self._call("POST", f"/findings/{finding_id}/resolve")
