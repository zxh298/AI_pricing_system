"""Streamlit chat UI for the diagnostic assistant (build step 7).

    streamlit run services/ui/app.py

A thin client of diagnostic-api: no DuckDB, Postgres or SAP access, and no LLM call of its own. Everything shown
here came back from an HTTP call to diagnostic-api, so it is exactly what the API would give any other caller.
Reads .env from the current directory (DIAGNOSTIC_API_URL, default http://localhost:8000). diagnostic-api,
mock-sap and Postgres must already be running -- see docs/PROJECT_CONTEXT.md section 17.10.
"""
from __future__ import annotations

import sys
from pathlib import Path

# `streamlit run` puts only this file's own directory on sys.path, unlike `python -m`, so the `services.*` /
# `shared.*` imports below would fail without the project root added first.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import streamlit as st

from services.ui.client import ApiClient, ApiError
from shared.config import load_config
from shared.envfile import load_dotenv

load_dotenv()
st.set_page_config(page_title="Clearance Pricing Diagnostics", page_icon=":bar_chart:", layout="wide")

DEFAULTS = {"user": "alice", "role": "viewer", "session": None, "last_tool_calls": [], "last_flags": {}, "flash": None}
for _key, _value in DEFAULTS.items():
    st.session_state.setdefault(_key, _value)


def flash(kind: str, message: str) -> None:
    """Queue a message to show right after the next rerun (a message shown, then immediately followed by
    st.rerun(), would be replaced before a real user ever saw it)."""
    st.session_state.flash = (kind, message)


def show_flash() -> None:
    if st.session_state.flash:
        kind, message = st.session_state.flash
        getattr(st, kind)(message)
        st.session_state.flash = None


@st.cache_resource(show_spinner=False)
def _client(base_url: str, user: str, role: str | None) -> ApiClient:
    """One httpx.Client per (base_url, user, role), reused across reruns instead of opened and leaked each time."""
    return ApiClient(base_url, user, role)


# ---------------- sidebar: identity and session ----------------
with st.sidebar:
    st.header("Identity")
    st.session_state.user = st.text_input("User (X-User)", st.session_state.user, key="user_input")
    st.session_state.role = st.selectbox("Role", ["viewer", "analyst"],
                                         index=["viewer", "analyst"].index(st.session_state.role), key="role_select")
    st.caption("A stand-in for IAP locally; the model never sees this or decides what you may see.")
    api_url = st.text_input("diagnostic-api URL", load_config().diagnostic_api_url, key="api_url_input")

client = _client(api_url, st.session_state.user, st.session_state.role if st.session_state.role == "analyst" else None)

with st.sidebar:
    st.divider()
    st.header("Session")
    week = st.text_input("Week (blank = server default)", value="", key="new_session_week")
    if st.button("New session", width='stretch', key="new_session_button"):
        try:
            st.session_state.session = client.create_session(week or None)
            st.session_state.last_tool_calls, st.session_state.last_flags = [], {}
        except ApiError as e:
            st.error(f"Could not start a session: {e.detail}")
    s = st.session_state.session
    if s:
        st.caption(f"session `{s['session_id'][:10]}…`  \nweek {s['week']}  \nexpires {s['expires_at']}")
        if st.button("Forget session", width='stretch', key="forget_session_button"):
            st.session_state.session = None
            st.session_state.last_tool_calls, st.session_state.last_flags = [], {}
    st.divider()
    try:
        client.healthz()
        st.success("diagnostic-api reachable", icon=":material/check_circle:")
    except Exception:
        st.error("diagnostic-api not reachable", icon=":material/error:")

tab_chat, tab_findings = st.tabs(["Chat", "Findings"])

# ---------------- chat ----------------
with tab_chat:
    s = st.session_state.session
    if s is None:
        st.info("Start a session in the sidebar to ask a question.")
    else:
        full = None
        try:
            full = client.get_session(s["session_id"])
        except ApiError as e:
            st.error(e.detail)
            if e.status_code in (404, 410):                          # someone else's session, or expired: recover cleanly
                st.session_state.session = None

        if full is not None:
            if full["groups"]:
                with st.expander(f"Session state: {len(full['groups'])} product group(s)"):
                    for handle, g in full["groups"].items():
                        st.write(f"**{handle}** — {g['skus']} skus, {g['label']}, week {g['week']}, "
                                f"regions {'/'.join(g['regions'])}")

            for turn in full["conversation"]:
                with st.chat_message("user"):
                    st.write(turn["question"])
                with st.chat_message("assistant"):
                    st.write(turn["answer"])

            if st.session_state.last_tool_calls:
                with st.expander(f"Tool calls behind the last answer ({len(st.session_state.last_tool_calls)})",
                                 expanded=False):
                    for c in st.session_state.last_tool_calls:
                        mark = ":material/error:" if c["is_error"] else ":material/check:"
                        st.markdown(f"{mark} `{c['tool']}`")
                        if c["input"]:
                            st.json(c["input"], expanded=False)

            flags = st.session_state.last_flags
            if flags.get("escalated"):
                st.warning("The assistant hit its step limit and escalated instead of answering.")
            if flags.get("truncated"):
                st.warning("The last answer may have been cut off (max_tokens).")

            question = st.chat_input("Ask about this week's clearance pricing…")
            if question:
                with st.spinner("Thinking…"):
                    try:
                        out = client.post_message(s["session_id"], question)
                        st.session_state.last_tool_calls = out["tool_calls"]
                        st.session_state.last_flags = {"escalated": out["escalated"], "truncated": out["truncated"]}
                    except ApiError as e:
                        st.error(e.detail)
                        if e.status_code in (404, 410):
                            st.session_state.session = None
                        elif e.status_code == 409:
                            st.info("Another message is already being processed in this session; try again shortly.")
                    else:
                        st.rerun()      # only on success: rerunning on the error path would wipe the message above unseen

# ---------------- findings ----------------
with tab_findings:
    st.subheader("Confirmed findings")
    show_flash()
    col1, col2, col3 = st.columns([2, 2, 1])
    f_week = col1.text_input("Week", value=(st.session_state.session or {}).get("week", ""), key="f_week")
    f_status = col2.selectbox("Status", ["open", "resolved", "all"], key="f_status")
    if col3.button("Refresh", width='stretch'):
        st.rerun()

    rows: list[dict] = []
    try:
        rows = client.list_findings(f_week or None, f_status)
    except ApiError as e:
        st.error(f"Could not list findings: {e.detail}")

    if rows:
        st.dataframe([{k: r[k] for k in ("finding_id", "week", "region", "error_code", "reason", "brand", "owner",
                                         "ticket", "status", "confirmed_by", "created_at")} for r in rows],
                    width='stretch', hide_index=True)
        if st.session_state.role == "analyst":
            open_rows = [r for r in rows if r["status"] == "open"]
            if open_rows:
                st.caption("Resolve a finding once the underlying problem is gone:")
                for r in open_rows:
                    if st.button(f"Resolve #{r['finding_id']} ({r['error_code']} in {r['region']})",
                                key=f"resolve-{r['finding_id']}"):
                        try:
                            client.resolve_finding(r["finding_id"])
                            st.rerun()
                        except ApiError as e:
                            st.error(f"Could not resolve #{r['finding_id']}: {e.detail}")
    else:
        st.caption("No findings match these filters.")

    if st.session_state.role == "analyst":
        st.divider()
        with st.form("new_finding", clear_on_submit=True):
            st.write("Record a confirmed finding")
            c1, c2 = st.columns(2)
            nf_week = c1.text_input("Week", value=(st.session_state.session or {}).get("week", ""), key="nf_week")
            nf_region = c2.text_input("Region", key="nf_region")
            c3, c4 = st.columns(2)
            nf_code = c3.text_input("Error code (e.g. NOT_EFFECTIVE)", key="nf_code")
            nf_reason = c4.text_input("Reason (e.g. PROMOTION)", key="nf_reason")
            nf_brand = st.text_input("Brand (optional)", key="nf_brand")
            nf_skus = st.text_input("SKUs (comma-separated)", key="nf_skus")
            nf_cause = st.text_area("Root cause", key="nf_cause")
            c5, c6 = st.columns(2)
            nf_owner = c5.text_input("Owning team (optional)", key="nf_owner")
            nf_ticket = c6.text_input("Ticket (optional)", key="nf_ticket")
            if st.form_submit_button("Record finding", key="nf_submit"):
                try:
                    client.create_finding({
                        "week": nf_week, "region": nf_region, "error_code": nf_code, "reason": nf_reason,
                        "brand": nf_brand or None, "skus": [x.strip() for x in nf_skus.split(",") if x.strip()],
                        "root_cause": nf_cause, "owner": nf_owner or None, "ticket": nf_ticket or None})
                    flash("success", "Finding recorded.")
                    st.rerun()
                except ApiError as e:
                    st.error(f"Could not record the finding: {e.detail}")
    else:
        st.caption('Switch role to "analyst" in the sidebar to record or resolve findings.')
