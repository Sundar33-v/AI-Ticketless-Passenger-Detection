"""scripts/generate_dummy_data.py: AC12 numbers, idempotency, reset of synthetic rows only, M4 guard, AC19."""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

import generate_dummy_data as gen
from app.analytics import compute_metrics, period_for, synthetic_row_count
from app.database import Database, to_iso
from app.passenger_service import insert_boarding, insert_session
from app.qr_service import QRScanner, demo_token, encode_payload
from app.settings_schema import AppConfig
from app.ticket_validator import issue_ticket

DAY = date(2026, 1, 15)
TZ = "Asia/Kolkata"
NOW = datetime(2026, 1, 20, 6, 0, tzinfo=timezone.utc)
TABLES = ("passengers", "tickets", "detection_sessions", "detection_records", "boarding_events",
          "validation_events")


def run(db_path, *extra):
    return gen.main(["--db", str(db_path), "--date", DAY.isoformat(), "--no-qr-images", *extra])


def counts(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    finally:
        conn.close()


def metrics(db_path, choice, custom=None):
    conn = Database(db_path).read_connection()
    try:
        return compute_metrics(conn, period_for(choice, TZ, NOW, custom=custom))
    finally:
        conn.close()


def as_tuple(m):
    return (m.valid, m.invalid, m.total_passenger, m.total_detection, m.expired_tickets, m.boarding_events,
            m.tracking_ids)


def write_config(tmp_path, **sections):
    path = tmp_path / "demo_config.yaml"
    path.write_text(yaml.safe_dump(sections), encoding="utf-8")
    return path


@pytest.fixture
def demo_db(tmp_path):
    path = tmp_path / "demo.db"
    assert run(path) == 0
    return path


def test_demo_day_metrics_match_ac12(demo_db):
    day = metrics(demo_db, "custom", (DAY, DAY))
    assert as_tuple(day) == (7, 4, 16, 50, 2, 15, 16)


def test_all_time_metrics_match_ac12(demo_db):
    assert as_tuple(metrics(demo_db, "all")) == (8, 4, 18, 56, 2, 17, 18)


def test_second_run_changes_no_row_counts(demo_db):
    before = counts(demo_db)
    assert run(demo_db) == 0
    assert counts(demo_db) == before
    assert as_tuple(metrics(demo_db, "all")) == (8, 4, 18, 56, 2, 17, 18)


def test_run_for_another_day_reports_existing_day(demo_db, capsys):
    before = counts(demo_db)
    capsys.readouterr()
    assert gen.main(["--db", str(demo_db), "--date", "2026-01-20", "--no-qr-images"]) == 0
    out = capsys.readouterr().out
    assert "Demo data already exists for 2026-01-15 (and 2026-01-14); nothing was written for 2026-01-20." in out
    assert "--reset --generate --date 2026-01-20" in out
    assert "Demo data for 2026-01-20" not in out
    assert counts(demo_db) == before
    assert as_tuple(metrics(demo_db, "custom", (DAY, DAY))) == (7, 4, 16, 50, 2, 15, 16)
    assert gen.main(["--db", str(demo_db), "--date", "2026-01-20", "--no-qr-images", "--reset", "--generate"]) == 0
    assert "Demo data for 2026-01-20 (and 2026-01-19) is in place." in capsys.readouterr().out
    assert as_tuple(metrics(demo_db, "custom", (date(2026, 1, 20), date(2026, 1, 20)))) == (7, 4, 16, 50, 2, 15, 16)


def test_default_date_moves_old_demo_data_to_today(demo_db, capsys):
    before = counts(demo_db)
    capsys.readouterr()
    assert gen.main(["--db", str(demo_db), "--no-qr-images"]) == 0
    today = datetime.now(ZoneInfo(TZ)).date()
    out = capsys.readouterr().out
    assert f"Moved demo data from 2026-01-15 to {today:%Y-%m-%d}." in out
    assert f"Demo data for {today:%Y-%m-%d}" in out
    assert counts(demo_db) == before
    conn = Database(demo_db).read_connection()
    try:
        day = compute_metrics(conn, period_for("custom", TZ, datetime.now(timezone.utc), custom=(today, today)))
    finally:
        conn.close()
    assert as_tuple(day) == (7, 4, 16, 50, 2, 15, 16)


def test_default_date_move_keeps_real_rows(demo_db):
    _add_real_rows(demo_db)
    assert gen.main(["--db", str(demo_db), "--no-qr-images"]) == 0
    conn = sqlite3.connect(str(demo_db))
    try:
        assert conn.execute("SELECT COUNT(*) FROM tickets WHERE ticket_id = 'TKT-REAL01'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM detection_sessions WHERE session_id = 'real-session'"
                            ).fetchone()[0] == 1
    finally:
        conn.close()


def test_every_demo_row_is_synthetic(demo_db):
    conn = sqlite3.connect(str(demo_db))
    try:
        for table in TABLES:
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE is_synthetic = 0").fetchone()[0] == 0, table
        assert conn.execute("SELECT COUNT(*) FROM validation_events").fetchone()[0] == 14
        statuses = dict(conn.execute("SELECT session_id, status FROM detection_sessions").fetchall())
        assert statuses == {"demo-session-00": "STOPPED", "demo-session-01": "STOPPED"}
    finally:
        conn.close()


def test_expected_outcomes_per_track(demo_db):
    conn = sqlite3.connect(str(demo_db))
    try:
        rows = dict(conn.execute(
            "SELECT b.track_id, b.verification_status FROM boarding_events b "
            "WHERE b.session_id = 'demo-session-01'").fetchall())
        desk = conn.execute("SELECT outcome, boarding_id FROM validation_events WHERE event_key = 'demo:desk:T011'"
                            ).fetchone()
    finally:
        conn.close()
    expected = {1: "VALID", 2: "VALID", 3: "VALID", 4: "VALID", 5: "VALID", 6: "INVALID", 7: "EXPIRED",
                8: "EXPIRED", 9: "WRONG_ROUTE", 10: "VALID", 11: "ALREADY_USED", 12: "INVALID",
                13: "VERIFICATION_REQUIRED", 14: "VERIFICATION_REQUIRED", 15: "VERIFICATION_REQUIRED"}
    assert rows == expected
    assert desk == ("VALID", None)


def _add_real_rows(db_path):
    """Non-synthetic ticket, session and boarding that a reset must keep."""
    cfg = AppConfig.from_dict({})
    conn = Database(db_path).connect()
    try:
        issue_ticket(conn, route=cfg.route, tz=TZ, boarding_station="Central Bus Stand",
                     destination_station="Airport", travel_date=DAY, ticket_id="TKT-REAL01",
                     passenger_ref="PSG-REAL01", now=datetime(2026, 1, 15, 3, 0, tzinfo=timezone.utc))
        start = datetime(2026, 1, 15, 4, 0, tzinfo=timezone.utc)
        insert_session(conn, session_id="real-session", source_type="video", source_label="clip.mp4",
                       status="STOPPED", started_at=start, station="City Hospital", trip_id="R1-outbound-2026-01-15")
        insert_boarding(conn, session_id="real-session", track_id=1, boarded_at=start, direction="top_to_bottom",
                        station="City Hospital", trip_id="R1-outbound-2026-01-15")
    finally:
        conn.close()


def test_reset_removes_only_synthetic_rows(demo_db):
    _add_real_rows(demo_db)
    assert gen.main(["--db", str(demo_db), "--reset"]) == 0
    conn = Database(demo_db).read_connection()
    try:
        assert synthetic_row_count(conn) == 0
        assert conn.execute("SELECT ticket_id FROM tickets").fetchall()[0][0] == "TKT-REAL01"
        assert conn.execute("SELECT COUNT(*) FROM detection_sessions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM boarding_events").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM passengers").fetchone()[0] == 1
    finally:
        conn.close()


def test_reset_with_generate_regenerates(demo_db):
    before = counts(demo_db)
    assert gen.main(["--db", str(demo_db), "--date", DAY.isoformat(), "--no-qr-images", "--reset",
                     "--generate"]) == 0
    assert counts(demo_db) == before
    assert as_tuple(metrics(demo_db, "custom", (DAY, DAY))) == (7, 4, 16, 50, 2, 15, 16)


@pytest.mark.parametrize("sections", [
    {"journey": {"direction": "inbound"}},
    {"route": {"stations": ["Market Road", "Central Bus Stand", "City Hospital", "College Gate",
                            "Railway Station", "Airport"]}},
    {"route": {"route_id": "R2"}},
])
def test_config_mismatch_exits_1_and_writes_nothing(tmp_path, capsys, sections):
    db_path = tmp_path / "guard.db"
    code = gen.main(["--db", str(db_path), "--date", DAY.isoformat(), "--no-qr-images",
                     "--config", str(write_config(tmp_path, **sections))])
    assert code == 1
    err = capsys.readouterr().err
    assert "Demo data requires route R1 outbound with the default stations; current config: " in err
    assert not db_path.exists()


def test_mismatch_message_names_route_and_direction(tmp_path, capsys):
    path = write_config(tmp_path, journey={"direction": "inbound"})
    assert gen.main(["--db", str(tmp_path / "g.db"), "--config", str(path), "--no-qr-images"]) == 1
    assert capsys.readouterr().err.strip().endswith("current config: R1/inbound")


def test_fixed_journey_trip_id_is_ignored(tmp_path):
    db_path = tmp_path / "fixed.db"
    config = write_config(tmp_path, journey={"trip_id": "FIXED"})
    assert gen.main(["--db", str(db_path), "--date", DAY.isoformat(), "--no-qr-images",
                     "--config", str(config)]) == 0
    conn = sqlite3.connect(str(db_path))
    try:
        trips = dict(conn.execute("SELECT session_id, trip_id FROM detection_sessions").fetchall())
        validation_trips = {r[0] for r in conn.execute("SELECT DISTINCT trip_id FROM validation_events")}
    finally:
        conn.close()
    assert trips == {"demo-session-01": "R1-outbound-2026-01-15", "demo-session-00": "R1-outbound-2026-01-14"}
    assert validation_trips == {"R1-outbound-2026-01-15", "R1-outbound-2026-01-14"}
    assert as_tuple(metrics(db_path, "custom", (DAY, DAY))) == (7, 4, 16, 50, 2, 15, 16)


def test_demo_qr_images_decode_to_demo_tokens(tmp_path):
    qr_dir = tmp_path / "qr"
    assert gen.main(["--db", str(tmp_path / "q.db"), "--date", DAY.isoformat(), "--qr-dir", str(qr_dir)]) == 0
    pngs = sorted(p.name for p in qr_dir.glob("*.png"))
    assert len(pngs) == 12 and pngs[0] == "DEMO-TKT-001.png"
    decoded = QRScanner().decode_image(qr_dir / "DEMO-TKT-001.png")
    assert [d.payload for d in decoded] == [encode_payload(demo_token(gen.DEFAULT_SEED, "DEMO-TKT-001"))]


def test_session_timestamps_are_fixed(demo_db):
    conn = sqlite3.connect(str(demo_db))
    try:
        row = conn.execute("SELECT started_at, ended_at FROM detection_sessions WHERE session_id = "
                           "'demo-session-01'").fetchone()
    finally:
        conn.close()
    # 06:59:57 and 09:25 IST on D are 01:29:57 and 03:55 UTC.
    assert row == (to_iso(datetime(2026, 1, 15, 1, 29, 57, tzinfo=timezone.utc)),
                   to_iso(datetime(2026, 1, 15, 3, 55, tzinfo=timezone.utc)))


def test_generator_source_contains_no_sql_writes():
    source = Path(gen.__file__).read_text(encoding="utf-8")
    assert not re.search(r"\b(insert|update)\b", source, re.IGNORECASE)
    assert ".execute(" not in source
