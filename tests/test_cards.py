"""dashboard/components/cards.py (pure HTML builders) and status.placeholder_message."""

from __future__ import annotations

import dataclasses
import html
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.alert_service import AlertGroup
from app.analytics import METRIC_HELP, TOTAL_PASSENGER_CAPTION, DashboardMetrics
from app.settings_schema import AppConfig, JourneyConfig, RouteConfig
from app.system_status import (CAMERA_DISCONNECTED, FAILED, NO_SESSION, NOT_RESPONDING, ONLINE, STOPPED,
                               DetectionHealth)
from dashboard.components import cards, status, theme, tokens
from dashboard.components.tables import VALIDATION_COLUMNS

IST = ZoneInfo("Asia/Kolkata")
EVIL = '<script>alert("x")</script>'
AT = datetime(2026, 1, 15, 4, 37, 5, tzinfo=timezone.utc)  # 10:07:05 IST


def group(kind="invalid_ticket", track_id=3, key="k", severity="error"):
    return AlertGroup(key, "validation", (), kind, severity, "Invalid ticket", "msg", AT, AT, 1, track_id=track_id)


def health(state=NO_SESSION, feed="off", label="Detection: Not started", online=False, tone="muted"):
    return DetectionHealth(state, online, label, tone, "detail", None, None, None, feed, None, None, None, None)


def kpis(html_text):
    return dict(re.findall(r'data-kpi="(\w+)" data-value="(\d+)"', html_text))


def escaped(html_text):
    return "<script>" not in html_text and "&lt;script&gt;" in html_text


# ------------------------------------------------------------------ wording drift guards
def test_phrases_match_theme_terms():
    assert cards.BRAND_TITLE == theme.BRAND_TITLE
    for phrase in (cards.ALERT_HEADLINE, cards.NO_ALERT_HEADLINE, cards.KPI_SPECS[1].caption):
        assert phrase in theme.TICKETLESS_TERMS
    assert "for ticketless travel" in cards.BANNER_TEXT


# ------------------------------------------------------------------ KPI cards
def test_kpi_specs_order_and_help_keys():
    assert [s.slug for s in cards.KPI_SPECS] == ["valid", "invalid", "passenger", "detection", "expired"]
    assert [s.field for s in cards.KPI_SPECS] == ["valid", "invalid", "total_passenger", "tracking_ids",
                                                  "expired_tickets"]
    assert all(s.help_key in METRIC_HELP for s in cards.KPI_SPECS)
    fields = {f.name for f in dataclasses.fields(DashboardMetrics)}
    assert all(s.field in fields for s in cards.KPI_SPECS)


@pytest.mark.parametrize("cur, prev, expected", [
    (5, None, ("—", "flat")), (0, 0, ("0%", "flat")), (3, 0, ("New", "up")), (12, 10, ("20%", "up")),
    (5, 10, ("50%", "down")), (10, 10, ("0%", "flat")), (1, 3, ("67%", "down")),
])
def test_delta_text(cur, prev, expected):
    assert cards.delta_text(cur, prev) == expected


def test_delta_class_inverted_for_failures():
    assert cards.delta_class("valid", "up") == "good" and cards.delta_class("valid", "down") == "bad"
    assert cards.delta_class("passenger", "up") == "good"
    assert cards.delta_class("invalid", "up") == "bad" and cards.delta_class("invalid", "down") == "good"
    assert cards.delta_class("expired", "up") == "bad" and cards.delta_class("expired", "down") == "good"
    assert cards.delta_class("invalid", "flat") == "flat"


def test_kpi_cards_html_values_captions_and_deltas():
    current = DashboardMetrics(valid=7, invalid=4, total_passenger=16, total_detection=99, expired_tickets=2,
                               boarding_events=5, tracking_ids=18)
    previous = DashboardMetrics(valid=10, invalid=2, total_passenger=16, tracking_ids=9, expired_tickets=4)
    out = cards.kpi_cards_html(current, previous, "Last 7 days")
    assert out.count('class="tpd-kpi tpd-kpi--') == 5
    assert kpis(out) == {"valid": "7", "invalid": "4", "passenger": "16", "detection": "18", "expired": "2"}
    assert "Passengers · Last 7 days" in out and "Ticketless / Invalid QR" in out
    assert "tpd-delta--bad" in card(out, "invalid") and "▲" in card(out, "invalid")   # more failures: bad
    assert "tpd-delta--bad" in card(out, "valid") and "▼" in card(out, "valid")
    assert "tpd-delta--good" in card(out, "expired") and "▼" in card(out, "expired")  # fewer expired: good
    assert "tpd-delta--good" in card(out, "detection") and "100%" in card(out, "detection")
    assert "tpd-delta--flat" in card(out, "passenger") and "0%" in card(out, "passenger")
    assert html.escape(TOTAL_PASSENGER_CAPTION, quote=True) in card(out, "passenger")
    assert html.escape(METRIC_HELP["Tracking IDs"], quote=True) in card(out, "detection")
    assert 'title="vs previous period"' in out


def card(out, slug):
    start = out.index(f'data-kpi="{slug}"')
    end = out.find("data-kpi=", start + 1)
    return out[start:end if end != -1 else len(out)]


def test_kpi_cards_html_empty_and_all_time():
    out = cards.kpi_cards_html(DashboardMetrics(), None, "All time")
    assert set(kpis(out).values()) == {"0"} and "—" in out and "tpd-delta--flat" in out
    zero = cards.kpi_cards_html(DashboardMetrics(), DashboardMetrics(), "Today")
    assert zero.count("0%") == 5 and "Passengers · Today" in zero


def test_kpi_cards_escape_period_label():
    assert escaped(cards.kpi_cards_html(DashboardMetrics(), None, EVIL))


# ------------------------------------------------------------------ route, banner, top bar, footer
def test_route_summary_directions_and_fallbacks():
    cfg = AppConfig()
    assert cards.route_summary(cfg) == "R1 | Central Bus Stand → Airport"
    inbound = dataclasses.replace(cfg, journey=JourneyConfig(direction="inbound"))
    assert cards.route_summary(inbound) == "R1 | Airport → Central Bus Stand"
    one = dataclasses.replace(cfg, route=RouteConfig(route_id="R9", name="Solo", stations=("Only Stop",)))
    assert cards.route_summary(one) == "R9 | Only Stop"
    none = dataclasses.replace(cfg, route=RouteConfig(route_id="", name="x", stations=("A", "B")))
    assert cards.route_summary(none) == "No route configured"


def test_banner_pill_follows_online_and_tone():
    online = cards.banner_html(True, "Detection: Online", "success", "R1 | A → B")
    assert "System Online" in online and 'title="Detection: Online"' in online and 'role="status"' in online
    assert f"background:{tokens.SUCCESS}" in online and "R1 | A → B" in online
    assert "Welcome, Conductor!" in online and "for ticketless travel" in online
    stopped = cards.banner_html(False, "Detection: Failed", "error", "R1")
    assert "System Online" not in stopped and "Detection: Failed" in stopped
    assert f"background:{tokens.ERROR}" in stopped
    assert f"background:{tokens.MUTED}" in cards.banner_html(False, "Detection: ?", "bogus", "R1")
    assert escaped(cards.banner_html(False, EVIL, "muted", EVIL))


def test_topbar_html():
    left = cards.topbar_left_html()
    assert theme.BRAND_TITLE in left and "Conductor Dashboard • Safe Travel • Smart Monitoring" in left
    right = cards.topbar_right_html("Mon, 20 Oct 2026", "10:58 AM", True, "Detection: Online")
    assert "Mon, 20 Oct 2026" in right and "10:58 AM" in right and "Online" in right
    assert right.count("Conductor console: Online · Detection: Online") == 2   # title + visually hidden text
    off = cards.topbar_right_html("Mon", "1:00 PM", False, "Detection: Unknown")
    assert "Offline" in off and off.count(cards.OFFLINE_TIP) == 2 and "tpd-status-dot--offline" in off
    assert escaped(cards.topbar_right_html(EVIL, EVIL, True, EVIL))


def test_sidebar_footer_html():
    out = cards.sidebar_footer_html()
    assert '<em class="tpd-tagline">Smart Detection for Safer Journeys</em>' in out
    assert '<img class="tpd-icon" src="data:image/svg+xml;base64,' in out


# ------------------------------------------------------------------ donut legend
def distribution(**counts):
    return pd.DataFrame([(o, o.title(), n, 0.0) for o, n in counts.items()],
                        columns=["outcome", "label", "count", "percent"])


def test_donut_counts_groups_and_percentages():
    slices = cards.donut_counts(distribution(VALID=12, INVALID=2, WRONG_ROUTE=1, ALREADY_USED=1, EXPIRED=1,
                                             VERIFICATION_REQUIRED=1))
    assert [(s.label, s.count, s.percent) for s in slices] == [("Valid", 12, 67), ("Invalid", 4, 22),
                                                               ("Expired", 1, 6), ("Unverified", 1, 6)]
    assert [s.colour for s in slices] == [tokens.SUCCESS, tokens.ERROR, tokens.WARNING, tokens.PURPLE]


def test_donut_counts_zero_total():
    empty = pd.DataFrame(columns=["outcome", "label", "count", "percent"])
    assert [(s.count, s.percent) for s in cards.donut_counts(empty)] == [(0, 0)] * 4


def test_legend_html_rows_and_verification_wording():
    out = cards.legend_html(cards.donut_counts(distribution(VALID=12, EXPIRED=6)))
    assert re.search(r"<span>Valid</span>.*?12 \(67%\)", out)
    assert "0 (0%)" in out and out.count("tpd-legend-row") == 4
    assert 'title="Ticket verification required"' in out and "Ticket verification required" in out
    assert escaped(cards.legend_html([cards.DonutSlice(EVIL, 1, 100, EVIL)]))


# ------------------------------------------------------------------ live feed card
def test_latest_failed_check_skips_other_kinds():
    groups = [group("expired_ticket", key="e"), group("unverified_passenger", key="u"),
              group("detection_stopped", key="s"), group("unrecognised_qr", key="q"), group(key="i")]
    assert cards.latest_failed_check(groups).key == "q"
    assert cards.latest_failed_check(groups[:3]) is None
    assert cards.latest_failed_check([]) is None


def test_track_label():
    assert cards.track_label(3) == "003" and cards.track_label(1234) == "1234"
    assert cards.track_label(None) == "—" and cards.track_label(float("nan")) == "—"


def test_alert_panel_html():
    out = cards.alert_panel_html(group(track_id=3), IST, "R1")
    assert "Ticketless Passenger Detected!" in out and "ID: 003" in out
    assert "Time: 10:07:05 AM" in out and "Route: R1" in out
    desk = cards.alert_panel_html(group(track_id=None), IST, "")
    assert "ID: —" in desk and "Route: —" in desk
    ok = cards.alert_panel_html(None, IST, "R1")
    assert "No ticketless passengers" in ok and "No failed ticket checks in this period." in ok
    assert "Ticketless Passenger Detected!" not in ok and "tpd-alert--ok" in ok
    assert escaped(cards.alert_panel_html(group(), IST, EVIL))


def test_feed_placeholder_html():
    out = cards.feed_placeholder_html(status.COMPACT_OFF)
    assert status.COMPACT_OFF in out and "tpd-feed-placeholder" in out
    assert '<img class="tpd-icon" src="data:image/svg+xml;base64,' in out and "<svg" not in out
    assert escaped(cards.feed_placeholder_html(EVIL))


@pytest.mark.parametrize("h, expected", [
    (health(), status.COMPACT_OFF),
    (health(STOPPED, label="Detection: Stopped"), status.COMPACT_OFF),
    (health(FAILED, label="Detection: Failed"), "No live preview – Detection: Failed."),
    (health(CAMERA_DISCONNECTED, label="Camera: Disconnected"), "No live preview – Camera: Disconnected."),
    (health(NOT_RESPONDING, label="Detection: Not responding (last heartbeat 2 min ago)"),
     "No live preview – Detection: Not responding (last heartbeat 2 min ago)."),
    (health(ONLINE, feed="waiting", online=True), "Waiting for the first preview frame…"),
    (health(ONLINE, feed="live", online=True), "Waiting for the first preview frame…"),
    (health(ONLINE, feed="disabled", online=True), "Live preview is disabled (runtime.preview_interval_sec = 0)."),
])
def test_placeholder_message_per_state(h, expected):
    assert status.placeholder_message(h) == expected


def test_compact_off_text():
    assert status.COMPACT_OFF == "No live preview – detection is not running."


# ------------------------------------------------------------------ validation table
def frame(rows):
    return pd.DataFrame(rows, columns=VALIDATION_COLUMNS)


def row(outcome_label="Valid", reason="ok", time="2026-01-15 10:07:05"):
    return [time, outcome_label, reason, "TKT-1", "3", "Camera", "No"]


@pytest.mark.parametrize("code, pill", [("VALID", "valid"), ("INVALID", "invalid"), ("WRONG_ROUTE", "invalid"),
                                        ("ALREADY_USED", "invalid"), ("EXPIRED", "expired"),
                                        ("VERIFICATION_REQUIRED", "unverified"), ("SOMETHING_NEW", "unverified")])
def test_validation_table_pill_classes(code, pill):
    out = cards.validation_table_html(frame([row("Label")]), [code], empty_text="none")
    assert f'<span class="tpd-pill tpd-pill--{pill}">Label</span>' in out
    assert f'data-outcome="{code}"' in out


def test_validation_table_cells_header_and_rows():
    out = cards.validation_table_html(frame([row(), row("Expired")]), ["VALID", "EXPIRED"], empty_text="none")
    assert '<td title="2026-01-15 10:07:05">10:07:05</td>' in out
    assert '<th scope="col">Ticket</th>' in out and "Ticket ID" not in out
    assert out.count("<tr data-outcome=") == 2 and 'role="region"' in out and 'tabindex="0"' in out
    assert 'class="tpd-reason" title="ok"' in out


def test_validation_table_empty_text_and_nan():
    out = cards.validation_table_html(frame([]), [], empty_text="No results match “x”.")
    assert '<td colspan="7" class="tpd-empty">No results match “x”.</td>' in out
    nan = cards.validation_table_html(frame([[None, "Valid", float("nan"), "", "", "", "No"]]), ["VALID"],
                                      empty_text="")
    assert ">nan<" not in nan and 'title="nan"' not in nan and '<td title=""></td>' in nan


def test_validation_table_length_mismatch_raises():
    with pytest.raises(ValueError):
        cards.validation_table_html(frame([row()]), [], empty_text="")


def test_validation_table_escapes_everything():
    out = cards.validation_table_html(frame([[EVIL, EVIL, EVIL, EVIL, EVIL, EVIL, EVIL]]), [EVIL],
                                      empty_text=EVIL)
    assert escaped(out) and 'data-outcome="&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;"' in out
    assert escaped(cards.validation_table_html(frame([]), [], empty_text=EVIL))



def test_topbar_shows_conductor_without_user():
    on = cards.topbar_right_html("Mon", "1:00 PM", True, "Detection: Online")
    assert '<span class="tpd-avatar" aria-hidden="true"><img class="tpd-icon"' in on
    assert ">Conductor</div>" in on and "Online</div>" in on
    off = cards.topbar_right_html("Mon", "1:00 PM", False, "Detection: Unknown")
    assert "Offline</div>" in off and off.count(cards.OFFLINE_TIP) == 2 and " · Offline" not in off
