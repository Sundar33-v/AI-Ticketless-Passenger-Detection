"""Dashboard charts (real data only, nothing fabricated) with explicit empty states.

``trend_figure`` / ``donut_figure`` are pure Plotly figures; ``render_trend_card``
and ``render_status_card`` draw them into the Dashboard's middle-row cards.
``legend_frame`` serves the Reports page.
"""

from __future__ import annotations

import math
from typing import Sequence

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard.components import cards, icons, theme
from dashboard.components.cards import DonutSlice

EMPTY_RING = "rgba(148,163,184,0.25)"  # MUTED at 25 % opacity
_GRID = "rgba(148,163,184,.10)"
_TRANSPARENT = "rgba(0,0,0,0)"
TREND_TITLE = "Passenger Trend (Last 10 Min)"
TREND_EMPTY = "No detections or failed checks in the last 10 minutes."
STATUS_TITLE = "Ticket Status"
DISTRIBUTION_EMPTY = "No ticket checks in this period."
PLOTLY_CONFIG = {"displayModeBar": False}


def trend_figure(df: pd.DataFrame) -> go.Figure:
    """10-minute trend (``app.trends.minute_trend`` frame): Total area with a gradient + Ticketless line."""
    minute = pd.to_datetime(df["minute"])
    x = minute.dt.tz_localize(None) if minute.dt.tz is not None else minute  # local wall-clock time
    values = df[["total", "ticketless"]].to_numpy()
    top = max(1, int(values.max())) if values.size else 1
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=x, y=df["total"], name="Total", mode="lines+markers", line_shape="spline",
        line=dict(color=theme.PRIMARY, width=2.5), marker=dict(size=6, color=theme.PRIMARY), fill="tozeroy",
        fillgradient=dict(type="vertical", colorscale=[[0, "rgba(47,107,255,0)"], [1, "rgba(47,107,255,0.35)"]]),
        hovertemplate="%{y}"))
    fig.add_trace(go.Scatter(
        x=x, y=df["ticketless"], name=theme.TICKETLESS_SERIES, mode="lines+markers", line_shape="spline",
        line=dict(color=theme.ERROR, width=2), marker=dict(size=6, color=theme.ERROR), hovertemplate="%{y}"))
    fig.update_layout(
        hovermode="x unified", paper_bgcolor=_TRANSPARENT, plot_bgcolor=_TRANSPARENT,
        font=dict(color=theme.MUTED, size=11), height=230, margin=dict(l=8, r=8, t=8, b=8),
        legend=dict(orientation="h", x=1, xanchor="right", y=1.15),
        xaxis=dict(tickformat="%H:%M", hoverformat="%H:%M", dtick=120000, gridcolor=_GRID, showgrid=True),
        yaxis=dict(rangemode="tozero", range=[0, top * 1.15], tickmode="linear", tick0=0,
                   dtick=max(1, math.ceil(top / 4)), tickformat="d", gridcolor=_GRID))
    return fig


def donut_figure(counts: Sequence[DonutSlice]) -> go.Figure:
    """Ticket Status donut with the total in the centre; a grey ring (no hover) when nothing was checked."""
    total = sum(int(s.count) for s in counts)
    if total:
        pie = go.Pie(labels=[s.label for s in counts], values=[int(s.count) for s in counts], hole=.68,
                     sort=False, direction="clockwise", textinfo="none",
                     marker=dict(colors=[s.colour for s in counts], line=dict(color=theme.CARD, width=2)),
                     hovertemplate="%{label}: %{value} (%{percent})<extra></extra>")
    else:
        pie = go.Pie(labels=["No checks"], values=[1], hole=.68, sort=False, textinfo="none", hoverinfo="skip",
                     marker=dict(colors=[EMPTY_RING], line=dict(color=theme.CARD, width=2)))
    fig = go.Figure(pie)
    fig.update_layout(
        showlegend=False, paper_bgcolor=_TRANSPARENT, plot_bgcolor=_TRANSPARENT, height=200,
        margin=dict(l=0, r=0, t=0, b=0), font=dict(color=theme.TEXT),
        annotations=[dict(text=f"<span style='font-size:12px;color:{theme.MUTED}'>Total</span><br>"
                               f"<b style='font-size:22px'>{total}</b>", x=.5, y=.5, showarrow=False,
                          font=dict(color=theme.TEXT))])
    return fig


def _title(text: str, svg: str = "") -> None:
    st.html(cards.title_html(text, svg))


def render_trend_card(df: pd.DataFrame) -> None:
    """Card body: title, the 10-minute Plotly trend and an empty caption when nothing happened."""
    _title(TREND_TITLE, icons.BAR_CHART)
    st.plotly_chart(trend_figure(df), width="stretch", theme=None, config=PLOTLY_CONFIG, key="dash_trend")
    if int(df[["total", "ticketless"]].to_numpy().sum()) == 0:
        st.caption(TREND_EMPTY)


def render_status_card(distribution: pd.DataFrame) -> None:
    """Card body: title, donut (left) + legend (right); the distinct-credential counts of the period."""
    _title(STATUS_TITLE, icons.QR_SCAN)
    counts = cards.donut_counts(distribution)
    chart_col, legend_col = st.columns([1.15, 1], vertical_alignment="center")
    with chart_col:
        st.plotly_chart(donut_figure(counts), width="stretch", theme=None, config=PLOTLY_CONFIG, key="dash_donut")
    with legend_col:
        st.html(cards.legend_html(counts))
    if sum(int(s.count) for s in counts) == 0:
        st.caption(DISTRIBUTION_EMPTY)


def legend_frame(df: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({"Outcome": df["label"], "Checks": df["count"], "Share (%)": df["percent"]})
