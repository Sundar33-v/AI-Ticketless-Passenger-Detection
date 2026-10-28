"""Pure HTML builders for the mockup Dashboard (no Streamlit calls, no DB access).

Every interpolated string passes through ``html.escape`` and every number
through ``int()``; this module owns HTML building for DB/config data. Leaf
module: imports only ``tokens``, ``icons`` and ``app.*`` (never ``shell``,
``actions`` or ``theme``). The "ticketless" phrases used here are exactly the
ones listed in ``theme.TICKETLESS_TERMS`` (guarded by tests/test_cards.py).
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Iterable, NamedTuple, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

from app.alert_service import AlertGroup
from app.analytics import METRIC_HELP, DashboardMetrics
from app.settings_schema import AppConfig
from dashboard.components import icons
from dashboard.components.tokens import (ERROR, MUTED, PRIMARY, PRIMARY_TEXT, PURPLE, SUCCESS, TONE_COLORS, WARNING,
                                         tint)

BRAND_TITLE = "AI Ticketless Passenger Detection"  # same text as theme.BRAND_TITLE
BRAND_SUBTITLE = "Conductor Dashboard • Safe Travel • Smart Monitoring"
BANNER_TITLE = "Welcome, Conductor!"
BANNER_TEXT = "Monitor passenger activity and get instant alerts for ticketless travel."
ALERT_HEADLINE = "Ticketless Passenger Detected!"
NO_ALERT_HEADLINE = "No ticketless passengers"
NO_ALERT_TEXT = "No failed ticket checks in this period."
TAGLINE = "Smart Detection for Safer Journeys"
OFFLINE_TIP = "Conductor console: Offline – database unavailable · Detection: Unknown"
DELTA_TIP = "vs previous period"
VERIFICATION_WORDING = "Ticket verification required"
FAILED_CHECK_KINDS = ("invalid_ticket", "unrecognised_qr")
PERIOD_TOKEN = "{period}"
WHITE = "#FFFFFF"
ICON_MUTED = "#CBD5E1"


def title_html(text: str, svg: str = "", colour: str = PRIMARY, *, live: bool = False) -> str:
    """Card heading: an icon (or the red live dot) followed by the escaped text."""
    mark = ('<span class="tpd-live-dot" aria-hidden="true"></span>' if live
            else icons.icon(svg, colour, 20) if svg else "")
    return f'<div class="tpd-card-title">{mark}<span>{_e(text)}</span></div>'


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


# ------------------------------------------------------------------ KPI row
class KpiSpec(NamedTuple):
    slug: str
    label: str
    field: str        # DashboardMetrics attribute
    icon: str         # inline SVG
    colour: str
    caption: str      # PERIOD_TOKEN is replaced by the period label
    help_key: str     # key of app.analytics.METRIC_HELP (card tooltip)


KPI_SPECS: Tuple[KpiSpec, ...] = (
    KpiSpec("valid", "Valid", "valid", icons.CHECK_CIRCLE, SUCCESS, "Tickets verified successfully",
            "Valid Tickets"),
    KpiSpec("invalid", "Invalid", "invalid", icons.X_CIRCLE, ERROR, "Ticketless / Invalid QR", "Invalid Tickets"),
    KpiSpec("passenger", "Total Passenger", "total_passenger", icons.PEOPLE, PRIMARY,
            "Passengers · " + PERIOD_TOKEN, "Total Passenger"),
    KpiSpec("detection", "Total Detection", "tracking_ids", icons.DETECT, PURPLE, "People detected by AI",
            "Tracking IDs"),
    KpiSpec("expired", "Expired Tickets", "expired_tickets", icons.CLOCK, WARNING, "Ticket validity expired",
            "Expired Tickets"),
)
INVERTED_DELTA = ("invalid", "expired")  # more failures is bad


def delta_text(cur: int, prev: Optional[int]) -> Tuple[str, str]:
    """``(text, direction)`` with direction ``up`` / ``down`` / ``flat``; ``"—"`` without a comparison."""
    if prev is None:
        return "—", "flat"
    cur, prev = int(cur), int(prev)
    if prev == 0:
        return ("0%", "flat") if cur == 0 else ("New", "up")
    pct = round((cur - prev) / prev * 100)
    return f"{abs(pct)}%", ("up" if pct > 0 else "down" if pct < 0 else "flat")


def delta_class(slug: str, direction: str) -> str:
    """``good`` / ``bad`` / ``flat`` (inverted for Invalid and Expired)."""
    if direction == "flat":
        return "flat"
    good = direction == "up"
    if slug in INVERTED_DELTA:
        good = not good
    return "good" if good else "bad"


def _delta_html(slug: str, cur: int, prev: Optional[int]) -> str:
    text, direction = delta_text(cur, prev)
    arrow = {"up": "▲", "down": "▼"}.get(direction, "")
    mark = (f'<span aria-hidden="true">{arrow}</span><span class="tpd-sr">{direction}</span> ' if arrow else "")
    return (f'<div class="tpd-delta tpd-delta--{delta_class(slug, direction)}" title="{_e(DELTA_TIP)}">'
            f'{mark}{_e(text)}<span class="tpd-sr"> {_e(DELTA_TIP)}</span></div>')


def kpi_cards_html(current: DashboardMetrics, previous: Optional[DashboardMetrics], period_label: str) -> str:
    """One ``.tpd-kpis`` grid of five cards (``data-kpi`` / ``data-value`` are the test hooks)."""
    cards = []
    for spec in KPI_SPECS:
        value = int(getattr(current, spec.field))
        prev = None if previous is None else int(getattr(previous, spec.field))
        caption = spec.caption.replace(PERIOD_TOKEN, str(period_label))
        surface = f"background:{tint(spec.colour, .10)};border-color:{tint(spec.colour, .45)}"
        cards.append(
            f'<div class="tpd-kpi tpd-kpi--{spec.slug}" data-kpi="{spec.slug}" data-value="{value}" '
            f'title="{_e(METRIC_HELP[spec.help_key])}" style="{surface}">'
            f'<span class="tpd-kpi-icon" style="background:{spec.colour};'
            f'box-shadow:0 0 0 5px {tint(spec.colour, .22)},0 0 18px {tint(spec.colour, .5)}">'
            f'{icons.icon(spec.icon, WHITE, 28)}</span><div class="tpd-kpi-body">'
            f'<div class="tpd-kpi-label">{_e(spec.label)}</div><div class="tpd-kpi-value">{value}</div>'
            f'{_delta_html(spec.slug, value, prev)}<div class="tpd-kpi-caption">{_e(caption)}</div></div></div>')
    return '<div class="tpd-kpis">' + "".join(cards) + "</div>"


# ------------------------------------------------------------------ top bar, banner, footer
def topbar_left_html() -> str:
    return (f'<div class="tpd-brand"><span class="tpd-brand-tile">{icons.icon(icons.BUS, PRIMARY, 40)}</span><div>'
            f'<div class="tpd-title">{_e(BRAND_TITLE)}</div><div class="tpd-subtitle">{_e(BRAND_SUBTITLE)}</div>'
            "</div></div>")


def topbar_right_html(date_text: str, time_text: str, online: bool, detection_label: str) -> str:
    """Local date/time, divider and the conductor-console block (name, status; tooltip + hidden text)."""
    tip = f"Conductor console: Online · {detection_label}" if online else OFFLINE_TIP
    state = "online" if online else "offline"
    name = "Conductor"
    status_line = "Online" if online else "Offline"
    return (f'<div class="tpd-topbar-right"><span class="tpd-datetime">{icons.icon(icons.CALENDAR, ICON_MUTED, 20)}'
            f'<span class="tpd-date">{_e(date_text)}</span><span class="tpd-time">{_e(time_text)}</span></span>'
            '<span class="tpd-divider" aria-hidden="true"></span>'
            f'<div class="tpd-conductor" title="{_e(tip)}">'
            f'<span class="tpd-avatar" aria-hidden="true">{icons.icon(icons.USER, ICON_MUTED, 34)}</span>'
            f'<div><div class="tpd-conductor-name">{_e(name)}</div><div class="tpd-conductor-status">'
            f'<span class="tpd-status-dot tpd-status-dot--{state}" aria-hidden="true"></span>'
            f'{status_line}</div></div><span class="tpd-sr">{_e(tip)}</span></div></div>')


def route_summary(cfg: AppConfig) -> str:
    """``"<route_id> | first → last"`` in the journey direction, or ``"No route configured"`` (plain text)."""
    route = cfg.route
    if not route.route_id:
        return "No route configured"
    stations = list(route.stations)
    if cfg.journey.direction == "inbound":
        stations.reverse()
    if len(stations) >= 2:
        return f"{route.route_id} | {stations[0]} → {stations[-1]}"
    if stations:  # defensive: RouteConfig.from_dict enforces >= 2 stations
        return f"{route.route_id} | {stations[0]}"
    return str(route.route_id)


def banner_html(online: bool, label: str, tone: str, route_text: str) -> str:
    """Gradient welcome banner: bus tile, greeting, detection pill and the route card."""
    colour = SUCCESS if online else TONE_COLORS.get(tone, MUTED)
    pill = "System Online" if online else label
    return (f'<div class="tpd-banner"><span class="tpd-banner-tile">{icons.icon(icons.BUS, PRIMARY_TEXT, 48)}</span>'
            f'<div><h2>{_e(BANNER_TITLE)}</h2><p>{_e(BANNER_TEXT)}</p></div>'
            f'<span class="tpd-banner-pill" role="status" title="{_e(label)}">'
            f'<span class="tpd-dot" aria-hidden="true" style="background:{_e(colour)}"></span>{_e(pill)}</span>'
            f'<div class="tpd-route-card"><span class="tpd-route-tile">{icons.icon(icons.BUS, WHITE, 26)}</span>'
            '<div><div class="tpd-route-label">Bus Route</div>'
            f'<div class="tpd-route-text">{_e(route_text)}</div></div></div></div>')


def sidebar_footer_html() -> str:
    return (f'<div class="tpd-sidebar-footer"><div class="tpd-footer-bus">{icons.icon(icons.BUS, PRIMARY, 88)}</div>'
            f'<em class="tpd-tagline">{_e(TAGLINE)}</em></div>')


# ------------------------------------------------------------------ donut legend
class DonutSlice(NamedTuple):
    label: str
    count: int
    percent: int
    colour: str


DONUT_BUCKETS: Tuple[Tuple[str, Tuple[str, ...], str], ...] = (
    ("Valid", ("VALID",), SUCCESS),
    ("Invalid", ("INVALID", "WRONG_ROUTE", "ALREADY_USED"), ERROR),
    ("Expired", ("EXPIRED",), WARNING),
    ("Unverified", ("VERIFICATION_REQUIRED",), PURPLE),
)


def donut_counts(df: pd.DataFrame) -> Tuple[DonutSlice, ...]:
    """``outcome_distribution`` rows grouped into Valid / Invalid / Expired / Unverified (zero buckets kept)."""
    found = {}
    if df is not None and len(df):
        found = {str(o): int(c) for o, c in zip(df["outcome"], df["count"])}
    counts = [(label, sum(found.get(o, 0) for o in codes), colour) for label, codes, colour in DONUT_BUCKETS]
    total = sum(c for _, c, _ in counts)
    return tuple(DonutSlice(label, n, round(n / total * 100) if total else 0, colour)
                 for label, n, colour in counts)


def legend_html(counts: Iterable[DonutSlice]) -> str:
    rows = []
    for s in counts:
        extra = ""
        tip = ""
        if s.label == "Unverified":
            tip = f' title="{_e(VERIFICATION_WORDING)}"'
            extra = f'<span class="tpd-sr"> ({_e(VERIFICATION_WORDING)})</span>'
        rows.append(f'<div class="tpd-legend-row" data-bucket="{_e(s.label)}"{tip}>'
                    f'<span class="tpd-legend-dot" aria-hidden="true" style="background:{_e(s.colour)}"></span>'
                    f'<span>{_e(s.label)}</span>{extra}'
                    f'<span class="tpd-legend-value">{int(s.count)} ({int(s.percent)}%)</span></div>')
    return ('<div class="tpd-legend" title="Distinct credentials checked in this period">' + "".join(rows)
            + "</div>")


# ------------------------------------------------------------------ live feed card
def latest_failed_check(groups: Sequence[AlertGroup]) -> Optional[AlertGroup]:
    """Newest group of a completed failed check (invalid ticket / unrecognised QR), else None."""
    return next((g for g in groups if g.kind in FAILED_CHECK_KINDS), None)


def track_label(track_id: Optional[int]) -> str:
    """``"003"`` style Track ID, or ``"—"`` for a check without a linked boarding."""
    if track_id is None or (isinstance(track_id, float) and pd.isna(track_id)):
        return "—"
    return f"{int(track_id):03d}"


def _local_time(value: datetime, tz: ZoneInfo) -> str:
    return value.astimezone(tz).strftime("%I:%M:%S %p")


def alert_panel_html(group: Optional[AlertGroup], tz: ZoneInfo, route_id: Optional[str]) -> str:
    """The latest failed check (red) or the neutral "no failed checks" panel."""
    if group is None:
        return (f'<div class="tpd-alert tpd-alert--ok" role="status"><div class="tpd-alert-head">'
                f'<span class="tpd-alert-icon">{icons.icon(icons.CHECK, WHITE, 20)}</span>'
                f'<span class="tpd-alert-title">{_e(NO_ALERT_HEADLINE)}</span></div>'
                f'<div class="tpd-alert-row">{_e(NO_ALERT_TEXT)}</div></div>')
    rows = (f"ID: {track_label(group.track_id)}", f"Time: {_local_time(group.last_at, tz)}",
            f"Route: {route_id or '—'}")
    return (f'<div class="tpd-alert" role="status"><div class="tpd-alert-head">'
            f'<span class="tpd-alert-icon">{icons.icon(icons.WARNING, WHITE, 20)}</span>'
            f'<span class="tpd-alert-title">{_e(ALERT_HEADLINE)}</span></div>'
            + "".join(f'<div class="tpd-alert-row">{_e(r)}</div>' for r in rows) + "</div>")


def feed_placeholder_html(message: str) -> str:
    return (f'<div class="tpd-feed-placeholder">{icons.icon(icons.CAMERA_OFF, MUTED, 40)}'
            f'<span>{_e(message)}</span></div>')


# ------------------------------------------------------------------ validation table
PILL_CLASS = {"VALID": "valid", "INVALID": "invalid", "WRONG_ROUTE": "invalid", "ALREADY_USED": "invalid",
              "EXPIRED": "expired", "VERIFICATION_REQUIRED": "unverified"}
HEADER_LABELS = {"Ticket ID": "Ticket"}
DEFAULT_COLUMNS = 7


def _cell(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value)


def validation_table_html(frame: pd.DataFrame, outcomes: Sequence[str], *, empty_text: str) -> str:
    """``tables.validation_table`` frame as a striped HTML table; ``outcomes`` are the raw codes per row."""
    outcomes = list(outcomes)
    if len(frame) != len(outcomes):
        raise ValueError(f"{len(frame)} rows but {len(outcomes)} outcome codes")
    columns = list(frame.columns)
    head = "".join(f'<th scope="col">{_e(HEADER_LABELS.get(c, c))}</th>' for c in columns)
    body = []
    for code, row in zip(outcomes, frame.itertuples(index=False, name=None)):
        cells = []
        for column, raw in zip(columns, row):
            value = _cell(raw)
            if column == "Time (Local)":
                cells.append(f'<td title="{_e(value)}">{_e(value[11:19])}</td>')
            elif column == "Outcome":
                pill = PILL_CLASS.get(str(code), "unverified")
                cells.append(f'<td><span class="tpd-pill tpd-pill--{pill}">{_e(value)}</span></td>')
            elif column == "Reason":
                cells.append(f'<td class="tpd-reason" title="{_e(value)}">{_e(value)}</td>')
            else:
                cells.append(f"<td>{_e(value)}</td>")
        body.append(f'<tr data-outcome="{_e(code)}">' + "".join(cells) + "</tr>")
    if not body:
        span = len(columns) or DEFAULT_COLUMNS
        body.append(f'<tr><td colspan="{int(span)}" class="tpd-empty">{_e(empty_text)}</td></tr>')
    return ('<div class="tpd-table-wrap" role="region" aria-label="Recent validation results" tabindex="0">'
            f'<table class="tpd-table"><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>')
