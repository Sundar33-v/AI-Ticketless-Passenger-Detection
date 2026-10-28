"""Tests for dashboard/components/notification_cards.py (pure HTML builders of the bell and panel)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.alert_service import AlertGroup
from app.database import to_iso
from dashboard.components import notification_cards as nc
from dashboard.components import tokens

TZ = ZoneInfo("Asia/Kolkata")
T0 = datetime(2026, 1, 15, 5, 30, 12, tzinfo=timezone.utc)  # 11:00:12 IST
WHITE = "#FFFFFF"


def group(**over):
    values = dict(key="exp:12:t:TKT-1", source="validation", match=("EXPIRED", "other", 12, "t:TKT-1"),
                  kind="expired_ticket", severity="warning", title="Expired ticket",
                  message="Ticket verification failed for Track ID 3: Expired – Ticket expired at 10:00",
                  first_at=T0 - timedelta(minutes=5), last_at=T0, count=3, track_id=3, ticket_ref="TKT-1",
                  outcome="EXPIRED", detail="Expired – Ticket expired at 10:00")
    values.update(over)
    return AlertGroup(**values)


def test_view_fields():
    view = nc.view_of(group(), {}, TZ)
    assert view.title == "Expired ticket" and view.detail == "Expired – Ticket expired at 10:00"
    assert view.type_label == "Ticket check" and view.state == "Unread" and view.severity == "warning"
    assert view.time_text == "11:00:12 · 15 Jan" and view.time_iso == "2026-01-15T05:30:12+00:00"
    assert (view.track, view.ticket, view.count) == ("Track ID 003", "Ticket TKT-1", "×3 events")


def test_view_optional_fields_and_message_fallback():
    view = nc.view_of(group(track_id=None, ticket_ref=None, count=1, detail=""), {}, TZ)
    assert view.track is view.ticket is view.count is None
    assert view.detail.startswith("Ticket verification failed")


def test_state_pills():
    g = group()
    assert nc.view_of(g, {g.key: to_iso(g.last_at)}, TZ).state == "Read"
    assert nc.view_of(g, {g.key: to_iso(g.last_at - timedelta(seconds=1))}, TZ).state == "Unread"
    info = group(key="stop:s1", source="session", kind="detection_stopped", severity="info", title="Detection stopped")
    assert nc.view_of(info, {}, TZ).state == "Info"
    for kind, label in (("invalid_ticket", "Ticket check"), ("unrecognised_qr", "QR scan"),
                        ("unverified_passenger", "Boarding"), ("camera_disconnected", "Detection"),
                        ("tracking_failure", "Detection"), ("detection_stopped", "Detection")):
        assert nc.view_of(group(kind=kind), {}, TZ).type_label == label


def test_item_html_markup_and_states():
    body = nc.item_html(nc.view_of(group(), {}, TZ))
    assert body.startswith('<article class="tpd-note tpd-note--warning tpd-note--unread" data-state="unread" '
                           'aria-label="Expired ticket, Unread, 11:00:12 · 15 Jan">')
    for text in ('<span class="tpd-note-title">Expired ticket</span>', 'tpd-note-pill--state">Unread<',
                 'tpd-note-pill--type">Ticket check<', '<p class="tpd-note-msg">Expired – Ticket expired at 10:00</p>',
                 '<time datetime="2026-01-15T05:30:12+00:00">11:00:12 · 15 Jan</time>', "<span>Track ID 003</span>",
                 "<span>Ticket TKT-1</span>", "<span>×3 events</span>"):
        assert text in body, text
    g = group()
    read = nc.item_html(nc.view_of(g, {g.key: to_iso(g.last_at)}, TZ))
    assert "tpd-note--read" in read and 'data-state="read"' in read
    info = nc.item_html(nc.view_of(group(severity="info"), {}, TZ))
    assert "tpd-note--info" in info and ">Info<" in info


def test_item_html_escapes_everything():
    evil = "<script>alert(1)</script>"
    view = nc.view_of(group(title=evil, detail=f"http://x/?a=1&b={evil}", ticket_ref='"><img src=x>'), {}, TZ)
    body = nc.item_html(view)
    assert "<script>" not in body and "<img" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body and "a=1&amp;b=" in body
    assert "<a " not in body                                       # URLs are never linked


def test_bell_head_html():
    assert nc.bell_head_html(2, 7) == ('<div class="tpd-bell-head"><h2 id="tpd-bell-heading" tabindex="-1">'
                                       'Notifications</h2><p role="status">2 unread · 7 in this period</p></div>')
    assert "1000+ in this period" in nc.bell_head_html(5, 3000, truncated=True)


@pytest.mark.parametrize("state, expected", [
    (7, '<span class="tpd-bell-badge-wrap" data-count="7"><span class="tpd-bell-badge" aria-hidden="true">7</span>'
        '<span class="tpd-sr">7 unread notifications</span></span>'),
    (1, '<span class="tpd-bell-badge-wrap" data-count="1"><span class="tpd-bell-badge" aria-hidden="true">1</span>'
        '<span class="tpd-sr">1 unread notification</span></span>'),
    (99, '<span class="tpd-bell-badge-wrap" data-count="99"><span class="tpd-bell-badge" aria-hidden="true">99'
         '</span><span class="tpd-sr">99 unread notifications</span></span>'),
    (100, '<span class="tpd-bell-badge-wrap" data-count="99+"><span class="tpd-bell-badge" aria-hidden="true">99+'
          '</span><span class="tpd-sr">More than 99 unread notifications</span></span>'),
    (0, '<span class="tpd-bell-badge-wrap" data-count="0"><span class="tpd-bell-badge" aria-hidden="true" hidden>0'
        '</span><span class="tpd-sr">No unread notifications</span></span>'),
    (None, '<span class="tpd-bell-badge-wrap" data-count="–"><span class="tpd-bell-badge tpd-bell-badge--error" '
           'aria-hidden="true">–</span><span class="tpd-sr">Notifications unavailable</span></span>'),
    ("loading", '<span class="tpd-bell-badge-wrap" data-count="…"><span class="tpd-bell-badge '
                'tpd-bell-badge--loading" aria-hidden="true">…</span><span class="tpd-sr">Loading notifications'
                '</span></span>'),
])
def test_badge_states(state, expected):
    assert nc.badge_html(state) == expected


@pytest.mark.parametrize("bad", [-1, True, "7", 1.5, [], "busy"])
def test_badge_rejects_other_values(bad):
    with pytest.raises(ValueError):
        nc.badge_html(bad)


def test_state_html():
    error = nc.state_html("error", "Notifications unavailable – <db>")
    assert error == ('<div class="tpd-bell-state tpd-bell-state--error" role="alert" data-state="error">'
                     '<p id="tpd-bell-heading" tabindex="-1">Notifications unavailable – &lt;db&gt;</p></div>')
    for kind in ("loading", "empty"):
        body = nc.state_html(kind, "x")
        assert 'role="status"' in body and 'id="tpd-bell-heading" tabindex="-1"' in body
    notice = nc.state_html("warning", "Database busy")
    assert 'role="status"' in notice and "tpd-bell-heading" not in notice
    with pytest.raises(ValueError):
        nc.state_html("success", "x")


def test_focus_script_is_the_pinned_constant():
    assert nc.FOCUS_SCRIPT == (
        "<script>requestAnimationFrame(function(){var p=document.querySelector("
        "'[data-testid=\"stPopoverBody\"]:has(.st-key-bell-panel)');\n"
        "var h=document.getElementById('tpd-bell-heading');if(p&&h&&!p.contains(document.activeElement))"
        "{h.focus({preventScroll:true});}});</script>")


def test_panel_text_contrast():
    for fg in (tokens.TEXT, tokens.MUTED, tokens.PRIMARY_TEXT):
        for bg in (tokens.CARD, tokens.CARD_ALT):
            assert tokens.contrast_ratio(fg, bg) >= 4.5, (fg, bg)
    assert tokens.contrast_ratio(WHITE, tokens.ERROR_STRONG) >= 4.5
