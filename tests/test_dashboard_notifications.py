"""AppTests of the header notification bell and panel (stored alerts only, persistent read state)."""

from __future__ import annotations

import html
import re
import sqlite3
from datetime import timedelta

import pytest
from streamlit.testing.v1 import AppTest

import app.alert_reads as alert_reads
from app.alert_service import alert_groups, unread_count
from app.analytics import period_for
from app.config import load_config
from app.database import Database, utc_now
from app.passenger_service import insert_boarding, insert_session
from dashboard.components import notification_cards as nc
from dashboard.components import notifications
from dashboard.components.status import NOTIFY_TOAST
from test_dashboard import bell_label, row_counts, run
from test_dashboard_pages import open_page, prepare


# ------------------------------------------------------------------ helpers
def panel(at):
    return at.get_by_key("bell-panel")


def panel_html(at):
    return [h.proto.body for h in panel(at).get("html")]


def note_titles(at):
    body = " ".join(panel_html(at))
    return [html.unescape(t) for t in re.findall(r'<span class="tpd-note-title">([^<]*)</span>', body)]


def note_states(at):
    return re.findall(r'<article class="tpd-note [^"]*" data-state="([a-z]+)"', " ".join(panel_html(at)))


def item_buttons(at):
    return [b for b in at.button if (b.key or "").startswith("bell_read_") and b.key != "bell_read_all"]


def read_rows(path):
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT alert_key, last_at_seen FROM alert_reads ORDER BY alert_key").fetchall()
    finally:
        conn.close()


def stored_groups(path):
    cfg = load_config(None)
    now = utc_now()
    conn = Database(path).read_connection()
    try:
        return alert_groups(conn, period_for(cfg.dashboard.default_period, cfg.app.timezone, now), now, cfg).groups
    finally:
        conn.close()


def unverified_boardings(path, n):
    """``n`` boardings 30 min ago that were never checked (one warning group each) + a STOPPED session."""
    conn = Database(path).connect()
    try:
        insert_session(conn, session_id="bell-session", source_type="webcam", source_label="webcam 0",
                       status="STOPPED", started_at=utc_now() - timedelta(minutes=40), station="City Hospital",
                       trip_id="T")
        for track in range(1, n + 1):
            insert_boarding(conn, session_id="bell-session", track_id=track,
                            boarded_at=utc_now() - timedelta(minutes=30, seconds=track), direction="top_to_bottom",
                            station="City Hospital", trip_id="T")
    finally:
        conn.close()


def assert_skeleton(at):
    assert panel(at) is not None
    assert nc.FOCUS_SCRIPT in panel_html(at)
    assert at.get("popover")[0].proto.popover.label == notifications.BELL_LABEL


# ------------------------------------------------------------------ badge and list
def test_badge_equals_unread_count_and_panel_lists_stored_groups(open_dashboard, tmp_path):
    path = prepare("demo", tmp_path)
    at = run(open_dashboard(path))
    groups = stored_groups(path)
    expected = unread_count(groups, {})
    assert expected > 0 and bell_label(at) == str(expected)
    assert sum(s == "unread" for s in note_states(at)) == min(expected, notifications.BELL_LIMIT)
    titles = note_titles(at)
    assert titles and len(titles) == min(len(groups), notifications.BELL_LIMIT)
    assert set(titles) <= {g.title for g in groups}                        # only stored alert groups
    assert all(g.outcome != "VALID" for g in groups)                       # successful checks are not alerts
    head = " ".join(panel_html(at))
    assert f"{expected} unread · {len(groups)} in this period" in head
    assert_skeleton(at)


def test_empty_db_shows_empty_state(open_dashboard, tmp_path):
    path = prepare("empty", tmp_path)
    at = run(open_dashboard(path))
    assert bell_label(at) == "0"
    body = " ".join(panel_html(at))
    assert 'data-state="empty"' in body and html.escape(notifications.EMPTY_TEXT) in body
    assert note_titles(at) == [] and not item_buttons(at)
    assert not [b for b in at.button if b.key == "bell_read_all" and not b.disabled]   # nothing to mark
    assert_skeleton(at)


def test_info_only_groups_disable_mark_all(open_dashboard, tmp_path):
    path = prepare("empty", tmp_path)
    unverified_boardings(path, 0)                                          # only a STOPPED session (info)
    at = run(open_dashboard(path))
    assert bell_label(at) == "0"
    assert note_states(at) == ["info"] and not item_buttons(at)
    assert at.button(key="bell_read_all").disabled


def test_unreadable_db_shows_error_state(open_dashboard, tmp_path, monkeypatch):
    import dashboard.components.shell as shell

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    path = prepare("empty", tmp_path)
    monkeypatch.setattr(shell, "alert_groups", broken)
    at = open_dashboard(path)
    at.run()
    assert not at.exception
    assert bell_label(at) == "–"
    body = " ".join(panel_html(at))
    assert 'role="alert"' in body and html.escape(notifications.ERROR_TEXT) in body
    assert_skeleton(at)


def _loading_script():
    from zoneinfo import ZoneInfo

    from dashboard.components import notifications as bell

    bell.render_bell(bell.open_bell(), None, "loading", ZoneInfo("Asia/Kolkata"))


def test_loading_state_has_the_skeleton():
    at = AppTest.from_function(_loading_script, default_timeout=30)
    run(at)
    assert bell_label(at) == "…"
    body = " ".join(panel_html(at))
    assert 'data-state="loading"' in body and notifications.LOADING_TEXT in body
    assert_skeleton(at)


# ------------------------------------------------------------------ toggle
def test_bell_toggle_is_client_side_and_survives_reruns(open_dashboard, tmp_path):
    """The bell is a stateless native popover: opening/closing it is handled in the browser (no rerun,
    no widget state), and the popover + panel skeleton stay in place across reruns so an open panel
    stays open after Mark as read."""
    path = prepare("demo", tmp_path)
    at = run(open_dashboard(path))
    popovers = at.get("popover")
    assert len(popovers) == 1
    bell = popovers[0].proto.popover
    assert bell.label == notifications.BELL_LABEL and not bell.id and not bell.disabled
    assert "tpd_bell" not in at.session_state
    assert all(b.key != "tpd_bell" for b in at.button)
    item_buttons(at)[0].click()                                            # an interaction inside the panel
    run(at)
    after = at.get("popover")
    assert len(after) == 1 and after[0].proto.popover.label == notifications.BELL_LABEL
    assert not after[0].proto.popover.id
    assert_skeleton(at)


# ------------------------------------------------------------------ mark as read
def test_per_item_mark_as_read_writes_one_row(open_dashboard, tmp_path):
    path = prepare("demo", tmp_path)
    at = run(open_dashboard(path))
    before_counts = row_counts(path)
    before = int(bell_label(at))
    item_buttons(at)[0].click()
    run(at)
    assert int(bell_label(at)) == before - 1                               # same run
    assert len(read_rows(path)) == 1
    assert row_counts(path) == before_counts                               # no operational row changed
    assert not any(NOTIFY_TOAST in t.value for t in at.toast)              # dash_notify not triggered
    assert len(item_buttons(at)) == min(before - 1, notifications.BELL_LIMIT)


def test_mark_all_clears_more_than_the_shown_items(open_dashboard, tmp_path):
    path = prepare("empty", tmp_path)
    unverified_boardings(path, 20)
    at = run(open_dashboard(path))
    assert bell_label(at) == "20"
    assert len(note_titles(at)) == notifications.BELL_LIMIT
    assert any(c.value == "Showing 15 of 21" for c in panel(at).get("caption"))
    before_counts = row_counts(path)
    at.button(key="bell_read_all").click()
    run(at)
    assert bell_label(at) == "0" and not item_buttons(at)
    assert len(read_rows(path)) == 20 and row_counts(path) == before_counts
    assert at.button(key="bell_read_all").disabled

    fresh = run(open_dashboard(path))                                      # a new browser session
    assert bell_label(fresh) == "0"
    assert note_states(fresh) == ["read"] * notifications.BELL_LIMIT      # the older info group is not shown


def test_read_state_survives_a_new_session(open_dashboard, tmp_path):
    path = prepare("demo", tmp_path)
    at = run(open_dashboard(path))
    before = int(bell_label(at))
    item_buttons(at)[0].click()
    run(at)
    fresh = run(open_dashboard(path))
    assert int(bell_label(fresh)) == before - 1
    assert "read" in note_states(fresh)


def test_locked_db_keeps_session_read_and_shows_notice(open_dashboard, tmp_path, monkeypatch):
    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    path = prepare("demo", tmp_path)
    monkeypatch.setattr(alert_reads, "mark_read", locked)
    at = run(open_dashboard(path))
    before = int(bell_label(at))
    item_buttons(at)[0].click()
    run(at)
    assert int(bell_label(at)) == before - 1                               # session-only read
    assert html.escape(notifications.BUSY_MESSAGE) in " ".join(panel_html(at))
    assert read_rows(path) == []


def _garbage_script():
    import streamlit as st

    from dashboard.components import notifications as bell

    st.session_state[bell.READ_KEY] = {"kept": "2026-01-15T05:00:00.000000Z"}
    st.session_state["result"] = bell.mark_read([("k", "garbage"), ("", "2026-01-15T05:00:00.000000Z")])


def test_invalid_items_change_nothing_and_open_no_connection(monkeypatch):
    from dashboard.components import data

    def no_connection():
        raise AssertionError("write_conn opened")

    monkeypatch.setattr(data, "write_conn", no_connection)
    at = AppTest.from_function(_garbage_script, default_timeout=30)
    run(at)
    assert at.session_state["result"] is None
    assert at.session_state[notifications.READ_KEY] == {"kept": "2026-01-15T05:00:00.000000Z"}


# ------------------------------------------------------------------ other callers persist
def test_notify_conductor_persists(open_dashboard, tmp_path):
    path = prepare("demo", tmp_path)
    at = run(open_dashboard(path))
    before = int(bell_label(at))
    at.button(key="dash_notify").click()
    run(at)
    assert len(read_rows(path)) == 1
    assert any(NOTIFY_TOAST in t.value for t in at.toast)
    fresh = run(open_dashboard(path))
    assert int(bell_label(fresh)) == before - 1
    assert fresh.button(key="dash_notify").disabled


def test_live_feed_mark_all_persists(open_dashboard, tmp_path):
    path = prepare("demo", tmp_path)
    at = open_page(open_dashboard, path, "2_Live_Feed.py", router=True)
    assert int(bell_label(at)) > 0
    at.button(key="live_alerts_read").click()
    run(at)
    assert bell_label(at) == "0"
    assert len(read_rows(path)) == len(stored_groups(path))
    fresh = open_page(open_dashboard, path, "2_Live_Feed.py", router=True)
    assert bell_label(fresh) == "0"


@pytest.mark.parametrize("router", [False, True], ids=["direct", "router"])
def test_footer_link_or_caption(open_dashboard, tmp_path, router):
    path = prepare("demo", tmp_path)
    at = open_page(open_dashboard, path, "0_Dashboard.py", router=router)
    links = [p.proto.label for p in panel(at).get("page_link")]
    captions = [c.value for c in panel(at).get("caption")]
    if router:
        assert links == ["Open Live Feed for all alerts"]
    else:
        assert not links and notifications.LIVE_CAPTION in captions
