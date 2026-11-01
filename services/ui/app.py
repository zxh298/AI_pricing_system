"""Streamlit UI for the weekly triage and the diagnostic assistant (build steps 7 and 10).

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

ROLES = ["viewer", "stakeholder", "analyst"]
REJECTIONS = {"The cause is wrong": "wrong_cause", "We are the wrong owner": "wrong_owner",
              "We cannot do this action": "action_refused"}
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
    st.session_state.role = st.selectbox("Role", ROLES, index=ROLES.index(st.session_state.role), key="role_select")
    st.caption("A stand-in for IAP locally; the model never sees this or decides what you may see.")
    api_url = st.text_input("diagnostic-api URL", load_config().diagnostic_api_url, key="api_url_input")

client = _client(api_url, st.session_state.user, None if st.session_state.role == "viewer" else st.session_state.role)

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

show_flash()
tab_triage, tab_chat, tab_findings = st.tabs(["Weekly Triage", "Chat", "Findings"])


def ask_about(case: dict) -> None:
    """Carry a case into chat as a first question."""
    if st.session_state.session is None:
        st.session_state.session = client.create_session(case["week"])
    question = (f"Case {case['case_id']}: {case['error_code']} {case['reason']} in {'/'.join(case['regions'])}"
                f" for skus {', '.join(sorted({r['sku'] for r in case['records']})[:10])}. What is the current status?")
    client.post_message(st.session_state.session["session_id"], question)


def show_case(c: dict) -> None:
    title = f"#{c['case_id']} · {c['error_code']}" + (f":{c['reason']}" if c["reason"] else "")
    scope = f"{c['record_count']} records · {'/'.join(c['regions'])}" + (f" · {c['brand']}" if c["brand"] else "")
    with st.expander(f"{title} · {c['confidence']} · {c['owner']} · {c['status']} · {scope}"):
        if c.get("partial"):
            st.info("You can see only part of this case, because it covers regions outside your access.")
        label = {"CONFIRMED": "Reason", "LIKELY": "Possible reason, not proven", "UNCLASSIFIED": "Evidence summary"}
        if c["proposed_reason"]:
            st.markdown(f"**{label[c['confidence']]}:** {c['proposed_reason']}")
        else:
            st.caption("No proposal: the case went to the development team with its evidence.")
        if c["proposed_action"]:
            st.markdown(f"**Proposed action:**\n\n{c['proposed_action']}")
        if c["do_not"]:
            st.warning(c["do_not"])
        if c["doc_id"]:
            st.caption(f"Playbook {c['doc_id']}")
        run = c.get("investigation") or {}
        if run.get("status") in ("over_budget", "held_by_breaker", "no_llm", "failed", "step_limit", "refused"):
            st.caption(f"Not investigated: {run['status']}")
        for found in run.get("causes", []):
            st.markdown(f"**Evidence for {found['cause']}:**\n\n" + "\n".join(f"- {line}" for line in found["evidence"]))
        if run.get("dropped"):
            st.warning("Dropped by code, no signal behind them: " + ", ".join(d["cause"] for d in run["dropped"]))
        if run.get("unverified"):
            st.warning("Quotes values not found in the evidence: " + ", ".join(run["unverified"]))
        for line in run.get("ruled_out", []):
            st.caption(f"Ruled out: {line}")
        for line in run.get("not_verified", []):
            st.caption(f"Not verified: {line}")
        if run.get("trace"):
            st.caption("Steps: " + " → ".join(t["tool"] for t in run["trace"]))
        st.dataframe([{k: r.get(k) for k in ("sku", "pack_qty", "region", "detail")} for r in c["records"][:50]],
                     width='stretch', hide_index=True)

        if st.button("Ask in Chat", key=f"ask-{c['case_id']}"):
            try:
                ask_about(c)
                flash("info", "Asked in the Chat tab.")
                st.rerun()
            except ApiError as e:
                st.error(e.detail)
        if st.session_state.role == "viewer" or c["status"] not in ("open", "escalated") or c.get("partial"):
            return
        note = st.text_input("Note", key=f"note-{c['case_id']}")
        left, middle, right = st.columns(3)
        try:
            if left.button("Accept", key=f"accept-{c['case_id']}", width='stretch'):
                client.accept_case(c["case_id"], note or None)
                flash("success", f"Case #{c['case_id']} accepted. It closes when a re-scan no longer finds it.")
                st.rerun()
            if middle.button("Waive this week", key=f"waive-{c['case_id']}", width='stretch', disabled=not note):
                client.waive_case(c["case_id"], note)
                flash("success", f"Case #{c['case_id']} waived.")
                st.rerun()
            why = right.selectbox("Reject because", list(REJECTIONS), key=f"why-{c['case_id']}")
            new_owner = right.text_input("New owner", key=f"owner-{c['case_id']}") if REJECTIONS[why] == "wrong_owner" else None
            if right.button("Reject", key=f"reject-{c['case_id']}", width='stretch'):
                client.reject_case(c["case_id"], REJECTIONS[why], note or None, new_owner or None)
                flash("success", f"Case #{c['case_id']} rejected.")
                st.rerun()
        except ApiError as e:
            st.error(e.detail)


# ---------------- weekly triage ----------------
with tab_triage:
    st.subheader("Weekly triage")
    t_week = st.text_input("Week", value=load_config().default_week, key="t_week")
    try:
        scans = client.list_scans(t_week or None)
        cases = client.list_cases(t_week or None, "all")
        sent = client.list_notifications(t_week or None)
    except ApiError as e:
        st.error(f"Could not load the triage: {e.detail}")
        scans, cases, sent = [], [], []
    if scans:
        last = scans[0]
        s = last["summary"]
        if last["status"] == "BREAKER":
            st.error("Mass failure: treated as one incident. Owners were not paged and no LLM ran.")
        elif last["status"] == "INCIDENT":
            st.error(s.get("detail", "The week could not be scanned."))
        else:
            one, two, three, four = st.columns(4)
            one.metric("Records checked", s["records_checked"])
            two.metric("Records failing", s["records_failing"])
            three.metric(f"Not selling ({last['outcome_week']})", s["outcome_flagged"])
            four.metric("LLM steps", s["llm_steps"])
    else:
        st.caption("No scan for this week yet. It runs after the weekly pipeline: python -m jobs.weekly_triage")
    active = [c for c in cases if c["status"] in ("open", "escalated")]
    for c in active:
        show_case(c)
    rest = [c for c in cases if c["status"] not in ("open", "escalated")]
    if rest:
        st.divider()
        st.caption("Decided or closed")
        for c in rest:
            show_case(c)
    if sent:
        with st.expander(f"Notifications sent ({len(sent)})"):
            for n in sent:
                st.markdown(f"**{n['team']}** · {n['kind']} · {n['subject']}")
                st.text(n["body"])

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
            if flags.get("intent"):
                st.caption(f"intent `{flags['intent']}` · route `{flags['route']}`")
            if flags.get("route") == "fallback":
                st.info("No fixed workflow covers this question; the answer comes from an open-ended investigation.")
            if flags.get("unverified"):
                st.warning("The last answer quotes values not found in the results it was written from: "
                           + ", ".join(flags["unverified"]))
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
                        st.session_state.last_flags = {"escalated": out["escalated"], "truncated": out["truncated"],
                                                       "intent": out.get("intent"), "route": out.get("route"),
                                                       "unverified": out.get("unverified", [])}
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
