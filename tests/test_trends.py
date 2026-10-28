"""app/trends.py and app/tracking_queries.py against temp SQLite databases."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import generate_dummy_data
from app.analytics import Period, compute_metrics, period_for
from app.database import Database, to_iso
from app.passenger_service import DetectionRow, insert_boarding, insert_detections, insert_session
from app.tracking_queries import live_tracks, recent_sessions, track_history
from app.trends import (SERIES, TrendTooLargeError, _floor_minute, bucket_size, detection_trend, failure_reasons,
                        minute_trend, outcome_distribution)

IST = ZoneInfo("Asia/Kolkata")
DAY = datetime(2026, 1, 15).date()


def local(hour, minute=0, day=DAY):
    return datetime.combine(day, time(hour, minute), tzinfo=IST)


@pytest.fixture
def conn(db):
    c = db.connect()
    try:
        yield c
    finally:
        c.close()


def session(conn, sid="s1", status="STOPPED", start=None, **kw):
    insert_session(conn, session_id=sid, source_type="video", source_label="clip.mp4", status=status,
                   started_at=start or local(7), station="City Hospital", trip_id="T", **kw)


def det(conn, sid, track, at):
    insert_detections(conn, [DetectionRow(sid, track, int(at.timestamp()) % 100000, at, 0.8, 0, 0, 10, 10, False)])


def series(df, name):
    return list(df[df["series"] == name]["count"])


def test_bucket_size_rule():
    t = local(0)
    assert bucket_size(t, t + timedelta(hours=48)) == "hour"
    assert bucket_size(t, t + timedelta(hours=49)) == "day"
    assert bucket_size(t, t + timedelta(days=14)) == "day"
    assert bucket_size(t, t + timedelta(days=15)) == "week"


def test_today_hour_buckets_local_and_zero_filled(conn):
    session(conn)
    det(conn, "s1", 1, local(7, 5))
    det(conn, "s1", 1, local(7, 40))       # same person twice in the bucket -> 1
    det(conn, "s1", 2, local(7, 50))
    det(conn, "s1", 2, local(9, 10))
    b = insert_boarding(conn, session_id="s1", track_id=1, boarded_at=local(7, 6), direction="top_to_bottom",
                        station=None, trip_id="T")
    insert_boarding(conn, session_id="s1", track_id=2, boarded_at=local(9, 11), direction="top_to_bottom",
                    station=None, trip_id="T")
    conn.execute("UPDATE boarding_events SET verification_status = 'VALID' WHERE boarding_id = ?", (b,))
    now = local(10, 30)
    df = detection_trend(conn, period_for("today", IST, now), IST, now)
    buckets = sorted(set(df["bucket"]))
    assert buckets[0] == local(0) and buckets[-1] == local(10) and len(buckets) == 11  # ends at "now"
    persons = dict(zip(df[df["series"] == SERIES[0]]["bucket"], series(df, SERIES[0])))
    assert persons[local(7)] == 2 and persons[local(8)] == 0 and persons[local(9)] == 1
    unverified = dict(zip(df[df["series"] == SERIES[2]]["bucket"], series(df, SERIES[2])))
    assert unverified[local(7)] == 0 and unverified[local(9)] == 1
    assert sum(series(df, SERIES[1])) == 2


def test_week_buckets_start_on_local_monday(conn):
    session(conn)
    det(conn, "s1", 1, local(0, 10))                       # Thu 15 Jan 00:10 IST (still 14 Jan in UTC)
    now = local(12, day=DAY + timedelta(days=30))
    df = detection_trend(conn, Period(None, None), IST, now)
    first = min(df["bucket"])
    assert first == local(0, day=DAY - timedelta(days=3)) and first.weekday() == 0
    assert series(df, SERIES[0])[0] == 1


def test_empty_db_gives_empty_frames(conn):
    now = local(12)
    assert detection_trend(conn, Period(None, None), IST, now).empty
    assert outcome_distribution(conn, Period()).empty
    assert failure_reasons(conn, Period()).empty
    assert not detection_trend(conn, period_for("today", IST, now), IST, now).empty  # zero-filled day


def test_trend_cap(conn, monkeypatch):
    import app.trends as trends

    session(conn)
    det(conn, "s1", 1, local(7))
    det(conn, "s1", 2, local(7, 1))
    monkeypatch.setattr(trends, "TREND_ROW_CAP", 1)
    with pytest.raises(TrendTooLargeError):
        detection_trend(conn, Period(), IST, local(12))


def test_distribution_matches_kpi_cards_on_demo_db(tmp_path):
    path = tmp_path / "demo.db"
    assert generate_dummy_data.main(["--db", str(path), "--date", DAY.isoformat(), "--no-qr-images"]) == 0
    c = Database(path).read_connection()
    try:
        period = period_for("today", IST, local(23))
        metrics = compute_metrics(c, period)
        dist = outcome_distribution(c, period).set_index("outcome")
        reasons = failure_reasons(c, period)
    finally:
        c.close()
    assert dist.loc["VALID", "count"] == metrics.valid
    assert dist.loc["EXPIRED", "count"] == metrics.expired_tickets
    assert dist["percent"].sum() == pytest.approx(100, abs=0.5)
    assert set(reasons["outcome"]) <= {"INVALID", "EXPIRED", "WRONG_ROUTE", "ALREADY_USED"} and len(reasons)


# ------------------------------------------------------------------ 10-minute trend
def val(conn, ref, outcome, at):
    conn.execute("INSERT INTO validation_events (validated_at, outcome, reason, credential_type, presented_ref, "
                 "channel) VALUES (?, ?, 'test', 'ticket_id', ?, 'cli')", (to_iso(at), outcome, ref))


def test_floor_minute_is_utc_and_rejects_naive():
    floored = _floor_minute(datetime(2026, 1, 15, 10, 7, 42, 123456, tzinfo=IST))
    assert floored == datetime(2026, 1, 15, 4, 37, tzinfo=timezone.utc) and floored.tzinfo is timezone.utc
    with pytest.raises(ValueError):
        _floor_minute(datetime(2026, 1, 15, 10, 7))


def test_minute_trend_empty_db_is_eleven_zero_rows(conn):
    now = local(10, 7) + timedelta(seconds=42)
    df = minute_trend(conn, now, IST)
    assert list(df.columns) == ["minute", "total", "ticketless"] and len(df) == 11
    assert df["total"].tolist() == [0] * 11 and df["ticketless"].tolist() == [0] * 11
    assert all(isinstance(v, int) for v in df["total"].tolist() + df["ticketless"].tolist())
    minutes = list(df["minute"])
    assert minutes[0] == local(9, 57) and minutes[-1] == local(10, 7)
    assert all(str(m.tzinfo) == "Asia/Kolkata" for m in minutes)
    assert all(b - a == timedelta(minutes=1) for a, b in zip(minutes, minutes[1:]))


def test_minute_trend_counts_distinct_tracks_and_failed_checks_only(conn):
    session(conn, status="RUNNING", start=local(9))
    session(conn, sid="s2", status="RUNNING", start=local(9))
    now = local(10, 7) + timedelta(seconds=30)
    det(conn, "s1", 1, local(10, 5) + timedelta(seconds=1))
    det(conn, "s1", 1, local(10, 5) + timedelta(seconds=40))   # same person in the minute -> 1
    det(conn, "s1", 2, local(10, 5) + timedelta(seconds=50))
    det(conn, "s2", 1, local(10, 5) + timedelta(seconds=55))   # same Track ID, other session -> counted
    det(conn, "s1", 3, local(10, 7) + timedelta(seconds=10))   # current (partial) minute
    det(conn, "s1", 4, local(9, 56) + timedelta(seconds=59))   # before the window
    det(conn, "s1", 5, now + timedelta(seconds=1))             # after now
    for ref, outcome in (("A", "INVALID"), ("A", "INVALID"), ("B", "WRONG_ROUTE"), ("C", "ALREADY_USED"),
                         ("D", "EXPIRED"), ("E", "VERIFICATION_REQUIRED"), ("F", "VALID")):
        val(conn, ref, outcome, local(10, 6) + timedelta(seconds=5))
    val(conn, "G", "INVALID", local(9, 50))                    # outside the window
    insert_boarding(conn, session_id="s1", track_id=2, boarded_at=local(10, 6), direction="top_to_bottom",
                    station=None, trip_id="T")                  # unverified boarding: never ticketless
    df = minute_trend(conn, now, IST).set_index("minute")
    assert df.loc[local(10, 5), "total"] == 3 and df.loc[local(10, 7), "total"] == 1
    assert df.loc[local(10, 6), "ticketless"] == 3             # A once, B, C; not D, E, F
    assert int(df["total"].sum()) == 4 and int(df["ticketless"].sum()) == 3


def test_live_tracks_and_history(conn):
    now = local(8)
    session(conn, status="RUNNING", start=local(7, 50))
    det(conn, "s1", 1, now - timedelta(seconds=5))
    det(conn, "s1", 2, now - timedelta(minutes=5))       # outside the window
    insert_boarding(conn, session_id="s1", track_id=1, boarded_at=now - timedelta(seconds=4),
                    direction="top_to_bottom", station=None, trip_id="T")
    df = live_tracks(conn, "s1", now, 30)
    assert list(df["track_id"]) == [1] and df.iloc[0]["verification_status"] == "VERIFICATION_REQUIRED"
    hist = track_history(conn, "s1", 1)
    assert hist["samples"] == 1 and hist["verification_status"] == "VERIFICATION_REQUIRED"
    assert track_history(conn, "s1", 99) is None


def test_recent_sessions_period_rule_and_states(conn):
    now = local(12)
    today = period_for("today", IST, now)
    y = DAY - timedelta(days=1)
    session(conn, "span", start=local(23, 30, day=y))
    conn.execute("UPDATE detection_sessions SET ended_at = ?, last_heartbeat_at = ? WHERE session_id = 'span'",
                 (local(0, 20).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),) * 2)
    session(conn, "old", start=local(8, day=y))
    session(conn, "stale", status="RUNNING", start=local(6, day=y - timedelta(days=1)))
    df = recent_sessions(conn, today, now=now, stale_sec=10)
    assert set(df["session_id"]) == {"span", "stale"}
    assert dict(zip(df["session_id"], df["state"])) == {"span": "Stopped", "stale": "Not responding"}
