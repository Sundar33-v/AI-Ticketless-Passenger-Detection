"""KPI cards: the Dashboard's five HTML KPI cards and the seven ``st.metric`` cards (4 + 3) of Reports."""

from __future__ import annotations

from typing import Optional

import streamlit as st

from app.analytics import TOTAL_PASSENGER_CAPTION, DashboardMetrics
from dashboard.components import cards
from dashboard.components.theme import KPI_SLUGS

EMPTY_MESSAGE = "No records yet – start detection or run python scripts/generate_dummy_data.py"
PERIOD_EMPTY_MESSAGE = ("No records in this period – choose All time under Filters & data, or run "
                        "python scripts/generate_dummy_data.py to move the demo data to today")
MODEL_LABELS = ("Accuracy Score", "Precision", "Recall (Sensitivity)", "F1-Score")
ICONS = {
    "Valid Tickets": ":material/check_circle:", "Invalid Tickets": ":material/cancel:",
    "Total Passenger": ":material/groups:", "Total Detection": ":material/person_search:",
    "Expired Tickets": ":material/schedule:", "Boarding Events": ":material/login:",
    "Tracking IDs": ":material/tag:", "Accuracy Score": ":material/verified:",
    "Precision": ":material/target:", "Recall (Sensitivity)": ":material/radar:", "F1-Score": ":material/balance:",
}


def metric_card(label: str, value, help_text: str) -> None:
    with st.container(key=f"kpi-{KPI_SLUGS[label]}"):
        st.metric(label, value, help=help_text, border=True, icon=ICONS[label])


def empty_message(has_records: bool) -> str:
    """The hint for an empty period: the database may still hold records in other periods."""
    return PERIOD_EMPTY_MESSAGE if has_records else EMPTY_MESSAGE


def render_metrics(metrics: DashboardMetrics, *, has_records: bool = False) -> None:
    """Exactly seven ``st.metric`` cards in the fixed order, then the Total Passenger definition."""
    cards_ = metrics.cards()
    with st.container(key="kpi-grid"):
        columns = list(st.columns(4)) + list(st.columns(3))
        for column, (label, value, help_text) in zip(columns, cards_):
            with column:
                metric_card(label, value, help_text)
    st.caption(TOTAL_PASSENGER_CAPTION)
    if metrics.is_empty():
        st.info(empty_message(has_records))


def render_kpi_row(current: DashboardMetrics, previous: Optional[DashboardMetrics], period_label: str,
                   *, has_records: bool = False) -> None:
    """The Dashboard's five KPI cards with "vs previous period" deltas; the empty hint below an empty row."""
    st.html(cards.kpi_cards_html(current, previous, period_label))
    if current.is_empty():
        st.info(empty_message(has_records))


def percent(value: Optional[float]) -> str:
    return "N/A" if value is None else f"{value * 100:.1f}%"
