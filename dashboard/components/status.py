"""Detection source status, live preview frame and verification alerts.

The preview is a JPEG written by the detection process; the dashboard only
reads the file (it never opens a camera or loads a model).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

import streamlit as st

from app.alert_service import Alert
from app.analytics import SourceStatus
from app.config import resolve_path
from app.settings_schema import AppConfig

MAX_ALERT_BOXES = 10


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


def preview_file(status: SourceStatus, cfg: AppConfig, now: datetime) -> Optional[Path]:
    """The preview JPEG, only while detection runs and the file itself is fresh."""
    if not status.running:
        return None
    path = resolve_path(cfg.runtime.preview_path)
    try:
        age = now.timestamp() - path.stat().st_mtime
    except OSError:
        return None
    return path if age <= cfg.runtime.heartbeat_stale_sec else None


def render_preview(status: SourceStatus, cfg: AppConfig, now: datetime) -> None:
    path = preview_file(status, cfg, now)
    if path is None:
        return
    try:
        data = path.read_bytes()  # bytes, so the writer can replace the file while we display it
    except OSError:
        return
    st.image(data, caption="Live preview (latest processed frame)", width="stretch")


def render_alerts(alerts: Sequence[Alert]) -> None:
    st.subheader("Verification alerts")
    if not alerts:
        st.caption("No verification alerts in this period.")
        return
    for alert in alerts[:MAX_ALERT_BOXES]:
        if alert.kind == "verification_failed":
            st.error(alert.message)
        else:
            st.warning(alert.message)
    if len(alerts) > MAX_ALERT_BOXES:
        st.caption(f"{len(alerts) - MAX_ALERT_BOXES} more alerts not shown; see the tables below.")
