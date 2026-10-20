"""The seven primary metric cards (4 + 3 columns) and the Total Passenger caption."""

from __future__ import annotations

import streamlit as st

from app.analytics import TOTAL_PASSENGER_CAPTION, DashboardMetrics

EMPTY_MESSAGE = "No records yet – start detection or run python scripts/generate_dummy_data.py"


def render_metrics(metrics: DashboardMetrics) -> None:
    """Exactly seven ``st.metric`` cards in the fixed order, then the Total Passenger definition."""
    cards = metrics.cards()
    columns = list(st.columns(4)) + list(st.columns(3))
    for column, (label, value, help_text) in zip(columns, cards):
        column.metric(label, value, help=help_text, border=True)
    st.caption(TOTAL_PASSENGER_CAPTION)
    if metrics.is_empty():
        st.info(EMPTY_MESSAGE)
