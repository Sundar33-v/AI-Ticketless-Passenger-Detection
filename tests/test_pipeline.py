"""Pipeline integration (FakeTracker + FakeScanner + real tmp DB) and app.main exit codes."""

from __future__ import annotations

import io
import logging
from datetime import datetime, timedelta, timezone

import cv2
import numpy as np
import pytest
import yaml

import app.main as main_mod
from app.alert_service import AlertService
from app.overlay import OverlayState, draw_overlay
from app.passenger_service import BoardingRules, PassengerService
from app.pipeline import UNLINKED_QR_MESSAGE, DetectionPipeline
from app.qr_service import QRDetection, encode_payload
from app.settings_schema import AppConfig
from app.ticket_validator import TicketValidator, build_journey, issue_ticket
from app.tracker import TrackedPerson

T0 = datetime(2026, 1, 15, 5, 30, tzinfo=timezone.utc)  # 11:00 IST
BLANK = np.zeros((480, 640, 3), dtype=np.uint8)


def person(tid, bottom_y, x1=100.0, hits=5):
    return TrackedPerson(tid, (x1, bottom_y - 100.0, x1 + 60.0, float(bottom_y)), 0.87, hits=hits)


class FakeTracker:
    def __init__(self, frames):
        self.frames = list(frames)

    def track(self, _frame):
        return self.frames.pop(0) if self.frames else []


class FakeScanner:
    """Returns the scripted detections for the n-th decode call (0-based)."""

    def __init__(self, script):
        self.script = dict(script)
        self.calls = 0

    def decode_frame(self, _frame):
        result = self.script.get(self.calls, [])
        self.calls += 1
        return result


def make_cfg(tmp_path, **extra):
    data = {"runtime": {"preview_path": str(tmp_path / "runtime" / "latest.jpg"), "preview_interval_sec": 0},
            "qr": {"scan_every_n_frames": 1, "rescan_cooldown_sec": 0}, "boarding": {"min_frames_seen": 1}}
    for key, value in extra.items():
        data.setdefault(key, {}).update(value)
    return AppConfig.from_dict(data)


@pytest.fixture
def build(db, tmp_path):
    services = []

    def _build(frames, scan=None, cfg=None, mot=None):
        cfg = cfg or make_cfg(tmp_path)
        service = PassengerService(db, cfg, clock=lambda: T0)
        services.append(service)
        journey = build_journey(cfg, now=T0)
        service.create_session("video", "clip.mp4", journey.current_station, journey.trip_id)
        return DetectionPipeline(
            tracker=FakeTracker(frames), service=service,
            boarding_rules=BoardingRules(cfg.boarding.entry_line, cfg.boarding.min_frames_seen),
            validator=TicketValidator(tz=cfg.app.timezone), alerts=AlertService(), journey=journey, cfg=cfg,
            scanner=FakeScanner(scan or {}), mot_writer=mot)

    yield _build
    for service in services:
        service.close()


def run_frames(pipeline, n, start=T0):
    for i in range(n):
        pipeline.process_frame(BLANK, start + timedelta(seconds=i * 0.1))
    pipeline.flush(start + timedelta(seconds=n * 0.1))


def rows(pipeline, sql, *params):
    return pipeline.service.conn.execute(sql, params).fetchall()


def ticket(pipeline, cfg, tid="TKT-P1"):
    _, token = issue_ticket(pipeline.service.conn, route=cfg.route, tz=cfg.app.timezone,
                            boarding_station="Central Bus Stand", destination_station="Airport",
                            travel_date="2026-01-15", ticket_id=tid, now=T0 - timedelta(hours=1))
    return token


# ------------------------------------------------------------------ pipeline
def test_boarding_starts_as_verification_required(build):
    p = build([[person(1, 200)], [person(1, 300)], [person(1, 320)]])
    run_frames(p, 3)
    boarding = rows(p, "SELECT * FROM boarding_events")
    assert len(boarding) == 1 and boarding[0]["verification_status"] == "VERIFICATION_REQUIRED"
    assert boarding[0]["track_id"] == 1 and boarding[0]["trip_id"] == p.journey.trip_id
    assert len(rows(p, "SELECT * FROM detection_records")) == 1  # throttled: first sight only
    assert any("Ticket verification required" in m for m in p.alerts.messages)


def test_qr_linked_by_bbox(build, tmp_path):
    cfg = make_cfg(tmp_path)
    inside = ((110.0, 210.0), (150.0, 210.0), (150.0, 250.0), (110.0, 250.0))  # centre in track 2's box
    p = build([[person(1, 200, x1=400), person(2, 200)], [person(1, 300, x1=400), person(2, 300)],
               [person(1, 310, x1=400), person(2, 310)]], cfg=cfg)
    token = ticket(p, cfg)
    p.scanner.script = {2: [QRDetection(encode_payload(token), inside)]}
    run_frames(p, 3)
    v = rows(p, "SELECT v.outcome, b.track_id FROM validation_events v JOIN boarding_events b USING (boarding_id)")
    assert [tuple(r) for r in v] == [("VALID", 2)]
    assert p.service.boardings[2].status == "VALID" and p.service.boardings[1].status == "VERIFICATION_REQUIRED"


def test_qr_fallback_to_single_recent_boarding(build, tmp_path):
    cfg = make_cfg(tmp_path)
    p = build([[person(1, 200)], [person(1, 300)], [person(1, 310)]], cfg=cfg)
    token = ticket(p, cfg)
    p.scanner.script = {2: [QRDetection(token, None)]}
    run_frames(p, 3)
    assert [r["outcome"] for r in rows(p, "SELECT outcome FROM validation_events")] == ["VALID"]


def test_qr_held_by_non_boarded_person_is_not_linked_to_recent_boarding(build, tmp_path):
    cfg = make_cfg(tmp_path)
    held = ((420.0, 90.0), (440.0, 90.0), (440.0, 110.0), (420.0, 110.0))  # centre in track 2's box
    # Track 1 boards; track 2 is tracked above the entry line (never boards) and holds the QR.
    p = build([[person(1, 200), person(2, 150, x1=400)], [person(1, 300), person(2, 160, x1=400)],
               [person(1, 310), person(2, 170, x1=400)]], cfg=cfg)
    token = ticket(p, cfg)
    p.scanner.script = {2: [QRDetection(encode_payload(token), held)]}
    run_frames(p, 3)
    assert list(p.service.boardings) == [1]
    assert rows(p, "SELECT * FROM validation_events") == []
    assert rows(p, "SELECT status FROM tickets")[0]["status"] != "USED"
    assert p.service.boardings[1].status == "VERIFICATION_REQUIRED"
    assert UNLINKED_QR_MESSAGE in p.alerts.messages


def test_qr_outside_every_box_uses_single_recent_boarding(build, tmp_path):
    cfg = make_cfg(tmp_path)
    nowhere = ((600.0, 400.0), (620.0, 400.0), (620.0, 420.0), (600.0, 420.0))
    p = build([[person(1, 200)], [person(1, 300)], [person(1, 310)]], cfg=cfg)
    token = ticket(p, cfg)
    p.scanner.script = {2: [QRDetection(encode_payload(token), nowhere)]}
    run_frames(p, 3)
    assert [r["outcome"] for r in rows(p, "SELECT outcome FROM validation_events")] == ["VALID"]


def test_qr_seen_state_is_pruned_at_flush(build, tmp_path):
    cfg = make_cfg(tmp_path, qr={"rescan_cooldown_sec": 5.0})
    p = build([[person(1, 200)], [person(1, 300)], [person(1, 310)], [person(1, 320)]], cfg=cfg)
    token = ticket(p, cfg)
    p.scanner.script = {2: [QRDetection(encode_payload(token), None)], 3: [QRDetection(encode_payload(token), None)]}
    for i in range(3):
        p.process_frame(BLANK, T0 + timedelta(seconds=i * 0.1))
    p.flush(T0 + timedelta(seconds=1))
    assert len(p._qr_seen) == 1  # still inside the cooldown
    p.flush(T0 + timedelta(seconds=10))
    assert p._qr_seen == {}
    p.process_frame(BLANK, T0 + timedelta(seconds=11))  # same QR, same boarding after the cooldown
    assert len(rows(p, "SELECT * FROM validation_events")) == 1


def test_unlinked_qr_is_not_recorded(build, tmp_path):
    cfg = make_cfg(tmp_path)
    p = build([[person(1, 200)], [person(1, 210)]], cfg=cfg)  # nobody boards
    token = ticket(p, cfg)
    p.scanner.script = {1: [QRDetection(token, None)], 0: [QRDetection("not a ticket", None)]}
    run_frames(p, 2)
    assert rows(p, "SELECT * FROM validation_events") == []
    assert UNLINKED_QR_MESSAGE in p.alerts.messages


def test_repeat_scans_are_deduplicated(build, tmp_path):
    cfg = make_cfg(tmp_path)
    p = build([[person(1, 200)]] + [[person(1, 300 + i)] for i in range(5)], cfg=cfg)
    token = ticket(p, cfg)
    p.scanner.script = {i: [QRDetection(token, None)] for i in range(1, 6)}
    run_frames(p, 6)
    assert len(rows(p, "SELECT * FROM validation_events")) == 1


def test_overlay_draws_on_blank_frame(build):
    p = build([[person(1, 200)], [person(1, 300)]])
    run_frames(p, 2)
    out = p.render(BLANK.copy())
    assert out.shape == BLANK.shape and out.any()
    state = OverlayState(people=[person(3, 300)], status_by_track={3: "EXPIRED"}, messages=["x"])
    assert draw_overlay(BLANK.copy(), state).any()


def test_mot_export_lines(build):
    mot = io.StringIO()
    p = build([[person(4, 200)], [], [person(4, 300)]], mot=mot)
    run_frames(p, 3)
    assert mot.getvalue().splitlines() == ["1,4,100.00,100.00,60.00,100.00,0.8700,-1,-1,-1",
                                           "3,4,100.00,200.00,60.00,100.00,0.8700,-1,-1,-1"]


def test_preview_written_atomically(build, tmp_path):
    cfg = make_cfg(tmp_path, runtime={"preview_interval_sec": 1.0})
    p = build([[person(1, 200)]], cfg=cfg)
    run_frames(p, 1)
    assert (tmp_path / "runtime" / "latest.jpg").is_file()
    assert not (tmp_path / "runtime" / "latest.jpg.tmp").exists()


def test_session_trip_id_matches_dashboard_journey(build, cfg):
    p = build([])
    session = rows(p, "SELECT trip_id FROM detection_sessions")[0]["trip_id"]
    dashboard = build_journey(cfg, now=T0 + timedelta(hours=8), station="Airport", trip_id=None)
    assert session == dashboard.trip_id == "R1-outbound-2026-01-15"
    next_day = build_journey(cfg, now=T0 + timedelta(days=1), trip_id=session)  # inherited from boarding
    assert next_day.trip_id == session


# ---------------------------------------------------------------- app.main
@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    yield
    for handler in [h for h in root.handlers if h not in before]:
        root.removeHandler(handler)
        handler.close()
    root.setLevel(level)


def config_file(tmp_path, **extra):
    data = {"logging": {"dir": str(tmp_path / "logs")},
            "runtime": {"preview_path": str(tmp_path / "runtime" / "latest.jpg")},
            "detection": {"model_path": str(tmp_path / "models" / "missing.pt")},
            "camera": {"open_timeout_sec": 1, "reconnect": {"enabled": False}}}
    for key, value in extra.items():
        data.setdefault(key, {}).update(value)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return str(path)


@pytest.fixture
def spy_model(monkeypatch):
    calls = []

    def fake_load(cfg):
        calls.append(cfg)
        return object()

    monkeypatch.setattr(main_mod, "load_model", fake_load)
    return calls


@pytest.mark.parametrize("path, message", [("data/missing.mp4", "Video file not found:\ndata/missing.mp4"),
                                           ("notes.txt", "Unsupported video file type '.txt'")])
def test_bad_video_exits_2_before_model_and_db(tmp_path, caplog, spy_model, restore_logging, path, message):
    db_file = tmp_path / "x.db"
    code = main_mod.main(["--config", config_file(tmp_path), "--source", "video", "--path", path,
                          "--db", str(db_file), "--no-display"])
    assert code == 2 and message in caplog.text
    assert spy_model == [] and not db_file.exists()


def test_missing_model_exits_4(tmp_path, caplog, restore_logging):
    db_file = tmp_path / "x.db"
    code = main_mod.main(["--config", config_file(tmp_path), "--source", "webcam", "--no-display",
                          "--max-frames", "1", "--db", str(db_file)])
    assert code == 4
    assert str(tmp_path / "models" / "missing.pt") in caplog.text and "YOLO('yolov8n.pt')" in caplog.text
    assert not db_file.exists()


def test_config_error_exits_1(tmp_path, caplog, restore_logging):
    bad = tmp_path / "bad.yaml"
    bad.write_text("camera:\n  source_type: cctv\n", encoding="utf-8")
    assert main_mod.main(["--config", str(bad), "--db", str(tmp_path / "x.db")]) == 1
    assert "camera.source_type" in caplog.text


def test_config_error_handled_before_value_error(tmp_path, caplog, spy_model, monkeypatch, restore_logging):
    def raising(*a, **k):
        raise main_mod.ConfigError("journey.trip_id: broken")

    real_build_journey = main_mod.build_journey
    monkeypatch.setattr(main_mod, "build_journey", raising)
    args = ["--config", config_file(tmp_path), "--source", "webcam", "--no-display", "--db", str(tmp_path / "x.db")]
    assert main_mod.main(args) == 1
    assert "Configuration error: journey.trip_id: broken" in caplog.text
    monkeypatch.setattr(main_mod, "build_journey", real_build_journey)
    caplog.clear()
    assert main_mod.main(args + ["--station", "Mars"]) == 1
    assert "Station 'Mars' is not on route R1" in caplog.text


def test_unreachable_phone_exits_2_and_marks_session_failed(tmp_path, caplog, spy_model, restore_logging):
    import sqlite3

    db_file = tmp_path / "x.db"
    code = main_mod.main(["--config", config_file(tmp_path), "--source", "phone", "--url",
                          "http://127.0.0.1:9/video", "--no-display", "--max-frames", "1", "--db", str(db_file)])
    assert code == 2 and "same Wi-Fi network" in caplog.text
    with sqlite3.connect(db_file) as conn:
        status, error = conn.execute("SELECT status, last_error FROM detection_sessions").fetchone()
    conn.close()
    assert status == "FAILED" and "127.0.0.1:9" in error


def test_webcam_fallback_updates_session_source(tmp_path, spy_model, monkeypatch, restore_logging):
    import sqlite3

    class FallbackSource:  # stands in for VideoSource after the phone failed and the webcam opened
        def __init__(self, camera):
            self.source_type, self.finished = camera.source_type, False

        def open(self):
            self.source_type = "webcam"
            return self

        def frames(self):
            yield from [BLANK] * 2

        def release(self):
            pass

    monkeypatch.setattr(main_mod, "VideoSource", FallbackSource)
    monkeypatch.setattr(main_mod, "PersonTracker", lambda *a, **k: FakeTracker([]))
    db_file = tmp_path / "x.db"
    code = main_mod.main(["--config", config_file(tmp_path), "--source", "phone", "--fallback-webcam",
                          "--camera-index", "1", "--no-display", "--no-qr", "--db", str(db_file)])
    assert code == 0
    with sqlite3.connect(db_file) as conn:
        row = conn.execute("SELECT source_type, source_label, status FROM detection_sessions").fetchone()
    conn.close()
    assert row == ("webcam", "webcam 1", "STOPPED")


def test_main_processes_recorded_video(tmp_path, spy_model, monkeypatch, restore_logging):
    import sqlite3

    video = tmp_path / "clip.avi"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 10, (64, 48))
    for i in range(5):
        writer.write(np.full((48, 64, 3), i * 20, dtype=np.uint8))
    writer.release()
    monkeypatch.setattr(main_mod, "PersonTracker", lambda *a, **k: FakeTracker([[person(1, 20)]] * 5))
    db_file, mot = tmp_path / "x.db", tmp_path / "mot.txt"
    code = main_mod.main(["--config", config_file(tmp_path), "--path", str(video), "--no-display",
                          "--db", str(db_file), "--export-mot", str(mot)])
    assert code == 0
    with sqlite3.connect(db_file) as conn:
        status, frames, label = conn.execute(
            "SELECT status, frames_processed, source_label FROM detection_sessions").fetchone()
        detections = conn.execute("SELECT COUNT(*) FROM detection_records").fetchone()[0]
    conn.close()
    assert (status, frames, label) == ("STOPPED", 5, "clip.avi") and detections >= 1
    assert len(mot.read_text(encoding="utf-8").splitlines()) == 5
    assert (tmp_path / "runtime" / "bytetrack_effective.yaml").is_file()
