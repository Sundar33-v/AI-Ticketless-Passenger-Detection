"""scripts/init_db.py, generate_qr.py and validate_qr.py (AC11) against tmp databases."""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import timedelta

import pytest

import generate_qr
import init_db
import validate_qr
from app.database import Database, utc_now
from app.passenger_service import insert_boarding, insert_session
from app.qr_service import QRScanner, hash_token, parse_payload, write_qr_png

PASSENGER_NAME = "Asha Verma"


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "scripts.db"


def issue(db_path, tmp_path, capsys, *extra):
    """Run generate_qr in issue mode; returns {'Ticket ID': ..., 'QR PNG': ..., ...} from its output."""
    code = generate_qr.main(["--db", str(db_path), "--out", str(tmp_path / "qr"), "--boarding",
                             "Central Bus Stand", "--destination", "Airport", *extra])
    assert code == 0
    out = capsys.readouterr().out
    return dict(line.split(": ", 1) for line in out.splitlines() if ": " in line), out


def add_boardings(db_path, trip_id="R1-outbound-TEST", n=2):
    """A STOPPED session with boardings for Track IDs 1..n; returns their boarding IDs."""
    now = utc_now()
    conn = Database(db_path).connect()
    try:
        insert_session(conn, session_id="cli-session", source_type="video", source_label="clip.mp4",
                       status="STOPPED", started_at=now - timedelta(minutes=5), station="City Hospital",
                       trip_id=trip_id)
        return [insert_boarding(conn, session_id="cli-session", track_id=i, boarded_at=now - timedelta(minutes=4),
                                direction="top_to_bottom", station="City Hospital", trip_id=trip_id)
                for i in range(1, n + 1)]
    finally:
        conn.close()


def query(db_path, sql, *params):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def test_init_db_creates_every_table(db_path, capsys):
    assert init_db.main(["--db", str(db_path)]) == 0
    tables = {r[0] for r in query(db_path, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert set(init_db.TABLES) <= tables
    out = capsys.readouterr().out
    assert f"Database: {db_path}" in out and "validation_events" in out


def test_init_db_is_idempotent(db_path):
    assert init_db.main(["--db", str(db_path)]) == 0
    assert init_db.main(["--db", str(db_path)]) == 0
    assert query(db_path, "SELECT value FROM system_settings WHERE key = 'schema_version'") == [("1",)]


def test_init_db_rejects_bad_db_suffix(tmp_path, capsys):
    assert init_db.main(["--db", str(tmp_path / "x.txt")]) == 1
    assert "database.path" in capsys.readouterr().err


def test_generated_png_decodes_to_stored_token_and_validates(db_path, tmp_path, capsys):
    info, out = issue(db_path, tmp_path, capsys)
    decoded = QRScanner().decode_image(info["QR PNG"])
    assert len(decoded) == 1
    parsed = parse_payload(decoded[0].payload)
    assert parsed.wellformed and decoded[0].payload.startswith("TPD1:")
    stored = query(db_path, "SELECT qr_token_hash FROM tickets WHERE ticket_id = ?", info["Ticket ID"])
    assert stored == [(hash_token(parsed.token),)]
    assert parsed.token not in out  # token printed only with --show-token

    assert validate_qr.main(["--db", str(db_path), "--image", info["QR PNG"]]) == 0
    assert "Valid (VALID)" in capsys.readouterr().out
    assert query(db_path, "SELECT outcome, channel FROM validation_events") == [("VALID", "cli")]


def test_show_token_prints_the_token(db_path, tmp_path, capsys):
    info, out = issue(db_path, tmp_path, capsys, "--show-token")
    token = parse_payload(QRScanner().decode_image(info["QR PNG"])[0].payload).token
    assert info["Token"] == token


def test_second_boarding_is_already_used(db_path, tmp_path, capsys):
    info, _ = issue(db_path, tmp_path, capsys)
    first, second = add_boardings(db_path)
    assert validate_qr.main(["--db", str(db_path), "--image", info["QR PNG"], "--boarding-id", str(first)]) == 0
    assert validate_qr.main(["--db", str(db_path), "--image", info["QR PNG"], "--boarding-id", str(second)]) == 10
    assert "ALREADY_USED" in capsys.readouterr().out
    rows = query(db_path, "SELECT boarding_id, outcome, trip_id FROM validation_events ORDER BY validation_id")
    assert rows == [(first, "VALID", "R1-outbound-TEST"), (second, "ALREADY_USED", "R1-outbound-TEST")]


def test_trip_id_inherited_from_running_session(db_path, tmp_path, capsys):
    info, _ = issue(db_path, tmp_path, capsys)
    conn = Database(db_path).connect()
    try:
        insert_session(conn, session_id="live", source_type="webcam", source_label="webcam 0", status="RUNNING",
                       started_at=utc_now(), station="City Hospital", trip_id="TRIP-LIVE")
    finally:
        conn.close()
    assert validate_qr.main(["--db", str(db_path), "--ticket-id", info["Ticket ID"].lower()]) == 0
    assert query(db_path, "SELECT trip_id, credential_type FROM validation_events") == [("TRIP-LIVE", "ticket_id")]


def test_json_qr_with_name_is_unrecognised_and_name_not_stored(db_path, tmp_path, capsys):
    info, _ = issue(db_path, tmp_path, capsys)
    png = write_qr_png(json.dumps({"ticket_id": info["Ticket ID"], "name": PASSENGER_NAME}),
                       tmp_path / "forged.png")
    assert validate_qr.main(["--db", str(db_path), "--image", str(png)]) == 10
    assert "Unrecognised QR code" in capsys.readouterr().out
    assert query(db_path, "SELECT outcome, reason, ticket_id FROM validation_events") == [
        ("INVALID", "Unrecognised QR code", None)]
    for (table,) in query(db_path, "SELECT name FROM sqlite_master WHERE type = 'table'"):
        for row in query(db_path, f"SELECT * FROM {table}"):
            assert not any(PASSENGER_NAME.lower() in str(v).lower() for v in row), table


def test_rotate_invalidates_the_old_png(db_path, tmp_path, capsys):
    info, _ = issue(db_path, tmp_path, capsys)
    old_png = shutil.copy(info["QR PNG"], tmp_path / "old.png")
    assert generate_qr.main(["--db", str(db_path), "--out", str(tmp_path / "qr"), "--ticket-id",
                             info["Ticket ID"], "--rotate"]) == 0
    capsys.readouterr()
    assert validate_qr.main(["--db", str(db_path), "--image", str(old_png)]) == 10
    assert "No ticket record matches this QR code" in capsys.readouterr().out
    assert validate_qr.main(["--db", str(db_path), "--image", info["QR PNG"]]) == 0


def test_validate_usage_errors_exit_1(db_path, tmp_path, capsys):
    blank = tmp_path / "blank.png"
    blank.write_bytes(b"not an image")
    assert validate_qr.main(["--db", str(db_path), "--image", str(blank)]) == 1
    assert validate_qr.main(["--db", str(db_path), "--ticket-id", "TKT-0001", "--boarding-id", "999"]) == 1
    assert validate_qr.main(["--db", str(db_path), "--ticket-id", "TKT-0001", "--station", "Mars"]) == 1
    with pytest.raises(SystemExit) as exc:
        validate_qr.main(["--db", str(db_path)])  # one of --image/--token/--ticket-id is required
    assert exc.value.code == 1
    assert query(db_path, "SELECT COUNT(*) FROM validation_events") == [(0,)]


def test_generate_qr_rejects_bad_input(db_path, tmp_path, capsys):
    base = ["--db", str(db_path), "--out", str(tmp_path / "qr")]
    assert generate_qr.main(base + ["--boarding", "Airport", "--destination", "Nowhere"]) == 1
    with pytest.raises(SystemExit) as exc:
        generate_qr.main(base + ["--boarding", "Central Bus Stand", "--destination", "Airport",
                                 "--passenger-ref", PASSENGER_NAME])
    assert exc.value.code == 1
    with pytest.raises(SystemExit):
        generate_qr.main(base + ["--rotate"])
    assert generate_qr.main(base + ["--ticket-id", "TKT-NOPE", "--rotate"]) == 1
    assert query(db_path, "SELECT COUNT(*) FROM tickets") == [(0,)]
