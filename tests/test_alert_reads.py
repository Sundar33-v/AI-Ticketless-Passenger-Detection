"""Tests for app/alert_reads.py: the additive alert_reads table and the persistent read state."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

import pytest

import app.alert_reads as alert_reads
from app.database import Database

T0 = "2026-01-15T05:00:00.000000Z"
T1 = "2026-01-15T06:00:00.000000Z"
NOW = datetime(2026, 1, 15, 7, 0, tzinfo=timezone.utc)
OPERATIONAL = ("system_settings", "passengers", "tickets", "detection_sessions", "detection_records",
               "boarding_events", "validation_events", "model_evaluations", "metric_evaluations")


@pytest.fixture
def conn(db):
    c = db.connect()
    try:
        yield c
    finally:
        c.close()


def stored(conn):
    return {r[0]: (r[1], r[2]) for r in conn.execute("SELECT alert_key, last_at_seen, read_at FROM alert_reads")}


def test_table_added_to_old_populated_db_without_row_changes(tmp_path):
    db = Database(tmp_path / "old.db")
    db.initialize()
    c = db.connect()
    try:
        c.execute("DROP TABLE alert_reads")  # simulate a database from before this change
        c.execute("INSERT INTO detection_sessions (session_id, source_type, source_label, status, started_at, "
                  "last_heartbeat_at) VALUES ('s1', 'video', 'clip.mp4', 'RUNNING', ?, ?)", (T0, T0))
        c.execute("INSERT INTO validation_events (validated_at, outcome, reason, credential_type, channel) "
                  "VALUES (?, 'INVALID', 'No ticket record matches this ticket ID', 'ticket_id', 'cli')", (T0,))
        before = {t: c.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in OPERATIONAL
                  if t != "system_settings"}
        settings = c.execute("SELECT key, value FROM system_settings ORDER BY key").fetchall()
    finally:
        c.close()
    db.initialize()
    c = db.connect()
    try:
        after = {t: c.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in before}
        assert [tuple(r) for rows in after.values() for r in rows] == \
            [tuple(r) for rows in before.values() for r in rows]
        assert c.execute("SELECT key, value FROM system_settings ORDER BY key").fetchall() == settings
        assert c.execute("SELECT COUNT(*) FROM alert_reads").fetchone()[0] == 0
        assert c.execute("SELECT value FROM system_settings WHERE key='schema_version'").fetchone()[0] == "1"
    finally:
        c.close()


def test_key_length_is_checked_in_sql(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO alert_reads VALUES ('', ?, ?)", (T0, T0))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO alert_reads VALUES (?, ?, ?)", ("k" * 201, T0, T0))


def test_mark_read_upsert_keeps_max(conn):
    assert alert_reads.mark_read(conn, [("inv:a", T1)], NOW) == 1
    assert alert_reads.mark_read(conn, [("inv:a", T0), ("exp:b", T0)], NOW) == 2   # older value for inv:a
    rows = stored(conn)
    assert rows["inv:a"][0] == T1 and rows["exp:b"][0] == T0
    assert all(read_at == "2026-01-15T07:00:00.000000Z" for _seen, read_at in rows.values())
    assert alert_reads.load_read_state(conn, ["inv:a", "exp:b", "missing"]) == {"inv:a": T1, "exp:b": T0}


def test_mark_read_with_nothing_valid_runs_no_sql():
    class NoSql:
        def execute(self, *args):
            raise AssertionError("no SQL expected")

        executemany = execute
        in_transaction = False

    assert alert_reads.mark_read(NoSql(), [("", T0), ("k", "garbage")], NOW) == 0


def test_load_more_than_one_chunk(conn):
    keys = [f"unv:{i}" for i in range(1203)]
    assert alert_reads.mark_read(conn, [(k, T0) for k in keys], NOW) == 1203
    loaded = alert_reads.load_read_state(conn, keys + ["nope"])
    assert len(loaded) == 1203 and set(loaded) == set(keys)


def test_missing_table_gives_empty_map_and_one_warning(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(alert_reads, "_missing_table_logged", False)
    c = sqlite3.connect(str(tmp_path / "bare.db"))
    try:
        with caplog.at_level(logging.WARNING, logger="app.alert_reads"):
            assert alert_reads.load_read_state(c, ["a"]) == {}
            assert alert_reads.load_read_state(c, ["b"]) == {}
    finally:
        c.close()
    assert sum("alert_reads table is missing" in r.getMessage() for r in caplog.records) == 1


def test_other_errors_propagate(conn):
    conn.close()
    with pytest.raises(sqlite3.ProgrammingError):
        alert_reads.load_read_state(conn, ["a"])


def test_valid_items_drops_bad_items_and_keeps_max(caplog):
    items = [("ok", T0), ("ok", T1), ("ok", T0), ("bad-ts", "garbage"), ("", T0), ("k" * 201, T0),
             ("line\nbreak", T0), (None, T0), ("num", 5), "not-a-pair", ("k" * 200, "2026-01-15T05:00:00+00:00")]
    with caplog.at_level(logging.WARNING, logger="app.alert_reads"):
        clean = alert_reads.valid_items(items)
    assert clean == [("k" * 200, T0), ("ok", T1)]
    assert sum("Ignored invalid alert read item" in r.getMessage() for r in caplog.records) == 7
