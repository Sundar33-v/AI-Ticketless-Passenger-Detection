"""Streamlit AppTest of the conductor dashboard and the Ticket Check page (AC13, M2, M3, N-a, N-e)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import generate_dummy_data
from app.analytics import TOTAL_PASSENGER_CAPTION, DashboardMetrics
from app.database import Database, utc_now
from app.passenger_service import insert_boarding, insert_session
from app.qr_service import encode_payload, write_qr_png
from app.settings_schema import AppConfig
from app.ticket_validator import TicketValidator, build_journey, issue_ticket

TZ = ZoneInfo("Asia/Kolkata")
LABELS = ["Valid", "Invalid", "Total Passenger", "Total Detection", "Expired Tickets", "Boarding Events",
          "Tracking IDs"]
TABLES = ("system_settings", "passengers", "tickets", "detection_sessions", "detection_records",
          "boarding_events", "validation_events")
PAGE = "1_Ticket_Check.py"
NO_STATION = "(not set – skip station checks)"


# ------------------------------------------------------------------ helpers
def today_local():
    return datetime.now(TZ).date()


def make_demo(db_path, day=None):
    day = day or today_local()
    assert generate_dummy_data.main(["--db", str(db_path), "--date", day.isoformat(), "--no-qr-images"]) == 0


def row_counts(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    finally:
        conn.close()


def query(db_path, sql, *params):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def issue(db_path, *, expired=False, ticket_id=None):
    """Issue a ticket valid now (or one that expired yesterday); returns (ticket_id, token)."""
    cfg = AppConfig.from_dict({})
    database = Database(db_path)
    database.initialize()
    conn = database.connect()
    try:
        kwargs = {}
        travel = today_local()
        if expired:
            travel = travel - timedelta(days=1)
            kwargs = {"valid_from": datetime.combine(travel, datetime.min.time(), tzinfo=TZ) + timedelta(hours=5),
                      "expires_at": datetime.combine(travel, datetime.min.time(), tzinfo=TZ) + timedelta(hours=23)}
        record, token = issue_ticket(conn, route=cfg.route, tz=TZ, boarding_station="Central Bus Stand",
                                     destination_station="Airport", travel_date=travel, ticket_id=ticket_id,
                                     **kwargs)
        return record.ticket_id, token
    finally:
        conn.close()


def add_session(db_path, *, status="STOPPED", tracks=0, trip_id="R1-outbound-DASH"):
    """A session (heartbeat = now) with boardings for Track IDs 1..tracks; returns boarding IDs."""
    now = utc_now()
    database = Database(db_path)
    database.initialize()
    conn = database.connect()
    try:
        insert_session(conn, session_id="dash-session", source_type="webcam", source_label="webcam 0",
                       status=status, started_at=now, station="City Hospital", trip_id=trip_id)
        return [insert_boarding(conn, session_id="dash-session", track_id=i, boarded_at=now,
                                direction="top_to_bottom", station="City Hospital", trip_id=trip_id)
                for i in range(1, tracks + 1)]
    finally:
        conn.close()


def validate(db_path, ticket_id, boarding_id):
    cfg = AppConfig.from_dict({})
    conn = Database(db_path).connect()
    try:
        return TicketValidator(tz=TZ).validate(conn, ticket_id, credential_type="ticket_id",
                                               journey=build_journey(cfg, now=utc_now()), channel="cli",
                                               boarding_id=boarding_id)
    finally:
        conn.close()


def run(at):
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def metric_values(at):
    return [m.value for m in at.metric]


def rendered_texts(at):
    """Text in N-a scope: everything except the title and the sidebar 'Database: ' caption."""
    texts = []
    for kind in ("markdown", "caption", "info", "warning", "error", "success", "header", "subheader"):
        for element in getattr(at, kind):
            value = str(element.value)
            if kind == "caption" and value.startswith("Database: "):
                continue
            texts.append(value)
    for metric in at.metric:
        texts += [metric.label, str(metric.value), metric.proto.help]
    for frame in at.dataframe:
        texts += [str(c) for c in frame.value.columns]
        texts += [str(v) for v in frame.value.to_numpy().ravel()]
    return texts


def submit_ticket_id(at, ticket_id):
    at.radio[0].set_value("Enter ticket ID")
    at.text_input[0].input(ticket_id)
    at.button[0].click()
    return run(at)


def selectbox(at, label):
    return next(s for s in at.selectbox if s.label == label)


# ------------------------------------------------------------------ main page
def test_empty_db_renders_seven_zero_metrics_and_info(dashboard_app):
    at = run(dashboard_app)
    assert [m.label for m in at.metric] == LABELS
    assert metric_values(at) == ["0"] * 7
    assert any("No records yet" in i.value for i in at.info)
    assert any("No boarding events in this period." == c.value for c in at.caption)


def test_metric_labels_follow_dashboard_metrics_order(dashboard_app):
    at = run(dashboard_app)
    assert [m.label for m in at.metric] == list(DashboardMetrics.LABELS) == LABELS
    assert len(at.metric) == 7


def test_total_passenger_caption(dashboard_app, dashboard_db):
    make_demo(dashboard_db)
    at = run(dashboard_app)
    captions = [c.value for c in at.caption]
    assert TOTAL_PASSENGER_CAPTION in captions
    assert any("boarded Track IDs not linked to a valid ticket" in c for c in captions)
    assert not any("awaiting ticket verification" in t for t in rendered_texts(at))


def test_demo_db_shows_demo_values(dashboard_app, dashboard_db):
    make_demo(dashboard_db)
    at = run(dashboard_app)
    assert metric_values(at) == ["7", "4", "16", "50", "2", "15", "16"]
    assert any(c.value.startswith("Demo data: ") and "synthetic rows present" in c.value for c in at.caption)
    selectbox(at, "Period").set_value("all")
    run(at)
    assert metric_values(at) == ["8", "4", "18", "56", "2", "17", "18"]


def test_status_filter_applies_to_tables_only(dashboard_app, dashboard_db):
    make_demo(dashboard_db)
    at = run(dashboard_app)
    at.multiselect[0].set_value(["EXPIRED"])
    run(at)
    assert metric_values(at) == ["7", "4", "16", "50", "2", "15", "16"]
    boardings, validations = (frame.value for frame in at.dataframe)
    assert set(validations["Outcome"]) == {"Expired"} and len(validations) == 2
    assert set(boardings["Verification status"]) == {"Expired"}


def test_reruns_write_nothing(dashboard_app, dashboard_db):
    make_demo(dashboard_db)
    at = run(dashboard_app)
    before = row_counts(dashboard_db)
    run(at)
    run(at)
    assert row_counts(dashboard_db) == before


def test_rendered_text_uses_verification_wording(dashboard_app, dashboard_db):
    make_demo(dashboard_db)
    at = run(dashboard_app)
    texts = rendered_texts(at)
    assert texts and not [t for t in texts if "ticketless" in t.lower()]
    assert "Ticket verification required" in " ".join(texts)


def test_not_running_shows_message_and_no_preview(dashboard_app, dashboard_db):
    make_demo(dashboard_db)
    at = run(dashboard_app)
    assert any(i.value.startswith("Detection is not running") for i in at.info)
    assert len(at.get("image")) == 0


def test_running_session_shows_source(dashboard_app, dashboard_db):
    add_session(dashboard_db, status="RUNNING")
    at = run(dashboard_app)
    assert any(s.value.startswith("Detection running – webcam 0") for s in at.success)
    assert not any(i.value.startswith("Detection is not running") for i in at.info)


def test_cache_is_keyed_by_db_path(open_dashboard, tmp_path):
    demo, empty = tmp_path / "demo.db", tmp_path / "empty.db"
    make_demo(demo)
    first = run(open_dashboard(demo))
    second = run(open_dashboard(empty, clear_cache=False))
    assert metric_values(first) == ["7", "4", "16", "50", "2", "15", "16"]
    assert metric_values(second) == ["0"] * 7
    assert any(c.value == f"Database: {empty}" for c in second.caption)


def test_invalid_config_shows_error(open_dashboard, tmp_path, monkeypatch):
    bad = tmp_path / "bad.yaml"
    bad.write_text("dashboard:\n  refresh_sec: 0\n", encoding="utf-8")
    monkeypatch.setenv("TPD_CONFIG", str(bad))
    at = open_dashboard(tmp_path / "x.db")
    at.run()
    assert any("dashboard.refresh_sec" in e.value for e in at.error)
    assert len(at.metric) == 0


# ------------------------------------------------------------- Ticket Check
def test_ticket_check_enter_ticket_id_records_one_validation(open_dashboard, dashboard_db):
    ticket_id, _ = issue(dashboard_db)
    at = run(open_dashboard(dashboard_db, PAGE))
    submit_ticket_id(at, ticket_id.lower())
    assert any(s.value.startswith("Valid – ") for s in at.success)
    rows = query(dashboard_db, "SELECT outcome, channel, credential_type, station FROM validation_events")
    assert rows == [("VALID", "dashboard", "ticket_id", "City Hospital")]
    run(at)  # a plain rerun without submitting writes nothing
    assert len(query(dashboard_db, "SELECT * FROM validation_events")) == 1


def test_ticket_check_qr_upload(open_dashboard, dashboard_db, tmp_path):
    ticket_id, token = issue(dashboard_db)
    png = write_qr_png(encode_payload(token), tmp_path / "ticket.png")
    at = run(open_dashboard(dashboard_db, PAGE))
    at.file_uploader[0].set_value(("ticket.png", png.read_bytes(), "image/png"))
    at.button[0].click()
    run(at)
    assert any(ticket_id in s.value for s in at.success)
    assert query(dashboard_db, "SELECT outcome, credential_type FROM validation_events") == [("VALID", "qr_token")]


def test_empty_submit_writes_nothing_unless_no_ticket_ticked(open_dashboard, dashboard_db):
    (boarding,) = add_session(dashboard_db, tracks=1)
    at = run(open_dashboard(dashboard_db, PAGE))
    at.button[0].click()
    run(at)
    assert any("Nothing was checked" in i.value for i in at.info)
    assert query(dashboard_db, "SELECT COUNT(*) FROM validation_events") == [(0,)]
    selectbox(at, "Boarding event (optional)").set_value(boarding)
    at.checkbox[0].check()
    at.button[0].click()
    run(at)
    assert any(w.value.startswith("Ticket verification required – ") for w in at.warning)
    assert query(dashboard_db, "SELECT outcome, credential_type, boarding_id FROM validation_events") == [
        ("VERIFICATION_REQUIRED", "none", boarding)]


def test_selector_lists_failed_and_unverified_boardings(open_dashboard, dashboard_db):
    expired_id, _ = issue(dashboard_db, expired=True)
    valid_id, _ = issue(dashboard_db)
    failed, verified, pending = add_session(dashboard_db, tracks=3)
    assert validate(dashboard_db, expired_id, failed).outcome.value == "EXPIRED"
    assert validate(dashboard_db, valid_id, verified).outcome.value == "VALID"

    at = run(open_dashboard(dashboard_db, PAGE))
    box = selectbox(at, "Boarding event (optional)")
    assert box.options[0] == "(no boarding – desk check)"
    hhmm = datetime.now(TZ).strftime("%H:%M")
    labels = box.options[1:]
    assert len(labels) == 2
    assert any(o.startswith("Track ID 1 – ") and o.endswith(" – Expired") for o in labels)
    assert any(o.startswith("Track ID 3 – ") and o.endswith(" – Ticket verification required") for o in labels)
    assert not any(o.startswith("Track ID 2 ") for o in labels)
    assert all(len(o.split(" – ")[1]) == len(hhmm) for o in labels)
    assert "re-checks (VALID)" in box.help

    # Resolving the failed verification: a valid ticket checked against the EXPIRED boarding.
    new_ticket, _ = issue(dashboard_db)
    box.set_value(failed)
    submit_ticket_id(at, new_ticket)
    status = query(dashboard_db, "SELECT verification_status, ticket_id FROM boarding_events WHERE boarding_id = ?",
                   failed)
    assert status == [("VALID", new_ticket)]


def test_station_select_defaults_to_skip_when_station_not_set(open_dashboard, dashboard_db, monkeypatch):
    monkeypatch.setenv("TPD_CURRENT_STATION", "none")
    ticket_id, _ = issue(dashboard_db)
    at = run(open_dashboard(dashboard_db, PAGE))
    assert selectbox(at, "Current station").value == NO_STATION
    submit_ticket_id(at, ticket_id)
    assert query(dashboard_db, "SELECT outcome, station FROM validation_events") == [("VALID", None)]


def test_station_select_defaults_to_configured_station(open_dashboard, dashboard_db):
    at = run(open_dashboard(dashboard_db, PAGE))
    box = selectbox(at, "Current station")
    assert box.value == "City Hospital" and box.options[0] == NO_STATION


def test_selected_station_is_used_for_the_check(open_dashboard, dashboard_db):
    issue(dashboard_db, ticket_id="TKT-SHORT1")
    at = run(open_dashboard(dashboard_db, PAGE))
    selectbox(at, "Current station").set_value("Central Bus Stand")  # the ticket's boarding station
    at.radio[0].set_value("Enter ticket ID")
    at.text_input[0].input("TKT-SHORT1")
    at.button[0].click()
    run(at)
    assert query(dashboard_db, "SELECT outcome, station FROM validation_events") == [("VALID", "Central Bus Stand")]


def test_ticket_check_text_uses_verification_wording(open_dashboard, dashboard_db):
    add_session(dashboard_db, tracks=1)
    at = run(open_dashboard(dashboard_db, PAGE))
    texts = rendered_texts(at) + [o for s in at.selectbox for o in s.options] + [r.label for r in at.radio]
    assert not [t for t in texts if "ticketless" in t.lower()]


def test_dashboard_never_uses_camera_or_model():
    for path in (Path(__file__).resolve().parents[1] / "dashboard").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "ultralytics" not in source and "VideoCapture" not in source, path
