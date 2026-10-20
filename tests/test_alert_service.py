"""Tests for app/alert_service.py: wording, alert generation and the timeout rule."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from app.alert_service import (
    OUTCOME_LABELS,
    AlertService,
    failure_message,
    required_message,
    verification_alerts,
)
from app.analytics import Period
from app.passenger_service import insert_boarding, insert_session
from app.settings_schema import AppConfig
from app.ticket_validator import Outcome, TicketValidator, ValidationResult, build_journey

T0 = datetime(2026, 1, 15, 5, 30, tzinfo=timezone.utc)  # 11:00 IST
ALL = Period(None, None)


@pytest.fixture
def conn(db):
    c = db.connect()
    try:
        insert_session(c, session_id="s1", source_type="video", source_label="clip.mp4", status="RUNNING",
                       started_at=T0, station=None, trip_id="T")
        yield c
    finally:
        c.close()


def board(conn, track, at):
    return insert_boarding(conn, session_id="s1", track_id=track, boarded_at=at, direction="top_to_bottom",
                           station=None, trip_id="T")


def test_labels_use_verification_wording():
    assert OUTCOME_LABELS["VERIFICATION_REQUIRED"] == "Ticket verification required"
    assert set(OUTCOME_LABELS) == {o.value for o in Outcome}
    texts = list(OUTCOME_LABELS.values()) + [failure_message("INVALID", "r", 1), required_message(1, "07:00")]
    assert all("ticketless" not in t.lower() for t in texts)


def test_messages():
    assert failure_message("EXPIRED", "Ticket expired at x", 4) == \
        "Ticket verification failed for Track ID 4: Expired – Ticket expired at x"
    assert failure_message(Outcome.WRONG_ROUTE, "r", None) == "Ticket verification failed: Wrong route – r"
    assert required_message(9, "07:05") == ("Ticket verification required for Track ID 9 (boarded 07:05). "
                                            "Please check the passenger's ticket.")


def test_verification_alerts_failures_and_timeouts(conn, cfg):
    old = board(conn, 1, T0 - timedelta(minutes=5))
    board(conn, 2, T0 - timedelta(seconds=10))  # younger than the 60 s timeout
    failed = board(conn, 3, T0 - timedelta(minutes=3))
    validator = TicketValidator(tz="Asia/Kolkata")
    journey = build_journey(cfg, now=T0)
    validator.validate(conn, "TKT-NOPE", credential_type="ticket_id", journey=journey, channel="camera",
                       boarding_id=failed, now=T0 - timedelta(minutes=2))
    validator.validate(conn, "TKT-DESK", credential_type="ticket_id", journey=journey, channel="dashboard",
                       now=T0 - timedelta(minutes=1))
    alerts = verification_alerts(conn, ALL, T0, cfg)
    assert [a.kind for a in alerts] == ["verification_failed", "verification_failed", "verification_required"]
    assert alerts[0].message.startswith("Ticket verification failed: Invalid")  # desk check, no Track ID
    assert alerts[1].track_id == 3 and "for Track ID 3" in alerts[1].message
    assert alerts[2].boarding_id == old and "(boarded 10:55)" in alerts[2].message
    assert all("ticketless" not in a.message.lower() for a in alerts)


def test_valid_and_resolved_boardings_do_not_alert(conn, cfg):
    bid = board(conn, 1, T0 - timedelta(minutes=5))
    conn.execute("UPDATE boarding_events SET verification_status = 'VALID' WHERE boarding_id = ?", (bid,))
    assert verification_alerts(conn, ALL, T0, cfg) == []


def test_alerts_respect_period_and_limit(conn):
    cfg = AppConfig.from_dict({"dashboard": {"table_limit": 10}})
    for track in range(15):
        board(conn, track, T0 - timedelta(hours=1, minutes=track))
    alerts = verification_alerts(conn, ALL, T0, cfg)
    assert len(alerts) == 10 and alerts[0].track_id == 0
    future = Period("2026-02-01T00:00:00.000000Z", None)
    assert verification_alerts(conn, future, T0, cfg) == []


def test_alert_service_logs_and_keeps_last_messages(caplog):
    service = AlertService()
    with caplog.at_level(logging.INFO, logger="app.alert_service"):
        service.on_boarding(1, 10)
        service.on_validation(ValidationResult(Outcome.VALID, "Valid for A → B"), 1)
        service.on_validation(ValidationResult(Outcome.EXPIRED, "Ticket expired at x"), 2)
        service.on_pending([5])
    levels = {r.getMessage(): r.levelno for r in caplog.records}
    assert levels["Ticket verification failed for Track ID 2: Expired – Ticket expired at x"] == logging.WARNING
    assert levels["Ticket valid for Track ID 1: Valid for A → B"] == logging.INFO
    assert service.messages == ["Ticket valid for Track ID 1: Valid for A → B",
                                "Ticket verification failed for Track ID 2: Expired – Ticket expired at x",
                                "Ticket verification required: ID 5"]
    assert all("ticketless" not in m.lower() for m in service.messages)
