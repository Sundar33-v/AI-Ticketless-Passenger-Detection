"""Detection source status, the Live Feed page panel and the Dashboard's Live Feed card.

The live picture is the preview JPEG written (already overlaid with person
boxes, Track IDs and the entry line) by the detection process; the dashboard
only reads the file. It never opens a camera or loads a model.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

import streamlit as st

from app.alert_service import AlertGroup
from app.analytics import SourceStatus
from app.config import resolve_path
from app.settings_schema import AppConfig
from app.system_status import (CAMERA_DISCONNECTED, NO_SESSION, NOT_RESPONDING, STOPPED, DetectionHealth,
                               format_age)
from dashboard.components import cards, nav, notifications, shell

logger = logging.getLogger("dashboard")
COMPACT_OFF = "No live preview – detection is not running."
NOTIFY_TOAST = "Alert acknowledged – marked as read."
VIEW_TRACKING_CAPTION = "View Tracking: open Passenger Tracking from the sidebar."
WAITING_MESSAGE = "Waiting for the first preview frame…"
DISABLED_MESSAGE = "Live preview is disabled (runtime.preview_interval_sec = 0)."


def placeholder_message(health: DetectionHealth) -> str:
    """Text for the Dashboard feed placeholder when no preview frame is shown (pure)."""
    if health.feed == "disabled":
        return DISABLED_MESSAGE
    if health.feed in ("live", "waiting"):
        return WAITING_MESSAGE
    if health.state in (NO_SESSION, STOPPED):
        return COMPACT_OFF
    return f"No live preview – {health.label}."


def _local(value: Optional[datetime], tz: ZoneInfo, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return value.astimezone(tz).strftime(fmt) if value else "unknown time"


def render_source_status(status: SourceStatus, cfg: AppConfig) -> None:
    tz = ZoneInfo(cfg.app.timezone)
    if status.running:
        st.success(f"Detection running – {status.source_label} – last update "
                   f"{_local(status.last_heartbeat_at, tz, '%H:%M:%S')} (local)")
        return
    message = "Detection is not running. Start it with scripts\\run_detection.bat"
    if status.session_id is not None:
        state = "RUNNING (no heartbeat)" if status.status == "RUNNING" else status.status
        when = status.ended_at or status.last_heartbeat_at
        details = f"last session: {state} at {_local(when, tz)}"
        if status.last_error:
            details += f"; {status.last_error}"
        message += f" ({details})"
    st.info(message + ".")


def _preview_bytes(cfg: AppConfig) -> Optional[bytes]:
    try:
        return resolve_path(cfg.runtime.preview_path).read_bytes()  # bytes: the writer may replace the file
    except OSError as exc:
        logger.debug("Preview read failed: %s", exc)
        return None


def render_feed_card(health: DetectionHealth, cfg: AppConfig, groups: Sequence[AlertGroup], now: datetime,
                     tz: ZoneInfo) -> None:
    """Dashboard Live Feed card: preview frame (or placeholder) + the latest failed check with its actions.

    Notify Conductor only acknowledges (marks the alert read, saved to the database (alert_reads));
    nothing is sent.
    """
    st.html(cards.title_html("Live Feed", live=True))
    frame_col, alert_col = st.columns([1.35, 1], gap="small")
    with frame_col:
        data = _preview_bytes(cfg) if health.feed == "live" else None
        if data is not None:
            with st.container(key="feed-frame"):
                st.image(data, caption=f"Latest processed frame ({now.astimezone(tz):%H:%M:%S} local)",
                         width="stretch")
        else:
            st.html(cards.feed_placeholder_html(placeholder_message(health)))
    with alert_col:
        group = cards.latest_failed_check(groups)
        st.html(cards.alert_panel_html(group, tz, cfg.route.route_id))
        read = shell.read_state()
        disabled = group is None or (group.key in read and not shell.is_newer(group, read))
        if st.button("Notify Conductor", icon=":material/notifications:", key="dash_notify",
                     width="stretch", disabled=disabled) and group is not None:
            message = notifications.mark_read(notifications.items_of([group]))
            st.session_state[shell.TOAST_KEY] = message or NOTIFY_TOAST
            st.rerun()
        if shell.under_router():
            with st.container(key="feed-track-link"):
                st.page_link(nav.PAGE_TRACKING, label="View Tracking", icon=":material/my_location:",
                             width="stretch")
        else:
            st.caption(VIEW_TRACKING_CAPTION)


def render_live_feed(health: DetectionHealth, cfg: AppConfig, now: datetime) -> None:
    """The latest processed frame while detection is online, otherwise a clear state panel."""
    tz = ZoneInfo(cfg.app.timezone)
    if health.feed == "live":
        data = _preview_bytes(cfg)
        if data is not None:
            st.image(data, caption=f"Live preview – latest processed frame ({now.astimezone(tz):%H:%M:%S} local)",
                     width="stretch")
            return
    if health.feed in ("live", "waiting"):
        st.info("Detection is running but no recent preview frame is available yet.")
        return
    if health.feed == "disabled":
        st.info("Live preview is disabled (runtime.preview_interval_sec = 0).")
        return
    if health.state == CAMERA_DISCONNECTED:
        lines = (health.last_error or "").splitlines()
        st.error(f"Camera disconnected – {lines[0] if lines else 'unknown error'}")
        if len(lines) > 1:
            st.caption(lines[-1])
            with st.expander("Details"):
                st.code(health.last_error, language=None)
        st.caption(health.detail)
    elif health.state == NOT_RESPONDING:
        st.warning(f"Detection is not responding (no heartbeat for {format_age(health.heartbeat_age_sec or 0)}). "
                   "The camera may be reconnecting or the process may have stopped.")
    else:
        st.info(health.detail)
