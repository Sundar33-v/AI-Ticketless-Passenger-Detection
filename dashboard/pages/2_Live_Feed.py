"""Live Feed: source configuration, detection control commands, live preview, live tracks and all alerts.

The preview is the frame the detection process writes (with its person boxes
and Track IDs); this page never opens a camera.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Streamlit puts dashboard/ first on sys.path, where dashboard/app.py would shadow the
# project's `app` package. Always force the project root to the front.
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != ROOT]
sys.path.insert(0, str(ROOT))
if "app" in sys.modules and not hasattr(sys.modules["app"], "__path__"):
    del sys.modules["app"]  # drop a cached non-package `app` module

import streamlit as st  # noqa: E402

from app.alert_service import alert_groups  # noqa: E402
from app.database import utc_now  # noqa: E402
from app.tracking_queries import live_tracks  # noqa: E402
from app.video_source import redact_url  # noqa: E402
from dashboard.components import alerts, data, shell, status, tables  # noqa: E402
from dashboard.components.shell import PageContext  # noqa: E402

COMMANDS = ("scripts\\run_detection.bat --source phone\n"
            "scripts\\run_detection.bat --source webcam\n"
            "scripts\\run_detection.bat --source video --path data\\videos\\<file>.mp4")


def render_live_tracks(conn, ctx: PageContext, health, now) -> None:
    """Tracks of the running session (only while detection is online)."""
    if not health.online or not health.session_id:
        return
    window = max(10.0, 3 * ctx.cfg.runtime.heartbeat_stale_sec)
    tables.render_live_tracks(live_tracks(conn, health.session_id, now, window), ctx.tz)


def _preview_fragment(ctx: PageContext) -> None:
    now = utc_now()
    with shell.guard():
        with data.read_conn() as conn:
            health = shell.current_health(conn, ctx.cfg, now)
        st.subheader("Live preview")
        if health.source_label:
            st.caption(f"Latest session source: {health.source_label} · {health.label}")
        status.render_live_feed(health, ctx.cfg, now)


def _body_fragment(ctx: PageContext) -> None:
    now = utc_now()
    period = ctx.period_at(now)
    with shell.guard():
        with data.read_conn() as conn:
            health = shell.current_health(conn, ctx.cfg, now)
            render_live_tracks(conn, ctx, health, now)
            groups = alert_groups(conn, period, now, ctx.cfg)
            alerts.render_alert_panel(conn, groups, period, ctx.tz, key="live_alerts", max_items=50)


ctx = shell.start_page("Live Feed")
cfg = ctx.cfg
source_col, control_col = st.columns(2)
with source_col, st.container(border=True, key="card-source"):
    st.markdown("**Source configuration**")
    st.markdown(f"Source type: {cfg.camera.source_type}  \n"
                f"Android phone URL: `{redact_url(cfg.camera.phone_url)}`  \n"
                f"Webcam index: {cfg.camera.webcam_index}  \n"
                f"Recorded video: `{Path(cfg.camera.video_path).name}`")
    st.caption("Values from config/config.yaml / .env / environment. Command-line options used at start-up are "
               "shown in the session label.")
with control_col, st.container(border=True, key="card-control"):
    st.markdown("**Detection control**")
    st.markdown("Start/stop from the dashboard: not available. Detection runs as its own process:")
    st.code(COMMANDS, language=None)
    st.caption("Stop: press q in the detection window or Ctrl+C in its console.")

interval = cfg.runtime.preview_interval_sec
st.fragment(run_every=max(1, int(interval)) if interval > 0 else None)(_preview_fragment)(ctx)
st.fragment(run_every=cfg.dashboard.refresh_sec)(_body_fragment)(ctx)
