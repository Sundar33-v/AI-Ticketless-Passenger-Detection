"""Boarding events and validation results with display names, local times and outcome badges."""

from __future__ import annotations

import sqlite3
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from app.alert_service import outcome_label
from app.analytics import Period, count_validations, recent_validations
from app.database import parse_iso
from dashboard.components import cards, icons, theme

CHANNEL_LABELS = {"camera": "Camera", "dashboard": "Ticket Checker", "cli": "Command line", "demo": "Demo"}
VALIDATION_COLUMNS = ["Time (Local)", "Outcome", "Reason", "Ticket ID", "Track ID", "Channel", "Synthetic"]
BOARDING_COLUMNS = ["Time (Local)", "Track ID", "Session", "Station", "Verification status", "Ticket"]


def local_time(value: Optional[str], tz: ZoneInfo) -> str:
    return parse_iso(value).astimezone(tz).strftime("%Y-%m-%d %H:%M:%S") if value else ""


def _text(value) -> str:
    return "" if value is None or (isinstance(value, float) and pd.isna(value)) else str(value)


def _track(value) -> str:
    return "" if value is None or pd.isna(value) else str(int(value))


def boarding_table(df: pd.DataFrame, tz: ZoneInfo) -> pd.DataFrame:
    rows = [{
        "Time (Local)": local_time(r.boarded_at, tz),
        "Track ID": _track(r.track_id),
        "Session": _text(r.session_id)[:8],
        "Station": _text(r.station),
        "Verification status": outcome_label(r.verification_status),
        "Ticket": _text(r.ticket_id),
    } for r in df.itertuples(index=False)]
    return pd.DataFrame(rows, columns=BOARDING_COLUMNS)


def validation_table(df: pd.DataFrame, tz: ZoneInfo) -> pd.DataFrame:
    rows = [{
        "Time (Local)": local_time(r.validated_at, tz),
        "Outcome": outcome_label(r.outcome),
        "Reason": _text(r.reason),
        "Ticket ID": _text(r.ticket_id) or _text(r.presented_ref),
        "Track ID": _track(r.track_id),
        "Channel": CHANNEL_LABELS.get(r.channel, _text(r.channel)),
        "Synthetic": "Yes" if int(r.is_synthetic) else "No",
    } for r in df.itertuples(index=False)]
    return pd.DataFrame(rows, columns=VALIDATION_COLUMNS)


def live_tracks_table(df: pd.DataFrame, tz: ZoneInfo) -> pd.DataFrame:
    rows = [{"Track ID": _track(r.track_id), "Last seen (local)": local_time(r.last_seen, tz),
             "Confidence": f"{float(r.confidence):.2f}",
             "Boarding": local_time(r.boarded_at, tz) if isinstance(r.boarded_at, str) else "Not boarded",
             "Verification status": outcome_label(r.verification_status)
             if isinstance(r.verification_status, str) else ""} for r in df.itertuples(index=False)]
    return pd.DataFrame(rows, columns=["Track ID", "Last seen (local)", "Confidence", "Boarding",
                                       "Verification status"])


def render_live_tracks(df: pd.DataFrame, tz: ZoneInfo) -> None:
    st.subheader("Live tracks")
    if df.empty:
        st.caption("No person tracked in the last few seconds.")
    else:
        st.dataframe(live_tracks_table(df, tz), hide_index=True, width="stretch")
    st.caption("Track IDs are assigned by ByteTrack per detection session and can change after occlusion.")


def styled(frame: pd.DataFrame, column: str):
    return frame.style.map(theme.badge_style, subset=[column])


def render_boardings(df: pd.DataFrame, tz: ZoneInfo) -> None:
    st.subheader("Recent boarding events")
    if df.empty:
        st.caption("No boarding events in this period.")
        return
    st.dataframe(styled(boarding_table(df, tz), "Verification status"), hide_index=True, width="stretch")


def render_validations(df: pd.DataFrame, tz: ZoneInfo) -> None:
    st.subheader("Recent validation results")
    if df.empty:
        st.caption("No validation results in this period.")
        return
    st.dataframe(styled(validation_table(df, tz), "Outcome"), hide_index=True, width="stretch")


VALIDATION_LIMITS = (10, 25, 50)
VALIDATION_EMPTY = "No validation results in this period."
SEARCH_TIP = "Matches ticket ID, Track ID or the stored channel code (camera, dashboard, cli, demo)."


def render_validation_card(conn: sqlite3.Connection, ctx, period: Period) -> None:
    """Dashboard card: the newest validations (Last 10/25/50, search) as an HTML table; no paging."""
    with st.container(border=True, key="card-validations"):
        title_col, limit_col, search_col = st.columns([2.2, 1, 1.6], vertical_alignment="center")
        with title_col:
            st.html(cards.title_html("Recent Validation Results", icons.LIST))
        limit = limit_col.selectbox("Records", VALIDATION_LIMITS, format_func=lambda n: f"Last {n} Records",
                                    key="dash_val_limit", label_visibility="collapsed")
        search = search_col.text_input("Search", key="dash_val_search", max_chars=64,
                                       placeholder="Search by ID / Ticket / Channel...",
                                       icon=":material/search:",
                                       label_visibility="collapsed", help=SEARCH_TIP)
        search = (search or "").strip()
        outcomes = list(ctx.statuses)
        df = recent_validations(conn, period, outcomes, int(limit), search=search or None)
        total = count_validations(conn, period, outcomes, search=search or None)
        empty_text = f"No results match “{search}”." if search else VALIDATION_EMPTY
        st.html(cards.validation_table_html(validation_table(df, ctx.tz), list(df["outcome"]),
                                            empty_text=empty_text))
        st.caption(f"Showing {len(df)} of {total} · newest first", help=SEARCH_TIP)
