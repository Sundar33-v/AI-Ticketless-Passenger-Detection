"""app/system_status.py (detection health rule) and the time-free PageContext / header model."""

from __future__ import annotations

import ast
import os
from dataclasses import fields, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.analytics import SourceStatus, source_status
from app.passenger_service import insert_session
from app.settings_schema import AppConfig, RuntimeConfig
from app.system_status import (CAMERA_DISCONNECTED, FAILED, NO_SESSION, NOT_RESPONDING, ONLINE, STOPPED,
                               detection_health, format_age, is_camera_error, preview_mtime, route_label)
from app.video_source import _phone_help

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 1, 15, 5, 30, tzinfo=timezone.utc)
RT = RuntimeConfig()
IST = ZoneInfo("Asia/Kolkata")


def src(status="RUNNING", hb=T0, error=None, sid="abcdef123456"):
    return SourceStatus(running=False, session_id=sid, status=status, source_label="webcam 0", started_at=T0,
                        last_heartbeat_at=hb, last_error=error, station="City Hospital", trip_id="T1")


def test_states():
    assert detection_health(SourceStatus(False), T0, RT, None).state == NO_SESSION
    online = detection_health(src(), T0 + timedelta(seconds=10), RT, None)
    assert online.state == ONLINE and online.online and online.label == "Detection: Online"
    stale = detection_health(src(), T0 + timedelta(seconds=10.001), RT, None)
    assert stale.state == NOT_RESPONDING and not stale.online
    assert detection_health(src("FAILED", error="Could not open webcam at index 0."), T0, RT, None).state == \
        CAMERA_DISCONNECTED
    assert detection_health(src("FAILED", error="database is locked"), T0, RT, None).state == FAILED
    stopped = detection_health(src("STOPPED"), T0, RT, None)
    assert stopped.state == STOPPED and stopped.label == "Detection: Stopped" and stopped.feed == "off"


def test_two_day_old_heartbeat_label():
    h = detection_health(src(), T0 + timedelta(days=2, seconds=5), RT, None)
    assert (h.state, h.online, h.label) == (NOT_RESPONDING, False,
                                            "Detection: Not responding (last heartbeat 2 d ago)")


def test_online_matches_source_status_running(db):
    conn = db.connect()
    try:
        insert_session(conn, session_id="s", source_type="webcam", source_label="webcam 0", status="RUNNING",
                       started_at=T0, station=None, trip_id="T")
        for offset in (0, 5, 10, 11, 3600):
            now = T0 + timedelta(seconds=offset)
            s = source_status(conn, now, RT.heartbeat_stale_sec)
            assert s.running == (detection_health(s, now, RT, None).state == ONLINE)
    finally:
        conn.close()


def test_feed_states(tmp_path):
    frame = tmp_path / "f.jpg"
    frame.write_bytes(b"jpg")
    os.utime(frame, (T0.timestamp(), T0.timestamp()))
    now = T0 + timedelta(seconds=2)
    assert detection_health(src(hb=now), now, RT, preview_mtime(frame)).feed == "live"
    later = T0 + timedelta(seconds=60)
    assert detection_health(src(hb=later), later, RT, preview_mtime(frame)).feed == "waiting"
    assert detection_health(src(hb=now), now, RT, preview_mtime(tmp_path / "none.jpg")).feed == "waiting"
    assert detection_health(src(hb=now), now, replace(RT, preview_interval_sec=0), None).feed == "disabled"
    assert detection_health(src("STOPPED"), now, RT, preview_mtime(frame)).feed == "off"


@pytest.mark.parametrize("seconds, text", [(0, "0 s"), (59, "59 s"), (60, "1 min"), (3599, "59 min"),
                                           (3600, "1 h"), (86399, "23 h"), (2 * 86400 + 5, "2 d"), (-3, "0 s")])
def test_format_age(seconds, text):
    assert format_age(seconds) == text


def _sample(node, name_value):
    if isinstance(node, ast.Constant):
        return str(node.value)
    if isinstance(node, ast.JoinedStr):
        out = ""
        for part in node.values:
            if isinstance(part, ast.Constant):
                out += str(part.value)
            else:
                expr = part.value
                name = expr.id if isinstance(expr, ast.Name) else ""
                if name == "primary_err":
                    return None
                out += name_value if name == "source_type" else "x"
        return out
    if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_phone_help":
        return _phone_help("http://192.0.2.1:8080/video", "reason")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _sample(node.left, name_value)
    raise AssertionError(f"line {node.lineno}: unsupported message shape {type(node).__name__}")


def test_every_camera_error_message_is_classified():
    tree = ast.parse((ROOT / "app" / "video_source.py").read_text(encoding="utf-8"))
    sites = 0
    for node in ast.walk(tree):
        if (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
                and getattr(node.exc.func, "id", "") == "CameraConnectionError"):
            sites += 1
            for source_type in ("webcam", "phone", "video"):
                text = _sample(node.exc.args[0], source_type)
                if text is None:
                    continue
                if text.startswith("Unknown source type"):
                    assert not is_camera_error(text)
                else:
                    assert is_camera_error(text), (node.lineno, text)
    assert sites >= 10
    assert is_camera_error("webcam source 0 opened but returned no frames.")
    assert is_camera_error("Could not open webcam at index 1.\n\nWebcam fallback also failed: x")
    assert not is_camera_error("Unknown source type 'x'") and not is_camera_error(None)


def test_route_label():
    cfg = AppConfig.from_dict({})
    online = detection_health(src(), T0, RT, None)
    assert route_label(cfg, online, T0) == "Route R1 – City Express (outbound) · Station City Hospital · Trip T1"
    stopped = detection_health(src("STOPPED"), T0, RT, None)
    assert route_label(cfg, stopped, T0).endswith("· Station City Hospital · Trip (next run) R1-outbound-2026-01-15")
    no_route = replace(cfg, route=replace(cfg.route, route_id="", name=""))
    assert route_label(no_route, stopped, T0) == "Session abcdef12"
    assert route_label(no_route, detection_health(SourceStatus(False), T0, RT, None), T0) == \
        "No detection session yet"


# ------------------------------------------------------------------ PageContext / header (no frozen time)
def ctx_for(choice="today"):
    from dashboard.components.shell import PageContext

    cfg = AppConfig.from_dict({})
    return PageContext(cfg, IST, choice, None, ())


def test_page_context_has_no_time_fields():
    from dashboard.components.shell import PageContext

    names = {f.name for f in fields(PageContext)}
    assert names == {"cfg", "tz", "period_choice", "custom", "statuses", "sidebar"}
    assert not any(t in n for n in names for t in ("now", "time", "date", "start", "end", "period_at"))


def test_period_at_rolls_forward():
    ctx = ctx_for("last_24_hours")
    t1, t2 = T0, T0 + timedelta(minutes=7)
    p1, p2 = ctx.period_at(t1), ctx.period_at(t2)
    from app.database import parse_iso

    assert parse_iso(p2.start) - parse_iso(p1.start) == t2 - t1 == parse_iso(p2.end) - parse_iso(p1.end)
    today = ctx_for("today")
    before = datetime.combine(date(2026, 1, 15), datetime.min.time(), tzinfo=IST) + timedelta(hours=23, minutes=59)
    assert today.period_at(before) != today.period_at(before + timedelta(minutes=2))


def test_header_model_uses_the_given_now(db):
    from dashboard.components.shell import header_model

    conn = db.connect()
    try:
        insert_session(conn, session_id="s", source_type="webcam", source_label="webcam 0", status="RUNNING",
                       started_at=T0 + timedelta(seconds=5), station=None, trip_id="T")
        ctx = ctx_for()
        fresh = header_model(conn, ctx, T0 + timedelta(seconds=6))
        assert fresh.health.online and fresh.health.label == "Detection: Online"
        late = header_model(conn, ctx, T0 + timedelta(seconds=5 + RT.heartbeat_stale_sec + 1))
        assert late.health.state == NOT_RESPONDING and not late.health.online
        assert header_model(conn, ctx, T0 + timedelta(seconds=35)).health.state == NOT_RESPONDING
        assert "Jan 2026" in late.clock and "IST" in late.clock
    finally:
        conn.close()


def _calls(func, name):
    return [n.lineno for n in ast.walk(func) if isinstance(n, ast.Call)
            and (getattr(n.func, "id", None) == name or getattr(n.func, "attr", None) == name)]


def test_fragments_take_utc_now_first_and_never_read_ctx_time():
    checked = 0
    for path in (ROOT / "dashboard").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.endswith("_fragment"):
                checked += 1
                now_calls, reads = _calls(node, "utc_now"), _calls(node, "read_conn")
                assert now_calls, f"{path.name}:{node.name} never calls utc_now()"
                assert not reads or min(now_calls) <= min(reads), f"{path.name}:{node.name}"
            if isinstance(node, ast.Attribute) and node.attr in ("now", "period") \
                    and isinstance(node.value, ast.Name) and node.value.id == "ctx":
                raise AssertionError(f"{path.name}:{node.lineno} reads ctx.{node.attr}")
    assert checked >= 5
