"""dashboard/components/charts.py: the pure Plotly figures of the Dashboard."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from dashboard.components import cards, charts, theme

IST = ZoneInfo("Asia/Kolkata")
START = datetime(2026, 1, 15, 4, 30, tzinfo=timezone.utc).astimezone(IST)  # 10:00 IST


def trend(total, ticketless=None):
    ticketless = ticketless if ticketless is not None else [0] * len(total)
    return pd.DataFrame({"minute": [START + timedelta(minutes=i) for i in range(len(total))],
                         "total": total, "ticketless": ticketless})


def test_trend_traces_fill_and_axes():
    fig = charts.trend_figure(trend([0, 1, 2, 3, 1, 0, 0, 1, 2, 0, 1], [0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0]))
    assert [t.name for t in fig.data] == ["Total", theme.TICKETLESS_SERIES]
    total, failed = fig.data
    assert total.fill == "tozeroy" and total.fillgradient.type == "vertical" and failed.fill is None
    assert total.line.color == theme.PRIMARY and failed.line.color == theme.ERROR
    assert total.line.shape == "spline" and failed.line.shape == "spline" and total.mode == "lines+markers" and total.marker.size == 6
    assert fig.layout.hovermode == "x unified" and fig.layout.xaxis.tickformat == "%H:%M"
    assert fig.layout.xaxis.dtick == 120000 and fig.layout.height == 230
    assert fig.layout.paper_bgcolor == "rgba(0,0,0,0)"
    assert pd.Timestamp(total.x[0]) == pd.Timestamp("2026-01-15 10:00")  # local wall-clock time, tz stripped


@pytest.mark.parametrize("values, dtick, top", [([0] * 11, 1, 1), ([0, 3, 1] + [0] * 8, 1, 3),
                                                ([12] + [0] * 10, 3, 12)])
def test_trend_integer_y_ticks(values, dtick, top):
    y = charts.trend_figure(trend(values)).layout.yaxis
    assert isinstance(y.dtick, int) and y.dtick == dtick and y.dtick >= 1
    assert y.tickmode == "linear" and y.tick0 == 0 and y.tickformat == "d"
    assert tuple(y.range) == pytest.approx((0, top * 1.15))


def test_trend_top_uses_both_series():
    y = charts.trend_figure(trend([1] * 11, [8] + [0] * 10)).layout.yaxis
    assert y.dtick == 2 and tuple(y.range) == pytest.approx((0, 8 * 1.15))


def distribution(**counts):
    return pd.DataFrame([(o, o, n, 0.0) for o, n in counts.items()], columns=["outcome", "label", "count", "percent"])


def test_donut_figure_hole_colours_and_total():
    fig = charts.donut_figure(cards.donut_counts(distribution(VALID=12, INVALID=4, EXPIRED=1,
                                                              VERIFICATION_REQUIRED=1)))
    pie = fig.data[0]
    assert pie.hole == pytest.approx(.68) and pie.sort is False and pie.direction == "clockwise"
    assert list(pie.labels) == ["Valid", "Invalid", "Expired", "Unverified"] and list(pie.values) == [12, 4, 1, 1]
    assert list(pie.marker.colors) == [theme.SUCCESS, theme.ERROR, theme.WARNING, theme.PURPLE]
    assert pie.marker.line.color == theme.CARD and pie.textinfo == "none"
    text = fig.layout.annotations[0].text
    assert ">Total<" in text and "<b style='font-size:22px'>18</b>" in text
    assert fig.layout.showlegend is False and fig.layout.height == 200


def test_donut_figure_grey_ring_when_empty():
    fig = charts.donut_figure(cards.donut_counts(pd.DataFrame(columns=["outcome", "label", "count", "percent"])))
    pie = fig.data[0]
    assert list(pie.values) == [1] and list(pie.marker.colors) == [charts.EMPTY_RING]
    assert pie.hoverinfo == "skip"
    assert "<b style='font-size:22px'>0</b>" in fig.layout.annotations[0].text
