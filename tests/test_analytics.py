"""Tests for app/analytics.py: periods, the seven metric queries, tables, M3 selector, status."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.analytics import (
    METRIC_HELP,
    TOTAL_PASSENGER_CAPTION,
    DashboardMetrics,
    Period,
    boardings_needing_resolution,
    compute_metrics,
    period_for,
    recent_boardings,
    recent_validations,
    source_status,
    synthetic_row_count,
)
from app.passenger_service import DetectionRow, insert_boarding, insert_detections, insert_session
from app.qr_service import encode_payload
from app.ticket_validator import TicketValidator, build_journey, issue_ticket

IST = ZoneInfo("Asia/Kolkata")
D = date(2026, 1, 15)


def local(hour, minute=0, day=D):
    return datetime.combine(day, time(hour, minute), tzinfo=IST)


@pytest.fixture
def conn(db):
    c = db.connect()
    try:
        yield c
    finally:
        c.close()


# --------------------------------------------------------------------- periods
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)  # 17:30 IST


def test_today_is_local_midnight_to_midnight():
    assert period_for("today", IST, NOW) == Period("2026-09-27T18:30:00.000000Z", "2026-09-28T18:30:00.000000Z")
    early = datetime(2026, 9, 29, 3, 0, tzinfo=IST)  # still the 28th in UTC
    assert period_for("today", "Asia/Kolkata", early).start == "2026-09-28T18:30:00.000000Z"
    assert period_for("today", ZoneInfo("UTC"), NOW).start == "2026-09-28T00:00:00.000000Z"


def test_other_periods():
    assert period_for("yesterday", IST, NOW) == Period("2026-09-26T18:30:00.000000Z", "2026-09-27T18:30:00.000000Z")
    assert period_for("last_7_days", IST, NOW) == Period("2026-09-21T18:30:00.000000Z",
                                                         "2026-09-28T18:30:00.000000Z")
    assert period_for("all", IST, NOW) == Period(None, None)
    custom = period_for("custom", IST, NOW, (date(2026, 9, 10), date(2026, 9, 8)))  # swapped
    assert custom == Period("2026-09-07T18:30:00.000000Z", "2026-09-10T18:30:00.000000Z")


def test_period_errors():
    with pytest.raises(ValueError):
        period_for("Last hour", IST, NOW)
    with pytest.raises(ValueError):
        period_for("today", IST, NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        period_for("custom", IST, NOW)


# --------------------------------------------------------------------- fixture
def _det(sid, track, at, synthetic=False):
    return DetectionRow(sid, track, track, at, 0.8, 0, 0, 10, 10, synthetic)


def _build(conn, cfg):
    """Hand-built day (see the expected numbers in test_compute_metrics_on_fixture)."""
    for sid, start in (("s0", local(7, day=D - timedelta(days=1))), ("s1", local(7)), ("s2", local(9))):
        insert_session(conn, session_id=sid, source_type="video", source_label="clip.mp4", status="STOPPED",
                       started_at=start, station="City Hospital", trip_id="T")
    insert_detections(conn, [_det("s1", t, local(7, t)) for t in (1, 2, 3, 4, 5, 9)]
                      + [_det("s2", 1, local(9, 1)), _det("s0", 1, local(7, 1, day=D - timedelta(days=1)))])
    b = {}
    for sid, track, at in (("s1", 1, local(7, 1)), ("s1", 2, local(7, 2)), ("s1", 3, local(7, 3)),
                           ("s1", 4, local(7, 4)), ("s1", 5, local(7, 5)), ("s2", 1, local(9, 1)),
                           ("s0", 1, local(7, 1, day=D - timedelta(days=1)))):
        b[(sid, track)] = insert_boarding(conn, session_id=sid, track_id=track, boarded_at=at,
                                          direction="top_to_bottom", station="City Hospital", trip_id="T")
    tokens = {}
    for tid, window in (("TKT-1", (5, 23)), ("TKT-2", (5, 23)), ("TKT-3", (1, 6))):
        _, tokens[tid] = issue_ticket(conn, route=cfg.route, tz=IST, boarding_station="Central Bus Stand",
                                      destination_station="Airport", travel_date=D, ticket_id=tid,
                                      passenger_ref="PSG-" + tid, valid_from=local(window[0]),
                                      expires_at=local(window[1]), now=local(1))
    v = TicketValidator(tz=IST)
    j = build_journey(cfg, now=local(8), trip_id="T")

    def check(cred, ctype, key, minute):
        v.validate(conn, cred, credential_type=ctype, journey=j, channel="camera", now=local(8, minute),
                   boarding_id=b[key] if key else None)

    check(encode_payload(tokens["TKT-1"]), "qr_token", ("s1", 1), 1)
    check("TKT-1", "ticket_id", None, 2)                       # desk re-check: same ticket counts once
    check(tokens["TKT-2"], "qr_token", ("s1", 2), 3)
    check(tokens["TKT-2"], "qr_token", ("s1", 3), 4)            # ALREADY_USED: in Valid and Invalid
    check(tokens["TKT-3"], "qr_token", ("s1", 4), 5)            # EXPIRED: not in Invalid
    check("TKT-999", "ticket_id", ("s1", 5), 6)
    check("tkt-999", "ticket_id", ("s1", 5), 7)                 # duplicate failed scan counts once
    return b


def test_compute_metrics_on_fixture(conn, cfg):
    _build(conn, cfg)
    today = period_for("today", IST, local(12))
    assert compute_metrics(conn, today) == DashboardMetrics(
        valid=2, invalid=2, total_passenger=6, total_detection=7, expired_tickets=1, boarding_events=6,
        tracking_ids=7)
    everything = compute_metrics(conn, Period())
    assert (everything.total_detection, everything.boarding_events, everything.tracking_ids,
            everything.total_passenger) == (8, 7, 8, 7)
    yesterday = compute_metrics(conn, period_for("yesterday", IST, local(12)))
    assert (yesterday.boarding_events, yesterday.tracking_ids, yesterday.valid) == (1, 1, 0)


def test_empty_database_is_all_zero(conn):
    metrics = compute_metrics(conn, Period())
    assert metrics == DashboardMetrics() and metrics.is_empty()


def test_cards_order_and_help():
    cards = DashboardMetrics(1, 2, 3, 4, 5, 6, 7).cards()
    assert [c[0] for c in cards] == ["Valid Tickets", "Invalid Tickets", "Total Passenger", "Total Detection", "Expired Tickets",
                                     "Boarding Events", "Tracking IDs"]
    assert [c[1] for c in cards] == [1, 2, 3, 4, 5, 6, 7]
    assert "boarded Track IDs not linked to a valid ticket" in TOTAL_PASSENGER_CAPTION
    texts = [TOTAL_PASSENGER_CAPTION, *METRIC_HELP.values()]
    assert all("ticketless" not in t.lower() and "awaiting ticket verification" not in t for t in texts)


# ---------------------------------------------------------------------- tables
def test_boardings_needing_resolution_m3(conn, cfg):
    b = _build(conn, cfg)
    df = boardings_needing_resolution(conn, period_for("today", IST, local(12)), limit=50)
    assert list(df["boarding_id"]) == [b[("s2", 1)], b[("s1", 5)], b[("s1", 4)], b[("s1", 3)]]
    assert set(df["verification_status"]) == {"VERIFICATION_REQUIRED", "INVALID", "EXPIRED", "ALREADY_USED"}
    assert b[("s1", 1)] not in set(df["boarding_id"])
    assert len(boardings_needing_resolution(conn, Period(), limit=2)) == 2


def test_recent_tables(conn, cfg):
    _build(conn, cfg)
    today = period_for("today", IST, local(12))
    boardings = recent_boardings(conn, today, limit=50)
    assert len(boardings) == 6 and boardings.iloc[0]["track_id"] == 1 and boardings.iloc[0]["session_id"] == "s2"
    assert len(recent_boardings(conn, today, ["VALID"])) == 2
    assert len(recent_boardings(conn, today, [])) == 0
    assert len(recent_boardings(conn, today, ["DROP TABLE"])) == 0
    validations = recent_validations(conn, today)
    assert len(validations) == 7 and validations.iloc[0]["track_id"] == 5
    desk = validations[validations["boarding_id"].isna()]
    assert len(desk) == 1 and desk.iloc[0]["outcome"] == "VALID"
    assert set(recent_validations(conn, today, ["EXPIRED"])["outcome"]) == {"EXPIRED"}


def test_source_status(conn):
    assert source_status(conn, local(12), 10).running is False
    insert_session(conn, session_id="live", source_type="phone", source_label="http://192.0.0.4:8080/video",
                   status="RUNNING", started_at=local(11, 59), station=None, trip_id="T")
    fresh = source_status(conn, local(11, 59) + timedelta(seconds=5), 10)
    assert fresh.running and fresh.session_id == "live" and fresh.trip_id == "T"
    stale = source_status(conn, local(12, 5), 10)
    assert not stale.running and stale.status == "RUNNING"


def test_synthetic_row_count(conn):
    assert synthetic_row_count(conn) == 0
    insert_session(conn, session_id="demo", source_type="demo", source_label="demo", status="STOPPED",
                   started_at=local(7), station=None, trip_id="T", is_synthetic=True)
    insert_detections(conn, [_det("demo", 1, local(7), synthetic=True)])
    assert synthetic_row_count(conn) == 2


# ------------------------------------------------------------ pro dashboard additions
_PRE_CHANGE_VALID = ("SELECT COUNT(DISTINCT COALESCE(ticket_id, presented_ref, token_hash, 'evt:' || validation_id)) "
                     "FROM validation_events WHERE outcome = 'VALID' AND (:start IS NULL OR validated_at >= :start) "
                     "AND (:end IS NULL OR validated_at < :end)")
_PRE_CHANGE_INVALID = _PRE_CHANGE_VALID.replace("outcome = 'VALID'",
                                                "outcome IN ('INVALID', 'WRONG_ROUTE', 'ALREADY_USED')")
_PRE_CHANGE_EXPIRED = _PRE_CHANGE_VALID.replace("outcome = 'VALID'", "outcome = 'EXPIRED'")


def test_credential_key_keeps_kpi_sql_byte_identical():
    from app.analytics import _QUERIES, credential_key

    assert credential_key() == "COALESCE(ticket_id, presented_ref, token_hash, 'evt:' || validation_id)"
    assert credential_key("v") == "COALESCE(v.ticket_id, v.presented_ref, v.token_hash, 'evt:' || v.validation_id)"
    assert _QUERIES["valid"] == _PRE_CHANGE_VALID
    assert _QUERIES["invalid"] == _PRE_CHANGE_INVALID
    assert _QUERIES["expired_tickets"] == _PRE_CHANGE_EXPIRED


def test_last_24_hours_is_rolling_half_open():
    p = period_for("last_24_hours", IST, NOW)
    assert p == Period("2026-09-27T12:00:00.000000Z", "2026-09-28T12:00:00.000000Z")
    later = period_for("last_24_hours", IST, NOW + timedelta(minutes=5))
    assert later.start == "2026-09-27T12:05:00.000000Z" and later.end == "2026-09-28T12:05:00.000000Z"


def test_validation_search_count_and_offset(conn, cfg):
    from app.analytics import count_validations

    _build(conn, cfg)
    today = period_for("today", IST, local(12))
    assert count_validations(conn, today) == 7
    found = recent_validations(conn, today, search="tkt-99")
    assert len(found) == 2 and set(found["presented_ref"]) == {"TKT-999"}
    assert count_validations(conn, today, search="tkt-99") == 2
    assert set(recent_validations(conn, today, search="5")["track_id"]) == {5}
    assert len(recent_validations(conn, today, search="%")) == 0          # wildcard escaped
    assert len(recent_validations(conn, today, search="TKT_1")) == 0      # underscore escaped
    assert len(recent_validations(conn, today, search="'; DROP TABLE validation_events; --")) == 0
    assert count_validations(conn, today) == 7
    page1 = recent_validations(conn, today, limit=3)
    page2 = recent_validations(conn, today, limit=3, offset=3)
    assert len(page1) == 3 and len(page2) == 3
    assert not set(page1["validation_id"]) & set(page2["validation_id"])
    assert count_validations(conn, today, ["EXPIRED"], search="tkt") == 1


def test_validation_search_matches_channel_code(conn, cfg):
    from app.analytics import count_validations

    _build(conn, cfg)
    today = period_for("today", IST, local(12))
    assert count_validations(conn, today, search="camera") == 7
    assert count_validations(conn, today, search="CAM") == 7
    assert count_validations(conn, today, search="dashboard") == 0


# ------------------------------------------------------------ comparison period (KPI deltas)
def test_comparison_period_today_is_yesterday_up_to_same_clock_time():
    from app.analytics import comparison_period

    prev = comparison_period(period_for("today", IST, NOW), NOW)   # NOW = 17:30 IST
    assert prev == Period("2026-09-26T18:30:00.000000Z", "2026-09-27T12:00:00.000000Z")


def test_comparison_period_rolling_and_past_periods():
    from app.analytics import comparison_period

    assert comparison_period(period_for("last_24_hours", IST, NOW), NOW) == Period(
        "2026-09-26T12:00:00.000000Z", "2026-09-27T12:00:00.000000Z")
    assert comparison_period(period_for("yesterday", IST, NOW), NOW) == Period(
        "2026-09-25T18:30:00.000000Z", "2026-09-26T18:30:00.000000Z")


def test_comparison_period_none_for_all_time_and_future_ranges():
    from app.analytics import comparison_period

    assert comparison_period(Period(None, None), NOW) is None
    assert comparison_period(Period("2026-01-01T00:00:00.000000Z", None), NOW) is None
    future = period_for("custom", IST, NOW, (date(2026, 10, 5), date(2026, 10, 6)))
    assert comparison_period(future, NOW) is None
    with pytest.raises(ValueError):
        comparison_period(period_for("today", IST, NOW), datetime(2026, 9, 28, 12, 0))
