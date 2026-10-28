"""Grouped conductor alerts with read state saved in alert_reads (no dataframes in this panel)."""

from __future__ import annotations

import sqlite3
from zoneinfo import ZoneInfo

import streamlit as st

from app.alert_service import AlertGroupsResult, group_records, outcome_label, unread_count
from app.analytics import Period
from dashboard.components import nav, notifications, shell
from dashboard.components.theme import md_escape

BOX = {"error": st.error, "warning": st.warning, "info": st.info}
SESSION_STATES = {"RUNNING": "Running", "STOPPED": "Stopped", "FAILED": "Failed"}


def _state(value: str) -> str:
    return SESSION_STATES.get(value) or outcome_label(value)


def render_alert_panel(conn: sqlite3.Connection, result: AlertGroupsResult, period: Period, tz: ZoneInfo, *,
                       key: str, max_items: int = 10) -> None:
    st.subheader("Verification alerts")
    groups = result.groups
    if not groups:
        st.caption("No verification alerts in this period.")
        return
    read = shell.read_state()
    unread = unread_count(groups, read)
    head, button = st.columns([3, 2], vertical_alignment="center")
    head.caption(f"{unread} unread · {_total_label(result)} total")
    if button.button("Mark all as read", key=f"{key}_read", disabled=not unread):
        message = notifications.mark_read(notifications.items_of(groups))
        if message:
            st.session_state[nav.TOAST_KEY] = message
        st.rerun()
    for group in groups[:max_items]:
        parts = [md_escape(group.title), f"{group.last_at.astimezone(tz):%H:%M}"]
        if group.track_id is not None:
            parts.append(f"Track ID {group.track_id}")
        if group.source == "validation":
            parts.append(outcome_label(group.outcome))
        if group.count > 1:
            parts.append(f"×{group.count}")
        BOX[group.severity](" · ".join(parts) + "  \n" + md_escape(group.message))
        with st.expander("Details"):
            records = group_records(conn, group, period, limit=20)
            lines = [f"- {r.at.astimezone(tz):%H:%M:%S} · {r.record_id} · {md_escape(_state(r.state))}"
                     for r in records]
            if group.count > len(records):
                lines.append(f"- … and {group.count - len(records)} earlier records")
            st.markdown("\n".join(lines))
    if len(groups) > max_items:
        more = len(groups) - max_items
        if shell.under_router() and key != "live_alerts":
            st.page_link(nav.PAGE_LIVE, label=f"{more} more alerts – see Live Feed", icon=":material/videocam:")
        else:
            st.caption(f"{more} more alerts not shown.")


def _total_label(result: AlertGroupsResult) -> str:
    return "1000+" if result.truncated else str(len(result.groups))
