"""Conductor dashboard (Streamlit): seven metrics, detection status, alerts, recent events.

    streamlit run dashboard/app.py      (or scripts\\run_dashboard.bat, bound to 127.0.0.1)

Reads the same SQLite database as the detection process (``TPD_DB_PATH`` or
``database.path``). This page never writes records; ticket checks are made on
the "Ticket Check" page. The dashboard never opens a camera or loads a model.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Streamlit puts dashboard/ first on sys.path, where this file (app.py) would shadow the
# project's `app` package. Always force the project root to the front.
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != ROOT]
sys.path.insert(0, str(ROOT))
if "app" in sys.modules and not hasattr(sys.modules["app"], "__path__"):
    del sys.modules["app"]  # drop a cached non-package `app` module

from zoneinfo import ZoneInfo  # noqa: E402

import streamlit as st  # noqa: E402

from app.alert_service import verification_alerts  # noqa: E402
from app.analytics import (Period, compute_metrics, recent_boardings, recent_validations,  # noqa: E402
                           source_status, synthetic_row_count)
from app.database import utc_now  # noqa: E402
from app.settings_schema import AppConfig  # noqa: E402
from dashboard.components import data, filters, metrics, status, tables  # noqa: E402

st.set_page_config(page_title="AI Ticketless Passenger Detection – Conductor", layout="wide")
st.title("AI Ticketless Passenger Detection – Conductor")

cfg, _database = data.require_database()
period, statuses = filters.render_sidebar(cfg, utc_now())
st.sidebar.caption(f"Database: {data.db_path()}")
with data.read_conn() as _conn:
    _synthetic = synthetic_row_count(_conn)
if _synthetic:
    st.sidebar.caption(f"Demo data: {_synthetic} synthetic rows present")


def live_panel(cfg: AppConfig, period: Period, statuses: list) -> None:
    """Metrics, source status, preview, alerts and tables; re-run every ``dashboard.refresh_sec``."""
    now = utc_now()
    tz = ZoneInfo(cfg.app.timezone)
    limit = cfg.dashboard.table_limit
    with data.read_conn() as conn:
        values = compute_metrics(conn, period)
        source = source_status(conn, now, cfg.runtime.heartbeat_stale_sec)
        alerts = verification_alerts(conn, period, now, cfg)
        boardings = recent_boardings(conn, period, statuses, limit)
        validations = recent_validations(conn, period, statuses, limit)
    metrics.render_metrics(values)
    st.subheader("Detection source")
    status.render_source_status(source, cfg)
    status.render_preview(source, cfg, now)
    status.render_alerts(alerts)
    tables.render_boardings(boardings, tz)
    tables.render_validations(validations, tz)


st.fragment(run_every=cfg.dashboard.refresh_sec)(live_panel)(cfg, period, statuses)
