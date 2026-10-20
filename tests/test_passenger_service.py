"""Tests for app/passenger_service.py: SQL helpers, throttle, boarding rules, service lifecycle."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import app.passenger_service as ps
from app.database import DatabaseError, to_iso
from app.entry_line import EntryEvent, EntryLineConfig
from app.passenger_service import (
    BoardingRules,
    DetectionRow,
    DetectionThrottle,
    PassengerService,
    delete_synthetic,
    end_session_row,
    insert_boarding,
    insert_detections,
    insert_session,
)
from app.tracker import TrackedPerson

T0 = datetime(2026, 1, 15, 5, 30, tzinfo=timezone.utc)
FRAME = (640, 480)  # default line: horizontal at y = 240, top_to_bottom


def person(tid, bottom_y, hits=10, conf=0.9):
    return TrackedPerson(tid, (100.0, bottom_y - 100.0, 160.0, float(bottom_y)), conf, hits=hits)


class FakeTracker:
    """Replays scripted frames of TrackedPerson lists."""

    def __init__(self, frames):
        self.frames = list(frames)

    def track(self, _frame=None):
        return self.frames.pop(0)


@pytest.fixture
def conn(db):
    c = db.connect()
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def service(db, cfg):
    svc = PassengerService(db, cfg, clock=lambda: T0)
    svc.create_session("video", "clip.mp4", "City Hospital", "R1-outbound-2026-01-15")
    try:
        yield svc
    finally:
        svc.close()


def count(conn, table, where="1"):
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]


def session_row(conn, sid="s1", synthetic=False):
    insert_session(conn, session_id=sid, source_type="demo", source_label="demo", status="RUNNING",
                   started_at=T0, station=None, trip_id="T", is_synthetic=synthetic)


# ------------------------------------------------------------------ AC 7
def test_one_boarding_per_track_over_100_frames_back_and_forth(service, db):
    frames = []
    for i in range(100):
        phase = (i // 10) % 2  # 10 frames above the line, 10 below, repeatedly
        y = 280 if phase else 200
        frames.append([person(1, y, hits=i + 1), person(2, 480 - y, hits=i + 1)])
    tracker = FakeTracker(frames)
    rules = BoardingRules(EntryLineConfig(), min_frames_seen=3)
    boardings = []
    for i in range(100):
        people = tracker.track()
        for event in rules.update(people, now=i / 30.0, frame_size=FRAME):
            boardings.append(service.record_boarding(event, station="City Hospital", trip_id="T"))
    assert len(boardings) == 2  # track 1 (down) and track 2 (starts low, moves down later)
    check = db.connect()
    try:
        assert count(check, "boarding_events") == 2
        with pytest.raises(sqlite3.IntegrityError):
            check.execute("INSERT INTO boarding_events (session_id, track_id, boarded_at, direction) "
                          "VALUES (?, 1, ?, 'top_to_bottom')", (service.session_id, to_iso(T0)))
    finally:
        check.close()


def test_record_boarding_twice_returns_same_id(service):
    event = EntryEvent(5, "top_to_bottom", T0.timestamp())
    first = service.record_boarding(event, station=None, trip_id="T")
    assert service.record_boarding(event, station=None, trip_id="T") == first


# ------------------------------------------------------------------ AC 8
def test_throttle_ten_seconds_at_30fps(service, db):
    for k in range(300):
        service.record_detections([person(1, 300)], k, T0 + timedelta(seconds=k / 30.0))
    service.flush()
    check = db.connect()
    try:
        assert count(check, "detection_records") in (10, 11)
    finally:
        check.close()


def test_throttle_skips_untracked_and_caps_buffer():
    throttle = DetectionThrottle("s", 1.0, max_rows=3)

    class Untracked:
        track_id = None
        bbox = (0, 0, 1, 1)
        confidence = 0.5

    assert throttle.collect([Untracked()], 0, T0) == 0
    throttle.collect([person(t, 300) for t in range(5)], 0, T0)
    assert len(throttle) == 3
    assert [r.track_id for r in throttle.drain()] == [2, 3, 4]


# ------------------------------------------------------------ min_frames_seen
def test_crossing_confirmed_once_hits_reach_min():
    rules = BoardingRules(EntryLineConfig(), min_frames_seen=3)
    assert rules.update([person(1, 200, hits=1)], 0.0, FRAME) == []
    assert rules.update([person(1, 300, hits=2)], 0.1, FRAME) == []  # crossed, only 2 frames seen
    assert 1 in rules.pending_crossings
    confirmed = rules.update([person(1, 310, hits=3)], 0.2, FRAME)
    assert [(e.track_id, e.timestamp) for e in confirmed] == [(1, 0.1)]  # original crossing time
    assert rules.update([person(1, 320, hits=4)], 0.3, FRAME) == []


def test_pending_crossing_times_out():
    rules = BoardingRules(EntryLineConfig(cooldown_seconds=1.0), min_frames_seen=5)
    rules.update([person(1, 200, hits=1)], 0.0, FRAME)
    rules.update([person(1, 300, hits=2)], 0.1, FRAME)
    assert rules.update([], 0.5, FRAME) == []  # track missing for a moment
    assert rules.update([person(1, 310, hits=5)], 1.5, FRAME) == []  # past the deadline
    assert not rules.pending_crossings and not rules.boarded


def test_pending_crossing_dropped_when_crossing_back():
    rules = BoardingRules(EntryLineConfig(), min_frames_seen=3)
    rules.update([person(1, 200, hits=1)], 0.0, FRAME)
    rules.update([person(1, 300, hits=2)], 0.1, FRAME)
    assert rules.update([person(1, 200, hits=3)], 0.2, FRAME) == []
    assert not rules.pending_crossings
    assert rules.update([person(1, 300, hits=4)], 0.3, FRAME) == []  # never re-emitted
    assert not rules.boarded


def test_wrong_direction_is_not_a_boarding():
    rules = BoardingRules({"entry_direction": "top_to_bottom"}, min_frames_seen=1)
    rules.update([person(1, 300)], 0.0, FRAME)
    assert rules.update([person(1, 200)], 0.1, FRAME) == []


# ------------------------------------------------------------- SQL helpers
def test_helpers_are_idempotent(conn):
    session_row(conn)
    session_row(conn)
    assert count(conn, "detection_sessions") == 1
    rows = [DetectionRow("s1", 1, k, T0 + timedelta(seconds=k), 0.8, 0, 0, 10, 10) for k in range(3)]
    assert insert_detections(conn, rows) == 3
    assert insert_detections(conn, rows) == 0
    assert insert_detections(conn, []) == 0
    first = insert_boarding(conn, session_id="s1", track_id=1, boarded_at=T0, direction="top_to_bottom",
                            station=None, trip_id="T")
    again = insert_boarding(conn, session_id="s1", track_id=1, boarded_at=T0 + timedelta(minutes=5),
                            direction="top_to_bottom", station=None, trip_id="T")
    assert first == again and count(conn, "boarding_events") == 1
    end_session_row(conn, "s1", status="STOPPED", ended_at=T0 + timedelta(hours=1))
    end_session_row(conn, "s1", status="STOPPED", ended_at=T0 + timedelta(hours=1))
    row = conn.execute("SELECT status, ended_at, last_heartbeat_at FROM detection_sessions").fetchone()
    assert tuple(row) == ("STOPPED", to_iso(T0 + timedelta(hours=1)), to_iso(T0 + timedelta(hours=1)))


def test_helpers_join_an_open_transaction(conn):
    from app.database import transaction

    with pytest.raises(ZeroDivisionError):
        with transaction(conn):
            session_row(conn)
            1 / 0
    assert count(conn, "detection_sessions") == 0


def _seed_mixed(conn, cfg):
    from app.ticket_validator import TicketValidator, build_journey, issue_ticket

    for sid, synthetic in (("demo", True), ("live", False)):
        session_row(conn, sid, synthetic)
        insert_detections(conn, [DetectionRow(sid, 1, 0, T0, 0.9, 0, 0, 5, 5, synthetic)])
        insert_boarding(conn, session_id=sid, track_id=1, boarded_at=T0, direction="top_to_bottom",
                        station=None, trip_id="T", is_synthetic=synthetic)
    _, token = issue_ticket(conn, route=cfg.route, tz="Asia/Kolkata", boarding_station="Central Bus Stand",
                            destination_station="Airport", travel_date="2026-01-15", ticket_id="DEMO-TKT-001",
                            passenger_ref="DEMO-PSG-001", is_synthetic=True, now=T0)
    issue_ticket(conn, route=cfg.route, tz="Asia/Kolkata", boarding_station="Central Bus Stand",
                 destination_station="Airport", travel_date="2026-01-15", ticket_id="TKT-REAL",
                 passenger_ref="PSG-REAL", now=T0)
    demo_bid = conn.execute("SELECT boarding_id FROM boarding_events WHERE session_id='demo'").fetchone()[0]
    TicketValidator(tz="Asia/Kolkata").validate(conn, token, credential_type="qr_token",
                                                journey=build_journey(cfg, now=T0), channel="demo",
                                                boarding_id=demo_bid, is_synthetic=True, now=T0)


def test_delete_synthetic_keeps_real_rows(conn, cfg):
    _seed_mixed(conn, cfg)
    counts = delete_synthetic(conn)
    assert counts == {"validation_events": 1, "boarding_events": 1, "detection_records": 1,
                      "detection_sessions": 1, "tickets": 1, "passengers": 1}
    for table in ("passengers", "tickets", "detection_sessions", "detection_records", "boarding_events"):
        assert count(conn, table, "is_synthetic = 1") == 0
        assert count(conn, table) == 1
    assert delete_synthetic(conn)["tickets"] == 0


def test_delete_synthetic_rolls_back_on_restrict(conn, cfg):
    _seed_mixed(conn, cfg)
    conn.execute("UPDATE tickets SET passenger_ref = 'DEMO-PSG-001' WHERE ticket_id = 'TKT-REAL'")
    with pytest.raises(sqlite3.IntegrityError):
        delete_synthetic(conn)
    assert count(conn, "boarding_events", "is_synthetic = 1") == 1


# ------------------------------------------------------------ service lifecycle
def test_session_lifecycle_and_heartbeat(service, db):
    service.record_detections([person(1, 300), person(2, 300)], 0, T0)
    assert service.heartbeat(frames=42, fps=12.5, now=T0 + timedelta(seconds=3)) == 2
    service.end_session("FAILED", "x" * 900)
    check = db.connect()
    try:
        row = check.execute("SELECT * FROM detection_sessions").fetchone()
        assert (row["status"], row["frames_processed"], row["fps"]) == ("FAILED", 42, 12.5)
        assert row["source_label"] == "clip.mp4" and row["trip_id"] == "R1-outbound-2026-01-15"
        assert len(row["last_error"]) == 500 and row["ended_at"] is not None
    finally:
        check.close()


def test_flush_keeps_rows_when_database_is_locked(service, db, monkeypatch):
    real = ps.insert_detections
    calls = []

    def flaky(conn, rows):
        calls.append(len(rows))
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(conn, rows)

    monkeypatch.setattr(ps, "insert_detections", flaky)
    service.record_detections([person(1, 300)], 0, T0)
    assert service.flush() == 0
    assert len(service.throttle) == 1
    assert service.flush() == 1
    assert len(service.throttle) == 0


def test_flush_reraises_other_errors(service, monkeypatch):
    def broken(conn, rows):
        raise sqlite3.OperationalError("no such table: detection_records")

    monkeypatch.setattr(ps, "insert_detections", broken)
    service.record_detections([person(1, 300)], 0, T0)
    with pytest.raises(sqlite3.OperationalError):
        service.flush()


def test_boarding_queued_when_busy_then_retried(service, monkeypatch):
    real = ps.insert_boarding
    state = {"fail": 2}

    def flaky(conn, **kw):
        if state["fail"]:
            state["fail"] -= 1
            raise sqlite3.OperationalError("database is busy")
        return real(conn, **kw)

    monkeypatch.setattr(ps, "insert_boarding", flaky)
    event = EntryEvent(7, "top_to_bottom", T0.timestamp())
    assert service.record_boarding(event, station=None, trip_id="T") is None
    assert service.retry_boardings() == [] and service.queued_boardings == 1
    written = service.retry_boardings()
    assert [b.track_id for b in written] == [7] and service.queued_boardings == 0


def test_boarding_gives_up_after_max_tries(service, monkeypatch):
    def busy(conn, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ps, "insert_boarding", busy)
    service.record_boarding(EntryEvent(8, "top_to_bottom", T0.timestamp()), station=None, trip_id="T")
    with pytest.raises(DatabaseError):
        for _ in range(ps.MAX_BOARDING_TRIES):
            service.retry_boardings()


def test_pending_verifications_and_note_validation(service):
    event = EntryEvent(3, "top_to_bottom", T0.timestamp())
    bid = service.record_boarding(event, station=None, trip_id="T")
    assert service.pending_verifications(T0 + timedelta(seconds=30), 60) == []
    assert [b.track_id for b in service.pending_verifications(T0 + timedelta(seconds=61), 60)] == [3]
    service.note_validation(bid, "VERIFICATION_REQUIRED")
    assert service.boardings[3].status == "VERIFICATION_REQUIRED"
    service.note_validation(bid, "VALID")
    service.note_validation(bid, "INVALID")
    assert service.boardings[3].status == "VALID"
    assert service.pending_verifications(T0 + timedelta(seconds=61), 60) == []
