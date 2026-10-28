"""Settings: read-only effective configuration (allow-list; no secrets, credentials or absolute paths)."""

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

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from app.video_source import redact_url  # noqa: E402
from dashboard.components import data, shell  # noqa: E402
from dashboard.components.filters import PERIOD_LABELS  # noqa: E402


def section(title: str, rows) -> None:
    st.markdown(f"**{title}**")
    st.dataframe(pd.DataFrame([(k, str(v)) for k, v in rows], columns=["Setting", "Value"]), hide_index=True,
                 width="stretch")


ctx = shell.start_page("Settings", filters=False)
cfg = ctx.cfg
st.caption("Effective values after merging defaults, config/config.yaml, .env and environment variables (later "
           "wins). Command-line options of a running detection process are shown in its session label on Live Feed.")
left, right = st.columns(2)
with left:
    section("Detection", [("Model file", Path(cfg.detection.model_path).name), ("Device", cfg.detection.device),
                          ("Person confidence", cfg.detection.confidence), ("NMS IoU", cfg.detection.iou),
                          ("Image size", cfg.detection.imgsz)])
    section("Camera", [("Source type", cfg.camera.source_type), ("Android phone URL", redact_url(cfg.camera.phone_url)),
                       ("Webcam index", cfg.camera.webcam_index),
                       ("Recorded video file", Path(cfg.camera.video_path).name)])
    section("Route and journey", [("Route", f"{cfg.route.route_id} – {cfg.route.name}"),
                                  ("Stations", ", ".join(cfg.route.stations)), ("Direction", cfg.journey.direction),
                                  ("Current station", cfg.journey.current_station or "not set"),
                                  ("Trip ID", cfg.journey.trip_id or "derived per day")])
with right:
    section("Alerts", [("Verification timeout (s)", cfg.alerts.verification_timeout_sec)])
    section("Dashboard", [("Refresh (s)", cfg.dashboard.refresh_sec),
                          ("Default period", PERIOD_LABELS.get(cfg.dashboard.default_period)),
                          ("Table rows per page", cfg.dashboard.table_limit)])
    section("Runtime", [("Heartbeat stale after (s)", cfg.runtime.heartbeat_stale_sec),
                        ("Preview interval (s)", cfg.runtime.preview_interval_sec)])
    with shell.guard():
        with data.read_conn() as conn:
            row = conn.execute("SELECT value FROM system_settings WHERE key = ?", ("schema_version",)).fetchone()
        section("Database", [("Schema version", row[0] if row else "unknown")])
st.caption("Change settings in config/config.yaml (or .env / command-line options) and restart the affected process. "
           "The database path is shown in the sidebar.")
