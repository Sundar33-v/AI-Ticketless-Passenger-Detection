"""Shared page shell: theme, config/DB, custom sidebar (nav, filters, page slot, footer), top bar, welcome banner.

``PageContext`` deliberately carries no time: every run unit (fragment body or
non-fragment page run) starts with ``now = utc_now(); period = ctx.period_at(now)``
so auto-refreshing fragments never reuse a page-load clock or period.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterator, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo

import streamlit as st
from streamlit.delta_generator import DeltaGenerator

from app.alert_service import AlertGroupsResult, alert_groups, unread_count
from app.analytics import Period, period_for, source_status, synthetic_row_count
from app.config import resolve_path
from app.database import to_iso, utc_now
from app.settings_schema import AppConfig
from app.system_status import DetectionHealth, detection_health, preview_mtime
from dashboard.components import cards, data, nav, notifications, theme
from dashboard.components import filters as _filters

logger = logging.getLogger("dashboard")

TOAST_KEY = nav.TOAST_KEY


@dataclass(frozen=True)
class PageContext:
    cfg: AppConfig
    tz: ZoneInfo
    period_choice: str
    custom: Optional[Tuple[date, date]]
    statuses: Tuple[str, ...]
    # The page's own sidebar slot (above the footer); not part of equality/hash.
    sidebar: DeltaGenerator = field(default=None, compare=False, repr=False)

    def period_at(self, now: datetime) -> Period:
        return period_for(self.period_choice, self.tz, now, custom=self.custom)


def under_router() -> bool:
    return bool(st.session_state.get(nav.ROUTER_KEY, False))


def read_state() -> dict:
    """The alert read-state map (session copy of ``alert_reads``, see ``notifications``)."""
    return notifications.session_read()


def current_health(conn: sqlite3.Connection, cfg: AppConfig, now: datetime) -> DetectionHealth:
    source = source_status(conn, now, cfg.runtime.heartbeat_stale_sec)
    return detection_health(source, now, cfg.runtime, preview_mtime(resolve_path(cfg.runtime.preview_path)),
                            cfg.app.timezone)


@dataclass(frozen=True)
class HeaderModel:
    health: DetectionHealth
    alerts: AlertGroupsResult
    unread: int
    clock: str
    date_text: str
    time_text: str


def clock_texts(now: datetime, tz: ZoneInfo) -> Tuple[str, str, str]:
    """``(date_text, time_text, clock)`` of ``now`` in ``tz``, e.g. ``("Mon, 20 Oct 2026", "10:58 AM", ...)``."""
    local = now.astimezone(tz)
    date_text = local.strftime("%a, %d %b %Y")
    time_text = local.strftime("%I:%M %p").lstrip("0")
    return date_text, time_text, f"{date_text} · {time_text} {local:%Z}"


def header_model(conn: sqlite3.Connection, ctx: PageContext, now: datetime,
                 read: Optional[Mapping[str, str]] = None,
                 alerts: Optional[AlertGroupsResult] = None) -> HeaderModel:
    """Everything the header shows, computed for ``now`` (pure apart from the DB reads).

    ``alerts`` lets the caller pass groups it already queried (they are then not queried again).
    """
    health = current_health(conn, ctx.cfg, now)
    if alerts is None:
        alerts = alert_groups(conn, ctx.period_at(now), now, ctx.cfg)
    date_text, time_text, clock = clock_texts(now, ctx.tz)
    return HeaderModel(health, alerts, unread_count(alerts.groups, read or {}), clock, date_text, time_text)


@contextmanager
def guard() -> Iterator[None]:
    """Show a database failure in place of the page body; the next refresh retries."""
    try:
        yield
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Dashboard database read failed: %s", exc)
        st.error(f"Database error: {exc}")


def is_newer(group, read: Mapping[str, str]) -> bool:
    """True if ``group`` has an event after the time it was marked read."""
    return to_iso(group.last_at) > read[group.key]


def _load(ctx: PageContext, now: datetime) -> Optional[HeaderModel]:
    """Header model with the saved read state merged in, or None if the database can't be read."""
    try:
        with data.read_conn() as conn:
            alerts = alert_groups(conn, ctx.period_at(now), now, ctx.cfg)
            notifications.sync_read_state(conn, [g.key for g in alerts.groups])
            return header_model(conn, ctx, now, read_state(), alerts=alerts)
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Header refresh could not read the database: %s", exc)
        return None


def _header_fragment(ctx: PageContext, title: str) -> None:
    now = utc_now()
    first = not st.session_state.get(notifications.LOADED_KEY)
    # Later runs read first and fill the slots right after creating them (no flicker or scroll reset).
    model = None if first else _load(ctx, now)
    with st.container(key="tpd-topbar"):
        left, middle, right = st.columns([6, 4, 1], vertical_alignment="center", gap="small")
        with left:
            st.html(cards.topbar_left_html())
        middle_slot = middle.empty()
        with right:
            slots = notifications.open_bell()
    if first:  # first header run of the browser session: show the loading state during the read
        notifications.render_bell(slots, None, "loading", ctx.tz)
        model = _load(ctx, now)
        st.session_state[notifications.LOADED_KEY] = True
    if model is None:
        date_text, time_text, _clock = clock_texts(now, ctx.tz)
        middle_slot.html(cards.topbar_right_html(date_text, time_text, False, "Detection: Unknown"))
        notifications.render_bell(slots, None, None, ctx.tz)
    else:
        middle_slot.html(cards.topbar_right_html(model.date_text, model.time_text, True, model.health.label))
        notifications.render_bell(slots, model.alerts, model.unread, ctx.tz)


def render_header(ctx: PageContext, title: str) -> None:
    st.fragment(run_every=ctx.cfg.dashboard.refresh_sec)(_header_fragment)(ctx, title)


def render_banner(ctx: PageContext, health: DetectionHealth) -> None:
    """Gradient welcome banner: greeting, detection pill ("System Online" only when online) and the route."""
    st.html(cards.banner_html(health.online, health.label, health.tone, cards.route_summary(ctx.cfg)))


def start_page(title: str, *, filters: bool = True, show_title: bool = True) -> PageContext:
    """Theme + config/DB + pending toast + sidebar (nav, "Filters & data", page slot, footer) + top bar.

    Returns the time-free page context; ``ctx.sidebar`` is the page's own sidebar slot above the footer.
    """
    theme.inject_css()
    cfg, _database = data.require_database()
    toast = st.session_state.pop(TOAST_KEY, None)
    if toast:
        st.toast(toast)
    now = utc_now()
    tz = ZoneInfo(cfg.app.timezone)
    nav.render_sidebar_nav(title, routed=under_router())
    box = st.sidebar.expander("Filters & data", expanded=False)
    if filters:  # the parameter shadows the module name, hence the _filters alias
        choice, custom, statuses = _filters.render_sidebar(cfg, now, page=title, target=box)
    else:
        st.session_state["_flt_page"] = title
        choice, custom, statuses = _filters.current_selection(cfg, now)
    box.caption(f"Database: {data.db_path()}")
    try:
        with data.read_conn() as conn:
            synthetic = synthetic_row_count(conn)
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Could not count demo rows: %s", exc)
        synthetic = 0
    if synthetic:
        box.caption(f"Demo data: {synthetic} synthetic rows present")
    page_box = st.sidebar.container(key="sidebar-page")
    st.sidebar.html(cards.sidebar_footer_html())
    ctx = PageContext(cfg, tz, choice, custom, tuple(statuses), sidebar=page_box)
    render_header(ctx, title)
    if show_title:
        st.subheader(title)
    return ctx
