"""Detection health: the single owner of the "is detection online?" rule (pure, no Streamlit).

``detection_health`` classifies the latest detection session into one of six
states. ``online`` is True only for ``ONLINE`` (a RUNNING session with a fresh
heartbeat), so the dashboard can never show "Online" while detection is
stopped, crashed or silent. The heartbeat is written only from the frame loop,
so a fresh heartbeat proves that frames are being processed.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union
from zoneinfo import ZoneInfo

from app.analytics import SourceStatus
from app.settings_schema import AppConfig, RuntimeConfig
from app.video_source import CAMERA_ERROR_PREFIXES

logger = logging.getLogger(__name__)

NO_SESSION, ONLINE, NOT_RESPONDING = "NO_SESSION", "ONLINE", "NOT_RESPONDING"
CAMERA_DISCONNECTED, FAILED, STOPPED = "CAMERA_DISCONNECTED", "FAILED", "STOPPED"
STATES = (NO_SESSION, ONLINE, NOT_RESPONDING, CAMERA_DISCONNECTED, FAILED, STOPPED)
FEEDS = ("live", "waiting", "disabled", "off")


@dataclass(frozen=True)
class DetectionHealth:
    state: str
    online: bool
    label: str            # header chip text, e.g. "Detection: Online"
    tone: str             # success | warning | error | muted
    detail: str           # the render_source_status sentence
    source_label: Optional[str]
    heartbeat_age_sec: Optional[float]
    last_update_at: Optional[datetime]
    feed: str             # live | waiting | disabled | off
    session_id: Optional[str]
    station: Optional[str]
    trip_id: Optional[str]
    last_error: Optional[str]


def is_camera_error(text: Optional[str]) -> bool:
    """True iff ``text`` is one of the video-source connection messages (see CAMERA_ERROR_PREFIXES)."""
    return bool(text) and str(text).startswith(CAMERA_ERROR_PREFIXES)


def format_age(seconds: float) -> str:
    """Largest whole unit, floored: ``"N s"`` / ``"N min"`` / ``"N h"`` / ``"N d"``; negative -> ``"0 s"``."""
    s = max(0, int(seconds // 1)) if seconds > 0 else 0
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h"
    return f"{s // 86400} d"


def preview_mtime(path: Union[str, Path]) -> Optional[float]:
    """mtime of the preview JPEG, or None if it cannot be read."""
    try:
        return os.stat(path).st_mtime
    except OSError as exc:
        logger.debug("Preview file not available (%s): %s", path, exc)
        return None


def _local(value: Optional[datetime], tz: ZoneInfo, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return value.astimezone(tz).strftime(fmt) if value else "unknown time"


def source_detail(status: SourceStatus, tz: ZoneInfo) -> str:
    """The detection-source sentence (identical to the dashboard's existing status sentence)."""
    if status.running:
        return (f"Detection running – {status.source_label} – last update "
                f"{_local(status.last_heartbeat_at, tz, '%H:%M:%S')} (local)")
    message = "Detection is not running. Start it with scripts\\run_detection.bat"
    if status.session_id is not None:
        state = "RUNNING (no heartbeat)" if status.status == "RUNNING" else status.status
        when = status.ended_at or status.last_heartbeat_at
        details = f"last session: {state} at {_local(when, tz)}"
        if status.last_error:
            details += f"; {status.last_error}"
        message += f" ({details})"
    return message + "."


def detection_health(source: SourceStatus, now: datetime, runtime_cfg: RuntimeConfig,
                     preview_mtime: Optional[float], tz: Union[ZoneInfo, str] = "UTC") -> DetectionHealth:
    """Classify the latest session (first match wins; see docs/user_guide.md "Header states")."""
    tz = tz if isinstance(tz, ZoneInfo) else ZoneInfo(str(tz))
    detail = source_detail(source, tz)
    common = dict(detail=detail, source_label=source.source_label, last_update_at=source.last_heartbeat_at,
                  session_id=source.session_id, station=source.station, trip_id=source.trip_id,
                  last_error=source.last_error)
    if source.session_id is None:
        return DetectionHealth(NO_SESSION, False, "Detection: Not started", "muted", heartbeat_age_sec=None,
                               feed="off", **common)
    age = None
    if source.last_heartbeat_at is not None:
        age = (now.astimezone(timezone.utc) - source.last_heartbeat_at).total_seconds()
    stale = runtime_cfg.heartbeat_stale_sec
    if source.status == "RUNNING":
        if age is not None and age <= stale:
            return DetectionHealth(ONLINE, True, "Detection: Online", "success", heartbeat_age_sec=age,
                                   feed=_feed(now, runtime_cfg, preview_mtime), **common)
        label = f"Detection: Not responding (last heartbeat {format_age(age or 0)} ago)"
        return DetectionHealth(NOT_RESPONDING, False, label, "warning", heartbeat_age_sec=age, feed="off",
                               **common)
    if source.status == "FAILED":
        if is_camera_error(source.last_error):
            return DetectionHealth(CAMERA_DISCONNECTED, False, "Camera: Disconnected", "error",
                                   heartbeat_age_sec=age, feed="off", **common)
        return DetectionHealth(FAILED, False, "Detection: Failed", "error", heartbeat_age_sec=age, feed="off",
                               **common)
    return DetectionHealth(STOPPED, False, "Detection: Stopped", "muted", heartbeat_age_sec=age, feed="off",
                           **common)


def _feed(now: datetime, runtime_cfg: RuntimeConfig, mtime: Optional[float]) -> str:
    if runtime_cfg.preview_interval_sec <= 0:
        return "disabled"
    if mtime is not None and now.timestamp() - mtime <= runtime_cfg.heartbeat_stale_sec:
        return "live"
    return "waiting"


def route_label(cfg: AppConfig, health: DetectionHealth, now: datetime) -> str:
    """Configured route (never invented); a neutral session label when no route is configured."""
    from app.ticket_validator import build_journey  # local import keeps this module light

    route = cfg.route
    if route.route_id and route.name:
        text = f"Route {route.route_id} – {route.name} ({cfg.journey.direction})"
        if health.online:
            text += f" · Station {health.station or 'not set'}"
            if health.trip_id:
                text += f" · Trip {health.trip_id}"
            return text
        station = cfg.journey.current_station or "not set"
        trip = build_journey(cfg, now=now).trip_id
        return text + f" · Station {station} · Trip (next run) {trip}"
    if health.session_id:
        return f"Session {health.session_id[:8]}"
    return "No detection session yet"
