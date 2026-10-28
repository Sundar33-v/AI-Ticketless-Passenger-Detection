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


# ------------------------------------------------------------- grouped alerts
from pathlib import Path  # noqa: E402

from app.alert_service import alert_groups, classify_session, group_records, unread_count  # noqa: E402
from app.analytics import period_for  # noqa: E402
from app.database import to_iso  # noqa: E402
from app.tracking_queries import recent_sessions  # noqa: E402
from app.ticket_validator import issue_ticket  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def check(conn, cfg, credential, at, *, ctype="ticket_id", boarding_id=None):
    return TicketValidator(tz="Asia/Kolkata").validate(
        conn, credential, credential_type=ctype, journey=build_journey(cfg, now=at), channel="dashboard",
        boarding_id=boarding_id, now=at)


def expired_ticket(conn, cfg, tid):
    from datetime import date

    issue_ticket(conn, route=cfg.route, tz=ZoneInfo("Asia/Kolkata"), boarding_station="Central Bus Stand",
                 destination_station="Airport", travel_date=date(2026, 1, 14), ticket_id=tid,
                 valid_from=T0 - timedelta(days=1, hours=2), expires_at=T0 - timedelta(days=1), now=T0 - timedelta(days=2))


from zoneinfo import ZoneInfo  # noqa: E402


def test_repeated_expired_checks_are_one_group(conn, cfg):
    expired_ticket(conn, cfg, "TKT-EXP1")
    for i in range(11):
        check(conn, cfg, "TKT-EXP1", T0 - timedelta(minutes=30 - i))
    result = alert_groups(conn, ALL, T0, cfg)
    groups = [g for g in result.groups if g.kind == "expired_ticket"]
    assert len(groups) == 1 and groups[0].count == 11 and groups[0].title == "Expired ticket"
    assert groups[0].first_at == T0 - timedelta(minutes=30) and groups[0].last_at == T0 - timedelta(minutes=20)
    assert conn.execute("SELECT COUNT(*) FROM validation_events").fetchone()[0] == 11
    records = group_records(conn, groups[0], ALL)
    assert len(records) == 11 and records[0].at == groups[0].last_at and records[0].state == "EXPIRED"
    assert len(group_records(conn, groups[0], ALL, limit=5)) == 5


def test_counts_are_exact_beyond_the_table_limit(conn):
    cfg = AppConfig.from_dict({"dashboard": {"table_limit": 50}})
    for i in range(250):
        check(conn, cfg, "TKT-NOPE", T0 - timedelta(seconds=300 - i))
    for i in range(30):
        tid = f"TKT-E{i:03d}"
        expired_ticket(conn, cfg, tid)
        check(conn, cfg, tid, T0 - timedelta(seconds=10 + i))
    groups = [g for g in alert_groups(conn, ALL, T0, cfg).groups if g.source == "validation"]
    assert len(groups) == 31
    (invalid,) = [g for g in groups if g.kind == "invalid_ticket"]
    assert invalid.count == 250 and invalid.title == "Invalid ticket"
    assert len(group_records(conn, invalid, ALL)) == 20


def test_qr_kinds_and_titles(conn, cfg):
    check(conn, cfg, "not-a-token", T0 - timedelta(minutes=5), ctype="qr_token")               # malformed QR
    from app.qr_service import new_token

    check(conn, cfg, new_token(), T0 - timedelta(minutes=4), ctype="qr_token")                 # no record
    check(conn, cfg, "???", T0 - timedelta(minutes=3))                                          # malformed ID
    check(conn, cfg, "TKT-NOPE", T0 - timedelta(minutes=2))                                     # no record ID
    rows = conn.execute("SELECT reason FROM validation_events ORDER BY validation_id").fetchall()
    reasons = [r[0] for r in rows]
    assert reasons == ["Unrecognised QR code", "No ticket record matches this QR code", "Malformed ticket ID",
                       "No ticket record matches this ticket ID"]
    groups = alert_groups(conn, ALL, T0, cfg).groups
    kinds = {g.message.split(" – ")[-1]: (g.kind, g.title) for g in groups if g.source == "validation"}
    for reason in reasons:
        kind, title = kinds[reason]
        if "QR" in reason:
            assert (kind, title) == ("unrecognised_qr", "Unrecognised QR code"), reason
        else:
            assert (kind, title) == ("invalid_ticket", "Invalid ticket"), reason


def session_row(status, error=None, hb=T0, started=T0, ended=None):
    return {"status": status, "last_error": error, "last_heartbeat_at": to_iso(hb), "started_at": to_iso(started),
            "ended_at": to_iso(ended) if ended else None}


def test_classify_session_one_kind_each():
    assert classify_session(session_row("FAILED", "RuntimeError: model.track failed"), T0, 10)[0] == \
        "tracking_failure"
    assert classify_session(session_row("FAILED", "Could not open webcam at index 0."), T0, 10) == \
        ("camera_disconnected", "error", "Camera disconnected")
    assert classify_session(session_row("FAILED", "database is locked"), T0, 10) == \
        ("detection_stopped", "error", "Detection failed")
    assert classify_session(session_row("RUNNING", hb=T0 - timedelta(seconds=30)), T0, 10) == \
        ("detection_stopped", "warning", "Detection not responding")
    assert classify_session(session_row("RUNNING", hb=T0 - timedelta(seconds=5)), T0, 10) is None
    assert classify_session(session_row("STOPPED"), T0, 10) == ("detection_stopped", "info", "Detection stopped")


def test_session_spanning_period_start_in_alerts_and_sessions_table(db, cfg):
    c = db.connect()
    try:
        tz = ZoneInfo("Asia/Kolkata")
        now = T0  # 11:00 IST
        midnight = datetime(2026, 1, 15, 0, 0, tzinfo=tz)
        for sid, status in (("stopped", "STOPPED"), ("failed", "FAILED")):
            insert_session(c, session_id=sid, source_type="video", source_label="clip.mp4", status=status,
                           started_at=midnight - timedelta(minutes=30), station=None, trip_id="T")
            c.execute("UPDATE detection_sessions SET ended_at = ?, last_heartbeat_at = ?, last_error = ? "
                      "WHERE session_id = ?", (to_iso(midnight + timedelta(minutes=20)),
                                               to_iso(midnight + timedelta(minutes=20)),
                                               "boom" if status == "FAILED" else None, sid))
        insert_session(c, session_id="before", source_type="video", source_label="clip.mp4", status="STOPPED",
                       started_at=midnight - timedelta(hours=3), station=None, trip_id="T")
        today = period_for("today", tz, now)
        alerted = {g.match[0] for g in alert_groups(c, today, now, cfg).groups if g.source == "session"}
        listed = set(recent_sessions(c, today, now=now, stale_sec=10)["session_id"])
        assert alerted == listed == {"stopped", "failed"}
    finally:
        c.close()


def test_every_period_filtered_session_query_uses_session_at():
    for path in [*(ROOT / "app").rglob("*.py"), *(ROOT / "dashboard").rglob("*.py")]:
        source = path.read_text(encoding="utf-8")
        start = 0
        while (i := source.find("FROM detection_sessions", start)) >= 0:
            window = source[i:i + 300]
            if "P(" in window or ":start" in window:
                assert "SESSION_AT" in window, f"{path.name}: {window[:80]!r}"
            start = i + 1


def test_unread_count_and_reraise(conn, cfg):
    old = board(conn, 1, T0 - timedelta(minutes=5))
    insert_session(conn, session_id="s2", source_type="video", source_label="clip.mp4", status="STOPPED",
                   started_at=T0 - timedelta(minutes=1), station=None, trip_id="T")
    groups = alert_groups(conn, ALL, T0, cfg).groups
    assert any(g.severity == "info" for g in groups)                          # STOPPED session listed...
    assert unread_count(groups, {}) == 1                                      # ...but never counted
    read = {g.key: to_iso(g.last_at) for g in groups}
    assert unread_count(groups, read) == 0
    newer = [g if g.key != f"unv:{old}" else type(g)(**{**g.__dict__, "last_at": g.last_at + timedelta(seconds=1)})
             for g in groups]
    assert unread_count(newer, read) == 1


def test_detected_but_not_boarded_is_never_alerted(conn, cfg):
    from app.passenger_service import DetectionRow, insert_detections

    insert_detections(conn, [DetectionRow("s1", 7, 1, T0 - timedelta(minutes=5), 0.9, 0, 0, 5, 5, False)])
    groups = alert_groups(conn, ALL, T0, cfg).groups
    assert not [g for g in groups if g.source != "session"]
    assert all(g.track_id != 7 for g in groups)


def test_group_detail_omits_the_title(conn, cfg):
    expired_ticket(conn, cfg, "TKT-EXP9")
    check(conn, cfg, "TKT-EXP9", T0 - timedelta(minutes=10))
    board(conn, 3, T0 - timedelta(minutes=5))                                   # 10:55 IST, unverified
    insert_session(conn, session_id="s3", source_type="video", source_label="clip.mp4", status="STOPPED",
                   started_at=T0 - timedelta(minutes=2), station=None, trip_id="T")
    groups = {g.source: g for g in alert_groups(conn, ALL, T0, cfg).groups}
    expired = groups["validation"]
    reason = conn.execute("SELECT reason FROM validation_events").fetchone()[0]
    assert expired.detail == f"Expired – {reason}" and expired.title not in expired.detail
    assert expired.message == failure_message("EXPIRED", reason, None)          # message unchanged
    assert groups["boarding"].detail == "Boarded 10:55. Please check the passenger's ticket."
    assert groups["boarding"].message.startswith("Ticket verification required for Track ID 3")
    assert groups["session"].detail == groups["session"].message == "clip.mp4 · Stopped cleanly"


def test_validation_detail_without_reason_has_no_dangling_dash(conn, cfg):
    conn.execute("INSERT INTO validation_events (validated_at, outcome, reason, credential_type, channel) "
                 "VALUES (?, 'INVALID', '', 'ticket_id', 'cli')", (to_iso(T0 - timedelta(minutes=1)),))
    (group,) = [g for g in alert_groups(conn, ALL, T0, cfg).groups if g.source == "validation"]
    assert group.detail == "Invalid"
