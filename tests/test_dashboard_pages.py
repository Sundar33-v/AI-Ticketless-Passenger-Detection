"""Streamlit AppTests of every dashboard page: router + direct, empty / demo / copy of the real DB."""

from __future__ import annotations

import logging
import re
import shutil
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from streamlit.testing.v1.element_tree import Block

from app.database import Database, to_iso, utc_now
from app.evaluation import NA_MESSAGE, Counts, EvaluationRecord, store_evaluation
from app.passenger_service import insert_boarding, insert_session
from app.video_source import _phone_help
from dashboard.components.cards import TAGLINE
from dashboard.components.metrics import MODEL_LABELS
from dashboard.components.nav import NAV_ITEMS
from dashboard.components.status import COMPACT_OFF
from test_dashboard import (TABLES, bell_label, frame_with, html_texts, make_demo, raw_html, rendered_texts,
                            row_counts, run, validation_rows)

ROOT = Path(__file__).resolve().parents[1]
PAGES = ["0_Dashboard.py", "2_Live_Feed.py", "3_Passenger_Tracking.py", "1_Ticket_Check.py", "4_Reports.py",
         "5_Settings.py"]
REAL_DB = ROOT / "data" / "ticketless.db"
STOPPED_SENTENCE = "Detection is not running. Start it with scripts\\run_detection.bat"


def prepare(kind, tmp_path):
    path = tmp_path / f"{kind}.db"
    if kind == "demo":
        make_demo(path)
    elif kind == "real":
        if not REAL_DB.exists():
            pytest.skip("data/ticketless.db not present")
        shutil.copy2(REAL_DB, path)  # byte copy; the original is never opened
        wal = REAL_DB.with_name("ticketless.db-wal")
        if wal.exists():
            shutil.copy2(wal, path.with_name(path.name + "-wal"))
        conn = sqlite3.connect(str(path))
        try:
            ok = conn.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            conn.close()
        if ok != "ok":
            pytest.skip(f"copy of the real DB is not consistent ({ok}); detection may be writing")
    Database(path).initialize()
    return path


def session(path, *, status, heartbeat_age=0.0, error=None, source_type="webcam", label="webcam 0"):
    now = utc_now()
    conn = Database(path).connect()
    try:
        insert_session(conn, session_id="page-session", source_type=source_type, source_label=label, status=status,
                       started_at=now - timedelta(seconds=heartbeat_age + 1), station="City Hospital", trip_id="T")
        conn.execute("UPDATE detection_sessions SET last_heartbeat_at = ?, last_error = ? WHERE session_id = ?",
                     (to_iso(now - timedelta(seconds=heartbeat_age)), error, "page-session"))
    finally:
        conn.close()


def open_page(open_dashboard, path, page, *, router):
    if not router:
        return run(open_dashboard(path, page))
    at = run(open_dashboard(path))
    at.switch_page(f"pages/{page}")
    return run(at)


def all_text(at):
    texts = rendered_texts(at) + [c.value for c in at.code] + [m.value for m in at.markdown]
    return texts + [str(s.options) for s in at.selectbox]


# ------------------------------------------------------------------ every page renders
@pytest.mark.parametrize("router", [False, True], ids=["direct", "router"])
@pytest.mark.parametrize("kind", ["empty", "demo", "real"])
@pytest.mark.parametrize("page", PAGES)
def test_every_page_renders(open_dashboard, tmp_path, page, kind, router):
    path = prepare(kind, tmp_path)
    before = row_counts(path)
    at = open_page(open_dashboard, path, page, router=router)
    assert not at.exception
    assert len(at.get("image")) == 0                      # detection is not online in these DBs
    texts = all_text(at)
    assert not [t for t in texts if "ticketless" in t.lower()], page
    assert any(c.value.startswith("Database: ") for c in at.caption)
    run(at)
    assert row_counts(path) == before                      # reruns write nothing


def test_dashboard_model_panel_na_and_values(open_dashboard, tmp_path):
    path = prepare("empty", tmp_path)
    dash = run(open_dashboard(path))
    assert len(dash.metric) == 0 and len(dash.get("plotly_chart")) == 2   # model evaluation: Reports + HTML metrics panel
    at = run(open_dashboard(path, "4_Reports.py"))
    assert sum(i.value == NA_MESSAGE for i in at.info) == 1

    other = tmp_path / "eval.db"
    database = Database(other)
    database.initialize()
    conn = database.connect()
    try:
        store_evaluation(conn, EvaluationRecord("import", "yolov8n.pt", "v1", "bus01", Counts(4, 10, 10, 8, 2, 2,
                                                                                             None, None, None, None)),
                         utc_now())
    finally:
        conn.close()
    at = run(open_dashboard(other, "4_Reports.py"))         # fresh AppTest + cleared caches
    assert not any(i.value == NA_MESSAGE for i in at.info)
    evaluation = dict(frame_with(at, "Item", "Value").itertuples(index=False))
    assert [evaluation[label] for label in MODEL_LABELS] == ["N/A", "80.0%", "80.0%", "80.0%"]
    assert evaluation["Dataset"] == "bus01" and evaluation["Model"].startswith("yolov8n.pt")
    assert len(run(open_dashboard(other)).get("plotly_chart")) == 2


def test_stopped_sentence_once_and_compact_caption(open_dashboard, tmp_path):
    path = prepare("empty", tmp_path)
    session(path, status="STOPPED")
    at = run(open_dashboard(path))
    assert not any(STOPPED_SENTENCE in i.value for i in at.info)
    assert COMPACT_OFF in " ".join(html_texts(at))
    live = open_page(open_dashboard, path, "2_Live_Feed.py", router=False)
    assert sum(STOPPED_SENTENCE in i.value for i in live.info) == 1
    assert not any(c.value == COMPACT_OFF for c in live.caption)


@pytest.mark.parametrize("status, age, chip", [("RUNNING", 0, "Detection: Online"),
                                               ("RUNNING", 60, "Detection: Not responding"),
                                               ("STOPPED", 0, "Detection: Stopped")])
def test_header_chip_reflects_real_state(open_dashboard, tmp_path, status, age, chip):
    path = prepare("empty", tmp_path)
    session(path, status=status, heartbeat_age=age)
    at = run(open_dashboard(path))
    header = " ".join(html_texts(at))
    assert chip in header and "Conductor console: Online" in header
    if chip != "Detection: Online":
        assert "Detection: Online" not in header


def test_camera_disconnected_panel(open_dashboard, tmp_path):
    path = prepare("empty", tmp_path)
    session(path, status="FAILED", error=_phone_help("http://192.0.2.1:8080/video", "no frames received"),
            source_type="phone", label="http://192.0.2.1:8080/video")
    at = open_page(open_dashboard, path, "2_Live_Feed.py", router=True)
    assert any(e.value.startswith("Camera disconnected – Unable to connect to Android phone camera") for e in at.error)
    assert "Camera: Disconnected" in " ".join(html_texts(at))
    assert len(at.get("image")) == 0


def test_database_error_is_reported_not_raised(open_dashboard, tmp_path, monkeypatch):
    import app.analytics
    import dashboard.components.shell as shell

    def broken(*args, **kwargs):
        raise sqlite3.DatabaseError("database disk image is malformed")

    path = prepare("empty", tmp_path)
    monkeypatch.setattr(app.analytics, "compute_metrics", broken)
    monkeypatch.setattr(shell, "header_model", broken)
    at = open_dashboard(path)
    at.run()
    assert not at.exception
    assert any(e.value == "Database error: database disk image is malformed" for e in at.error)
    header = " ".join(html_texts(at))
    assert "Conductor console: Offline" in header and "Detection: Unknown" in header
    assert bell_label(at) == "–"


def test_mark_all_as_read_clears_the_bell(open_dashboard, tmp_path):
    path = prepare("demo", tmp_path)
    at = open_page(open_dashboard, path, "2_Live_Feed.py", router=True)
    assert int(bell_label(at)) > 0
    next(b for b in at.button if b.label == "Mark all as read").click()
    run(at)
    assert bell_label(at) == "0"


def test_alert_panel_has_no_dataframes(open_dashboard, tmp_path):
    path = prepare("demo", tmp_path)
    at = open_page(open_dashboard, path, "2_Live_Feed.py", router=True)
    assert not at.dataframe                                  # detection is not online: no live-tracks table
    assert any(m.value.startswith("- ") and "val:" in m.value for m in at.markdown)


def add_failed_check(path):
    conn = Database(path).connect()
    try:
        conn.execute("INSERT INTO validation_events (validated_at, outcome, reason, credential_type, channel) "
                     "VALUES (?, 'INVALID', 'No ticket record matches this ticket ID', 'ticket_id', 'cli')",
                     (to_iso(utc_now()),))
    finally:
        conn.close()


def test_notify_conductor_acknowledges_latest_alert(open_dashboard, tmp_path):
    path = prepare("demo", tmp_path)
    at = run(open_dashboard(path))
    before = int(bell_label(at))
    notify = at.button(key="dash_notify")
    assert before > 0 and not notify.disabled
    notify.click()
    run(at)
    assert int(bell_label(at)) == before - 1
    assert at.button(key="dash_notify").disabled                # already acknowledged, nothing newer


def test_notify_toast_survives_rerun(open_dashboard, tmp_path):
    path = prepare("demo", tmp_path)
    at = run(open_dashboard(path))
    at.button(key="dash_notify").click()
    run(at)
    assert any("Alert acknowledged" in t.value for t in at.toast)


def test_ticketless_headline_only_for_failed_checks(open_dashboard, tmp_path):
    path = prepare("empty", tmp_path)
    conn = Database(path).connect()
    try:
        insert_session(conn, session_id="unv-session", source_type="webcam", source_label="webcam 0",
                       status="STOPPED", started_at=utc_now() - timedelta(hours=1), station="City Hospital",
                       trip_id="T")
        for track in (1, 2):
            insert_boarding(conn, session_id="unv-session", track_id=track,
                            boarded_at=utc_now() - timedelta(minutes=30), direction="top_to_bottom",
                            station="City Hospital", trip_id="T")
    finally:
        conn.close()
    at = run(open_dashboard(path))
    page = " ".join(raw_html(at))
    assert "No ticketless passengers" in page and "Ticketless Passenger Detected!" not in page
    assert at.button(key="dash_notify").disabled
    add_failed_check(path)
    at = run(open_dashboard(path))
    page = " ".join(raw_html(at))
    assert "Ticketless Passenger Detected!" in page and "No ticketless passengers" not in page
    assert not at.button(key="dash_notify").disabled


class _Collect(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def test_custom_range_persists_across_pages(open_dashboard, tmp_path):
    from datetime import date

    handler = _Collect()
    logging.getLogger("streamlit").addHandler(handler)
    try:
        path = prepare("demo", tmp_path)
        at = run(open_dashboard(path))
        next(s for s in at.selectbox if s.label == "Period").set_value("custom")
        run(at)
        at.date_input[0].set_value((date(2026, 1, 1), date(2026, 1, 3)))
        run(at)
        at.switch_page("pages/4_Reports.py")
        run(at)
        assert next(s for s in at.selectbox if s.label == "Period").value == "custom"
        assert any("2026-01-01 00:00" in m.value for m in at.markdown)
        at.switch_page("pages/1_Ticket_Check.py")
        run(at)
        assert next(s for s in at.selectbox if s.label == "Period").value == "today"   # its own check_period
        at.switch_page("pages/0_Dashboard.py")
        run(at)
        assert next(s for s in at.selectbox if s.label == "Period").value == "custom"
        assert tuple(at.date_input[0].value) == (date(2026, 1, 1), date(2026, 1, 3))
    finally:
        logging.getLogger("streamlit").removeHandler(handler)
    assert not [m for m in handler.messages if "default value" in m and "Session State" in m]


def test_reports_downloads(open_dashboard, tmp_path):
    path = prepare("demo", tmp_path)
    at = open_page(open_dashboard, path, "4_Reports.py", router=False)
    labels = [b.label for b in at.download_button]
    assert labels == ["Download summary CSV", "Download validations CSV", "Download boardings CSV"]
    source = (ROOT / "dashboard" / "pages" / "4_Reports.py").read_text(encoding="utf-8")
    calls = re.findall(r"st\.download_button\((.*?)\)\n", source, flags=re.S)
    assert calls and all('on_click="ignore"' in c for c in calls)
    assert any("PDF export: not available" in c.value for c in at.caption)


def test_validation_card_limit_and_search(open_dashboard, tmp_path, monkeypatch):
    path = prepare("demo", tmp_path)
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text("dashboard:\n  table_limit: 10\n  default_period: all\n", encoding="utf-8")
    monkeypatch.setenv("TPD_CONFIG", str(cfg_file))
    add_failed_check(path)                                   # 15 validations in "All time"
    at = run(open_dashboard(path))
    assert at.selectbox(key="dash_val_limit").value == 10
    assert len(validation_rows(at)) == 10
    assert any(c.value.startswith("Showing 10 of 15") for c in at.caption)
    at.selectbox(key="dash_val_limit").set_value(25)
    run(at)
    assert 10 < len(validation_rows(at)) <= 25
    at.text_input(key="dash_val_search").input("zzz-no-such-ticket")
    run(at)
    assert validation_rows(at) == []
    assert any("No results match" in b and "zzz-no-such-ticket" in b for b in raw_html(at))
    at.text_input(key="dash_val_search").input("demo")       # channel code match
    run(at)
    assert validation_rows(at)


def test_default_nav_hidden():
    source = (ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
    assert re.search(r'st\.navigation\(.*position="hidden"\)', source)
    assert "showSidebarNavigation = false" in (ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8")


def sidebar_nodes(at):
    return [n for n in at.sidebar if n is not at.sidebar]


def nav_html(at):
    return next(h.proto.body for h in at.sidebar.get("html") if "tpd-nav-item" in h.proto.body)


@pytest.mark.parametrize("page", PAGES)
def test_sidebar_nav_items_and_active_direct(open_dashboard, tmp_path, page):
    path = prepare("empty", tmp_path)
    at = open_page(open_dashboard, path, page, router=False)
    title = next(label for label, target, *_ in NAV_ITEMS if target == f"pages/{page}")
    items = re.findall(r'<div class="(tpd-nav-item[^"]*)" data-nav="[a-z]+"[^>]*>'
                       r'<img class="tpd-icon" src="data:image/svg\+xml;base64,[^"]+"[^>]*><span>([^<]+)</span></div>',
                       nav_html(at))
    assert [label for _css, label in items] == [label for label, *_ in NAV_ITEMS]
    assert [label for css, label in items if "tpd-nav-active" in css] == [title]
    assert not at.sidebar.get("page_link")                  # sibling pages are not resolvable in direct runs
    assert [e.label for e in at.sidebar.get("expander")] == ["Filters & data"]
    assert any(TAGLINE in h.proto.body for h in at.sidebar.get("html"))


def test_sidebar_nav_items_and_active_router(open_dashboard, tmp_path):
    path = prepare("empty", tmp_path)
    at = run(open_dashboard(path))
    for page, title in ((None, "Dashboard"), ("pages/4_Reports.py", "Reports"),
                        ("pages/1_Ticket_Check.py", "Ticket Checker")):
        if page:
            at.switch_page(page)
            run(at)
        links = [link for n in sidebar_nodes(at) if isinstance(n, Block) and (n.key or "").startswith("nav-")
                 for link in at.sidebar.get_by_key(n.key).get("page_link")]
        assert [link.proto.label for link in links] == [label for label, *_ in NAV_ITEMS]
        assert not any((n.key or "") == "sidebar-signout" for n in sidebar_nodes(at) if isinstance(n, Block))
        active = at.sidebar.get_by_key("nav-active")
        assert [link.proto.label for link in active.get("page_link")] == [title]
        keys = [n.key for n in sidebar_nodes(at) if isinstance(n, Block) and (n.key or "").startswith("nav-")]
        assert keys == ["nav-active" if label == title else f"nav-{slug}" for label, _p, _i, slug in NAV_ITEMS]
        assert [e.label for e in at.sidebar.get("expander")] == ["Filters & data"]


def test_ticket_checker_sidebar_above_footer(open_dashboard, tmp_path):
    path = prepare("empty", tmp_path)
    at = open_page(open_dashboard, path, "1_Ticket_Check.py", router=False)
    nodes = sidebar_nodes(at)
    slot = at.sidebar.get_by_key("sidebar-page")
    header = next(h for h in slot.get("header") if h.value == "Boardings")
    period = next(s for s in slot.get("selectbox") if s.key == "check_period")
    footer = next(i for i, n in enumerate(nodes) if n.type == "html" and TAGLINE in n.proto.body)
    expander = next(i for i, n in enumerate(nodes) if n.type == "expander")

    def pos(node):
        return next(i for i, n in enumerate(nodes) if n is node)

    assert expander < pos(slot) < pos(header) < pos(period) < footer


def test_settings_never_renders_credentials(open_dashboard, tmp_path, monkeypatch):
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text("camera:\n  phone:\n    url: http://user:pass@192.0.2.7:8080/video\n", encoding="utf-8")
    monkeypatch.setenv("TPD_CONFIG", str(cfg_file))
    path = prepare("empty", tmp_path)
    for page in ("5_Settings.py", "2_Live_Feed.py"):
        at = open_page(open_dashboard, path, page, router=False)
        texts = " ".join(all_text(at))
        assert "user:pass" not in texts and "pass@" not in texts
        assert "***@192.0.2.7:8080" in texts


def test_dashboard_writes_only_via_ticket_checker():
    for path in (ROOT / "dashboard").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "ultralytics" not in source and "VideoCapture" not in source, path
        if path.name not in ("1_Ticket_Check.py", "data.py", "metrics_panel.py", "notifications.py"):
            assert "write_conn(" not in source, path


def test_tables_constant_is_complete():
    assert "validation_events" in TABLES
