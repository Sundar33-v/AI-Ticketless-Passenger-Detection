"""Tests for app/database.py (stdlib sqlite3 schema, connections, transactions, timestamps)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.database import (
    SCHEMA,
    Database,
    DatabaseError,
    parse_iso,
    to_iso,
    transaction,
    utc_now,
)

TABLES = {"system_settings", "passengers", "tickets", "detection_sessions", "detection_records",
          "boarding_events", "validation_events", "model_evaluations", "metric_evaluations", "alert_reads"}
INDEXES = {
    "idx_tickets_passenger", "idx_tickets_status", "idx_tickets_travel_date", "idx_sessions_started",
    "idx_detections_time", "idx_detections_track", "idx_boarding_time", "idx_boarding_status",
    "idx_validation_time", "idx_validation_outcome", "idx_validation_ticket", "idx_validation_boarding",
    "idx_model_evaluations_at", "idx_metric_evaluations_at",
}
NEW_TABLES = ("metric_evaluations", "alert_reads")
NEW_INDEXES = ("idx_metric_evaluations_at",)
T0 = "2026-01-15T05:00:00.000000Z"
T1 = "2026-01-15T23:00:00.000000Z"


def _names(conn, kind):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,)).fetchall()
    return {r["name"] for r in rows}


def _session(conn, sid="s1"):
    conn.execute(
        "INSERT INTO detection_sessions (session_id, source_type, source_label, status, started_at, "
        "last_heartbeat_at) VALUES (?, 'video', 'clip.mp4', 'RUNNING', ?, ?)", (sid, T0, T0))


def _passenger(conn, ref="PSG-0001"):
    conn.execute("INSERT INTO passengers (passenger_ref, created_at) VALUES (?, ?)", (ref, T0))


def _ticket(conn, ticket_id="TKT-1", **over):
    values = dict(ticket_id=ticket_id, passenger_ref="PSG-0001", route_id="R1", boarding_station="A",
                  destination_station="B", travel_date="2026-01-15", valid_from=T0, expires_at=T1,
                  status="ACTIVE", created_at=T0, updated_at=T0)
    values.update(over)
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    conn.execute(f"INSERT INTO tickets ({cols}) VALUES ({marks})", tuple(values.values()))


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / "t.db")
    db.initialize()
    return db


@pytest.fixture
def conn(database):
    c = database.connect()
    try:
        yield c
    finally:
        c.close()


# ------------------------------------------------------------------ schema
def test_initialize_is_idempotent_and_creates_everything(database):
    database.initialize()  # second run must not fail or duplicate settings
    c = database.connect()
    try:
        assert TABLES <= _names(c, "table")
        assert INDEXES <= _names(c, "index")
        settings = dict(c.execute("SELECT key, value FROM system_settings").fetchall())
        assert settings["schema_version"] == "1"
        assert "created_at" in settings
        assert c.execute("SELECT COUNT(*) FROM system_settings").fetchone()[0] == 2
    finally:
        c.close()


def test_created_at_is_written_once(database):
    c = database.connect()
    try:
        first = c.execute("SELECT value FROM system_settings WHERE key='created_at'").fetchone()[0]
        database.initialize()
        again = c.execute("SELECT value FROM system_settings WHERE key='created_at'").fetchone()[0]
    finally:
        c.close()
    assert first == again


def test_additive_migration_keeps_existing_rows(tmp_path):
    """A populated pre-change (v1 without model_evaluations) DB gains the table; no row is touched."""
    db = Database(tmp_path / "old.db")
    db.initialize()
    c = db.connect()
    try:
        c.execute("DROP INDEX idx_model_evaluations_at")
        c.execute("DROP TABLE model_evaluations")  # simulate the schema before this change
        _session(c)
        _passenger(c)
        _ticket(c)
        old_tables = {t for t in TABLES if t != "model_evaluations"}
        before = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in old_tables}
    finally:
        c.close()
    db.initialize()
    c = db.connect()
    try:
        after = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in old_tables}
        assert after == before and before["tickets"] == 1
        assert "model_evaluations" in _names(c, "table") and "idx_model_evaluations_at" in _names(c, "index")
        assert c.execute("SELECT value FROM system_settings WHERE key='schema_version'").fetchone()[0] == "1"
    finally:
        c.close()


def test_metric_table_added_to_old_db(tmp_path):
    """A populated DB from before the metrics change gains metric_evaluations; no row is touched."""
    db = Database(tmp_path / "old.db")
    db.initialize()
    c = db.connect()
    try:
        for name in NEW_INDEXES:
            c.execute(f"DROP INDEX {name}")
        for name in NEW_TABLES:
            c.execute(f"DROP TABLE {name}")
        _session(c)
        _passenger(c)
        _ticket(c)
        old_tables = TABLES - set(NEW_TABLES)
        before = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in old_tables}
    finally:
        c.close()
    db.initialize()
    db.initialize()  # idempotent
    c = db.connect()
    try:
        after = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in old_tables}
        assert after == before and before["tickets"] == 1
        assert set(NEW_TABLES) <= _names(c, "table") and set(NEW_INDEXES) <= _names(c, "index")
        assert c.execute("SELECT value FROM system_settings WHERE key='schema_version'").fetchone()[0] == "1"
    finally:
        c.close()


def test_new_db_has_no_account_tables(conn):
    """The dashboard has no sign-in: new databases get no users/auth_throttle and no FK to users."""
    assert not {"users", "auth_throttle"} & _names(conn, "table")
    assert conn.execute("PRAGMA foreign_key_list(metric_evaluations)").fetchall() == []
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(metric_evaluations)")}
    assert "created_by_user_id" not in columns


def test_metric_evaluations_rejects_total_zero(conn):
    sql = ("INSERT INTO metric_evaluations (evaluated_at, tp, tn, fp, fn, dataset_description) "
           "VALUES (?, ?, ?, ?, ?, 'set')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (T0, 0, 0, 0, 0))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (T0, -1, 2, 0, 0))
    conn.execute(sql, (T0, 1, 0, 0, 0))
    assert conn.execute("SELECT COUNT(*) FROM metric_evaluations").fetchone()[0] == 1

def test_every_index_in_schema_is_known():
    for name in INDEXES:
        assert name in SCHEMA


def test_connect_pragmas(conn):
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conn.isolation_level is None
    assert isinstance(conn.execute("SELECT 1 AS one").fetchone(), sqlite3.Row)


def test_newer_schema_version_is_fatal(database, conn):
    conn.execute("UPDATE system_settings SET value = '2' WHERE key = 'schema_version'")
    with pytest.raises(DatabaseError, match="newer version"):
        database.initialize()


def test_parent_directory_is_created(tmp_path):
    db = Database(tmp_path / "nested" / "dir" / "x.db")
    db.initialize()
    assert (tmp_path / "nested" / "dir" / "x.db").is_file()


def test_path_with_space_and_hash_works(tmp_path):
    db = Database(tmp_path / "my data #1" / "t.db")
    db.initialize()
    w = db.connect()
    try:
        _session(w)
    finally:
        w.close()
    r = db.read_connection()
    try:
        assert r.execute("SELECT COUNT(*) FROM detection_sessions").fetchone()[0] == 1
    finally:
        r.close()


# --------------------------------------------------------------- timestamps
def test_to_iso_fixed_width_utc():
    value = datetime(2026, 1, 15, 7, 0, tzinfo=timezone.utc)
    assert to_iso(value) == "2026-01-15T07:00:00.000000Z"
    ist = timezone(timedelta(hours=5, minutes=30))
    assert to_iso(datetime(2026, 1, 15, 12, 30, 0, 123, tzinfo=ist)) == "2026-01-15T07:00:00.000123Z"
    assert len(to_iso(utc_now())) == 27


def test_to_iso_rejects_naive():
    with pytest.raises(ValueError):
        to_iso(datetime(2026, 1, 15, 7, 0))


def test_parse_iso_round_trip():
    value = datetime(2026, 1, 15, 7, 0, 1, 500, tzinfo=timezone.utc)
    assert parse_iso(to_iso(value)) == value
    assert parse_iso(to_iso(value)).tzinfo is not None


def test_iso_strings_sort_chronologically():
    base = datetime(2026, 1, 15, tzinfo=timezone.utc)
    values = [base + timedelta(microseconds=n) for n in (1, 999999, 10, 1000000)]
    assert sorted(to_iso(v) for v in values) == [to_iso(v) for v in sorted(values)]


# ------------------------------------------------------------- transactions
def test_transaction_commits(database, conn):
    with transaction(conn):
        _session(conn)
    other = database.connect()
    try:
        assert other.execute("SELECT COUNT(*) FROM detection_sessions").fetchone()[0] == 1
    finally:
        other.close()


def test_transaction_rolls_back_on_exception(conn):
    with pytest.raises(ZeroDivisionError):
        with transaction(conn):
            _session(conn)
            1 / 0
    assert not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM detection_sessions").fetchone()[0] == 0


def test_transaction_refuses_nesting(conn):
    with transaction(conn):
        with pytest.raises(RuntimeError, match="already open"):
            with transaction(conn):
                pass
    assert not conn.in_transaction


def test_deferred_transaction(conn):
    with transaction(conn, immediate=False):
        _session(conn)
    assert conn.execute("SELECT COUNT(*) FROM detection_sessions").fetchone()[0] == 1


# --------------------------------------------------------------- read-only
def test_read_connection_rejects_writes(database):
    r = database.read_connection()
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            _session(r)
        assert r.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        r.close()


def test_read_connection_does_not_set_journal_mode(database, monkeypatch):
    executed = []
    real_connect = sqlite3.connect

    class Spy:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args):
            executed.append(sql)
            return self._conn.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def __setattr__(self, name, value):
            if name == "_conn":
                object.__setattr__(self, name, value)
            else:
                setattr(self._conn, name, value)

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: Spy(real_connect(*a, **k)))
    r = database.read_connection()
    r.close()
    assert executed and not any("journal_mode" in sql.lower() for sql in executed)


def test_memory_database_read_connection_is_normal():
    db = Database(":memory:")
    c = db.read_connection()
    try:
        c.execute("CREATE TABLE x (a INTEGER)")
    finally:
        c.close()


# -------------------------------------------------------------- constraints
def test_unique_session_track_boarding(conn):
    _session(conn)
    sql = ("INSERT INTO boarding_events (session_id, track_id, boarded_at, direction) "
           "VALUES ('s1', 7, ?, 'top_to_bottom')")
    conn.execute(sql, (T0,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (T1,))
    row = conn.execute("SELECT verification_status FROM boarding_events").fetchone()
    assert row[0] == "VERIFICATION_REQUIRED"


def test_ticket_status_check(conn):
    _passenger(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _ticket(conn, status="BOGUS")


def test_ticket_window_check(conn):
    _passenger(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _ticket(conn, valid_from=T1, expires_at=T0)
    with pytest.raises(sqlite3.IntegrityError):
        _ticket(conn, ticket_id="TKT-2", destination_station="A")


def test_ticket_requires_existing_passenger(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _ticket(conn)


def test_boarding_status_check(conn):
    _session(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO boarding_events (session_id, track_id, boarded_at, direction, "
                     "verification_status) VALUES ('s1', 1, ?, 'd', 'TICKETLESS')", (T0,))


def test_detection_confidence_and_synthetic_checks(conn):
    _session(conn)
    sql = ("INSERT INTO detection_records (session_id, track_id, frame_index, detected_at, confidence, "
           "x1, y1, x2, y2, is_synthetic) VALUES ('s1', 1, ?, ?, ?, 0, 0, 1, 1, ?)")
    conn.execute(sql, (0, T0, 0.5, 0))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (1, T0, 1.5, 0))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (2, T0, 0.5, 2))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (0, T0, 0.5, 0))  # UNIQUE(session, track, frame)
