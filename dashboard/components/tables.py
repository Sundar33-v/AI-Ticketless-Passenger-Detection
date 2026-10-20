"""Recent boarding events and recent validation results with display names and local times."""

from __future__ import annotations

from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from app.alert_service import outcome_label
from app.database import parse_iso

CHANNEL_LABELS = {"camera": "Camera", "dashboard": "Ticket Check", "cli": "Command line", "demo": "Demo"}


def local_time(value: Optional[str], tz: ZoneInfo) -> str:
    return parse_iso(value).astimezone(tz).strftime("%Y-%m-%d %H:%M:%S") if value else ""


def _text(value) -> str:
    return "" if value is None or (isinstance(value, float) and pd.isna(value)) else str(value)


def _track(value) -> str:
    return "" if value is None or pd.isna(value) else str(int(value))


def boarding_table(df: pd.DataFrame, tz: ZoneInfo) -> pd.DataFrame:
    rows = [{
        "Time (local)": local_time(r.boarded_at, tz),
        "Track ID": _track(r.track_id),
        "Session": _text(r.session_id)[:8],
        "Station": _text(r.station),
        "Verification status": outcome_label(r.verification_status),
        "Ticket": _text(r.ticket_id),
    } for r in df.itertuples(index=False)]
    return pd.DataFrame(rows, columns=["Time (local)", "Track ID", "Session", "Station", "Verification status",
                                       "Ticket"])


def validation_table(df: pd.DataFrame, tz: ZoneInfo) -> pd.DataFrame:
    rows = [{
        "Time (local)": local_time(r.validated_at, tz),
        "Outcome": outcome_label(r.outcome),
        "Reason": _text(r.reason),
        "Ticket": _text(r.ticket_id) or _text(r.presented_ref),
        "Track ID": _track(r.track_id),
        "Channel": CHANNEL_LABELS.get(r.channel, _text(r.channel)),
        "Synthetic": "Yes" if int(r.is_synthetic) else "No",
    } for r in df.itertuples(index=False)]
    return pd.DataFrame(rows, columns=["Time (local)", "Outcome", "Reason", "Ticket", "Track ID", "Channel",
                                       "Synthetic"])


def render_boardings(df: pd.DataFrame, tz: ZoneInfo) -> None:
    st.subheader("Recent boarding events")
    if df.empty:
        st.caption("No boarding events in this period.")
        return
    st.dataframe(boarding_table(df, tz), hide_index=True, width="stretch")


def render_validations(df: pd.DataFrame, tz: ZoneInfo) -> None:
    st.subheader("Recent validation results")
    if df.empty:
        st.caption("No validation results in this period.")
        return
    st.dataframe(validation_table(df, tz), hide_index=True, width="stretch")
