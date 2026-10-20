"""Sidebar filters: reporting period (all seven metrics) and validation status (tables only)."""

from __future__ import annotations

from datetime import datetime
from typing import List, Tuple
from zoneinfo import ZoneInfo

import streamlit as st

from app.alert_service import outcome_label
from app.analytics import OUTCOMES, PERIOD_CHOICES, Period, period_for
from app.settings_schema import AppConfig

PERIOD_LABELS = {"today": "Today", "yesterday": "Yesterday", "last_7_days": "Last 7 days", "all": "All time",
                 "custom": "Custom range"}


def select_period(cfg: AppConfig, now: datetime, *, key: str = "period") -> Period:
    """Period selectbox in the sidebar (default ``dashboard.default_period``)."""
    default = PERIOD_CHOICES.index(cfg.dashboard.default_period)
    choice = st.sidebar.selectbox("Period", PERIOD_CHOICES, index=default, format_func=PERIOD_LABELS.get,
                                  key=key)
    custom = None
    if choice == "custom":
        today = now.astimezone(ZoneInfo(cfg.app.timezone)).date()
        picked = st.sidebar.date_input("Date range", value=(today, today), key=f"{key}_range")
        dates = list(picked) if isinstance(picked, (list, tuple)) else [picked]
        dates = dates or [today]
        custom = (dates[0], dates[-1])
    return period_for(choice, cfg.app.timezone, now, custom=custom)


def select_statuses(*, key: str = "statuses") -> List[str]:
    """Validation-status multiselect (default: all six outcomes)."""
    return st.sidebar.multiselect("Validation status (tables)", OUTCOMES, default=list(OUTCOMES),
                                  format_func=outcome_label, key=key,
                                  help="Filters the two tables only; the metrics always count every outcome.")


def render_sidebar(cfg: AppConfig, now: datetime) -> Tuple[Period, List[str]]:
    st.sidebar.header("Filters")
    period = select_period(cfg, now)
    statuses = select_statuses()
    return period, statuses
