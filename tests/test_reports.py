"""app/reports.py: summary numbers, exports and CSV safety."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import generate_dummy_data
from app.analytics import compute_metrics, period_for, recent_validations
from app.database import Database
from app.reports import (boardings_export, csv_bytes, period_label, report_summary, summary_frame,
                         validations_export)

IST = ZoneInfo("Asia/Kolkata")
DAY = datetime(2026, 1, 15).date()
NOW = datetime.combine(DAY, time(23), tzinfo=IST)


@pytest.fixture
def demo(tmp_path):
    path = tmp_path / "demo.db"
    assert generate_dummy_data.main(["--db", str(path), "--date", DAY.isoformat(), "--no-qr-images"]) == 0
    conn = Database(path).read_connection()
    try:
        yield conn
    finally:
        conn.close()


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r"])
def test_csv_formula_neutralisation(prefix):
    data = csv_bytes(pd.DataFrame({"a": [prefix + "1+1"], "n": [5]}))
    assert data.startswith(b"\xef\xbb\xbf")
    text = data.decode("utf-8-sig")
    assert ("'" + prefix + "1+1") in text and "5" in text and "index" not in text.splitlines()[0]


def test_summary_matches_compute_metrics(demo):
    period = period_for("today", IST, NOW)
    summary = report_summary(demo, period, IST, NOW)
    assert summary.metrics == compute_metrics(demo, period)
    frame = dict(summary_frame(summary).itertuples(index=False))
    assert frame["Valid Tickets"] == str(summary.metrics.valid)
    assert frame["Report period"] == period_label(period, IST) and "Asia/Kolkata" in frame["Report period"]


def test_validations_export_respects_period_and_status(demo):
    period = period_for("today", IST, NOW)
    df, over = validations_export(demo, period, ["EXPIRED"])
    assert not over and set(df["outcome"]) == {"EXPIRED"}
    assert len(df) == len(recent_validations(demo, period, ["EXPIRED"], limit=1000))
    everything, _ = validations_export(demo, period_for("all", IST, NOW), None)
    assert len(everything) >= len(validations_export(demo, period, None)[0])
    boardings, over = boardings_export(demo, period, None)
    assert not over and len(boardings) == compute_metrics(demo, period).boarding_events


def test_export_cap(demo, monkeypatch):
    import app.reports as reports

    monkeypatch.setattr(reports, "EXPORT_CAP", 1)
    df, over = validations_export(demo, period_for("all", IST, NOW), None)
    assert over and df.empty
