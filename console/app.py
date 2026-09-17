"""The console: read the morning's brief, and decide what to do about it.

This is where the human approval in principle 5 actually happens, so the page is built around one question per
action — approve, edit, reject — and refuses to make any of them easy to answer without reading. Every item
shows the number, what it normally is, why it was flagged and where it came from, because an approval given
without the evidence is a rubber stamp with extra steps.

It talks to the API rather than the database. The API holds the permission filter, the policy gate and the
interrupt; a console with its own database connection would be a second place for all three to be wrong, and
the one people actually use.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

import httpx
import streamlit as st

API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8010")
API_KEY = os.environ.get("CONSOLE_API_KEY", "")
TIMEOUT_S = 120.0
SEVERITY_COLOURS = {"HIGH": "🔴", "WARN": "🟠"}
STATUS_WORDS = {
    "PROPOSED": "waiting for you",
    "EXECUTED": "done",
    "REJECTED": "rejected",
    "BLOCKED": "blocked by policy",
    "SUPPRESSED": "suppressed as a duplicate",
    "FAILED_RETRYABLE": "failed — will retry",
    "FAILED": "failed — needs a person",
}


def api(method: str, path: str, **kwargs: Any) -> dict:
    """One place that talks to the API, so an error is shown once and in words."""
    try:
        response = httpx.request(method, f"{API_BASE_URL.rstrip('/')}{path}",
                                 headers={"X-API-Key": API_KEY}, timeout=TIMEOUT_S, **kwargs)
    except httpx.HTTPError as exc:
        st.error(f"Could not reach the API at {API_BASE_URL}: {exc}")
        st.stop()
    if response.status_code == 401:
        st.error("The console's API key was rejected. Set CONSOLE_API_KEY to a key the API knows.")
        st.stop()
    if response.status_code == 403:
        st.error(response.json().get("detail", "This key may not see that brief."))
        st.stop()
    if not response.is_success:
        st.error(f"The API returned {response.status_code}: {response.text[:300]}")
        st.stop()
    return dict(response.json())


def show_item(item: dict, facts: dict) -> None:
    """One incident, with the evidence a person needs to judge it."""
    mark = SEVERITY_COLOURS.get(item["severity"], "⚪")
    st.markdown(f"**{mark} {item['headline']}**")
    st.write(item["why"])
    with st.expander("The numbers behind it"):
        for claim in item["claims"]:
            fact = facts.get(claim["metric_ref"], {})
            st.markdown(f"- {claim['text']}  \n"
                        f"  <span style='color:#666;font-size:0.85em'>{claim['metric_ref']}"
                        f"{' — ' + fact.get('means', '') if fact else ''}</span>",
                        unsafe_allow_html=True)


def show_action(action: dict, thread_id: str) -> None:
    """One decision. Approve, edit or reject — and nothing happens until one of them is pressed."""
    status = action["status"]
    if status != "PROPOSED":
        note = action.get("blocked_reason") or action.get("external_ref") or ""
        st.info(f"**{action['title']}** — {STATUS_WORDS.get(status, status)}"
                + (f"  \n{note}" if note else ""))
        return

    with st.form(key=f"form-{action['id']}"):
        st.markdown(f"**Proposed: {action['type'].replace('_', ' ')}** → {action['assignee_hint']}")
        title = st.text_input("Title", value=action["title"], key=f"title-{action['id']}")
        body = st.text_area("Body", value=action["body"], height=220, key=f"body-{action['id']}")
        note = st.text_input("Note (optional, recorded with your decision)", key=f"note-{action['id']}")
        approve, edit, reject = st.columns(3)
        approved = approve.form_submit_button("Approve", type="primary")
        edited = edit.form_submit_button("Approve with my edits")
        rejected = reject.form_submit_button("Reject")

    if not (approved or edited or rejected):
        return
    verdict = "approve" if approved else ("edit" if edited else "reject")
    payload: dict[str, Any] = {"action_id": action["id"], "verdict": verdict, "note": note or None}
    if verdict == "edit":
        payload |= {"title": title, "body": body}
    with st.spinner("Sending your decision…"):
        api("POST", f"/brief/{thread_id}/decide", json={"decisions": [payload]})
    st.success(f"{verdict.title()}d. The brief has been updated.")
    st.rerun()


def main() -> None:
    st.set_page_config(page_title="Supply chain command center", page_icon="📋", layout="centered")

    if not API_KEY:
        st.warning("CONSOLE_API_KEY is not set, so this console cannot talk to the API.")
        st.stop()

    with st.sidebar:
        page = st.radio("Page", ["Morning brief", "Ops"], label_visibility="collapsed")
    if page == "Ops":
        st.title("Ops")
        ops_page()
        return

    st.title("Morning brief")
    with st.sidebar:
        st.header("Brief")
        run_date = st.date_input("Date", value=date.today())
        build = st.button("Build the brief", type="primary")
        st.caption(f"API: {API_BASE_URL}")
        st.caption("Which brief you see is decided by your API key, not by this page.")

    if build:
        with st.spinner("Computing metrics, detecting, narrating…"):
            st.session_state["brief"] = api("POST", "/brief/run",
                                            params={"run_date": run_date.isoformat()})
    brief = st.session_state.get("brief")
    if not brief:
        st.info("Pick a date and build the brief.")
        return

    if brief["thread_id"]:
        brief = api("GET", f"/brief/{brief['thread_id']}")        # always show what the server holds
        st.session_state["brief"] = brief

    st.caption(f"{brief['run_date']} · for {brief['audience']} · {brief['status'].replace('_', ' ').lower()}"
               f" · written by {brief['narrated_by'] or 'unknown'}")
    if brief["freshness_line"]:
        st.warning(brief["freshness_line"]) if "Stale" in brief["freshness_line"] \
            else st.caption(brief["freshness_line"])
    st.subheader("Summary")
    st.write(brief["summary"])

    facts: dict[str, dict] = {}     # the API returns claims already resolved; kept for clarity
    st.subheader("What changed")
    if not brief["items"]:
        st.info("Nothing above the reporting level for this date.")
    for item in brief["items"]:
        show_item(item, facts)
        st.divider()

    st.subheader("What to do about it")
    if not brief["actions"]:
        st.caption("No actions were proposed.")
    for action in brief["actions"]:
        show_action(action, brief["thread_id"])

    blocked = [a for a in brief["actions"] if a["status"] == "BLOCKED"]
    if blocked:
        st.caption("Policy refused these before you were asked — shown so the policy stays visible.")


if __name__ == "__main__":
    main()


def ops_page() -> None:
    """The ops view: is this working, and what needs a person?

    Kept in the same console as the brief on purpose. An operations page nobody opens is a page that is
    wrong for a month before anybody notices; this one is two clicks from where approvals happen.
    """
    summary = api("GET", "/ops/summary")

    if summary["alerts"]:
        for alert in summary["alerts"]:
            line = f"**{alert['name']}** — {alert['detail']}  \n_see docs/RUNBOOK.md § {alert['name']}_"
            st.error(line) if alert["severity"] == "HIGH" else st.warning(line)
    else:
        st.success("No alerts. Briefs are running, the model is being used, nothing is stuck.")

    left, middle, right = st.columns(3)
    left.metric("Briefs in window", sum(r["briefs"] for r in summary["narration"]) or 0)
    middle.metric("Waiting for approval", len(summary["pending_approvals"]))
    right.metric("Model cost (window)", f"${summary['cost_usd_window']:.4f}")

    st.subheader("Runs")
    st.dataframe(summary["runs"], width="stretch")

    if summary["narration"]:
        st.subheader("Narration")
        st.caption("How often the model's answer shipped as written, was corrected once, or was replaced "
                   "by the template.")
        st.dataframe(summary["narration"], width="stretch")

    if summary["model_usage"]:
        st.subheader("Model usage and cost")
        st.dataframe(summary["model_usage"], width="stretch")

    if summary["pending_approvals"]:
        st.subheader("Waiting for a person")
        st.dataframe(summary["pending_approvals"], width="stretch")

    if summary["action_failures"]:
        st.subheader("Actions that failed")
        st.caption("FAILED_RETRYABLE is picked up by the next run; FAILED needs somebody to look.")
        st.dataframe(summary["action_failures"], width="stretch")

    if summary["policy_refusals"]:
        st.subheader("What policy refused")
        st.caption("Shown so a gate that has drifted from what the business needs is visible.")
        st.dataframe(summary["policy_refusals"], width="stretch")

    if summary["delivery"]:
        st.subheader("Delivery")
        st.dataframe(summary["delivery"], width="stretch")
