"""Passenger Tracking: passenger KPIs, boardings, detection sessions, Track ID lookup and live tracks."""

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

from app.alert_service import outcome_label  # noqa: E402
from app.analytics import METRIC_HELP, compute_metrics, recent_boardings  # noqa: E402
from app.database import utc_now  # noqa: E402
from app.tracking_queries import live_tracks, recent_sessions, track_history  # noqa: E402
from dashboard.components import data, metrics, shell, tables  # noqa: E402
from dashboard.components.shell import PageContext  # noqa: E402

SESSION_COLUMNS = ["Started (Local)", "Source", "State", "Frames", "FPS", "Boardings", "Station", "Trip", "Error"]


def sessions_frame(df: pd.DataFrame, tz) -> pd.DataFrame:
    rows = [{"Started (Local)": tables.local_time(r.started_at, tz), "Source": tables._text(r.source_label),
             "State": r.state, "Frames": int(r.frames_processed or 0),
             "FPS": "" if pd.isna(r.fps) else f"{float(r.fps):.1f}", "Boardings": int(r.boardings),
             "Station": tables._text(r.station), "Trip": tables._text(r.trip_id),
             "Error": (tables._text(r.last_error).splitlines() or [""])[0][:120]} for r in df.itertuples(index=False)]
    return pd.DataFrame(rows, columns=SESSION_COLUMNS)


def history_frame(hist: dict, tz) -> pd.DataFrame:
    def t(value):
        return tables.local_time(value, tz) if value else ""
    rows = [("First seen (local)", t(hist["first_seen"])), ("Last seen (local)", t(hist["last_seen"])),
            ("Detection samples", str(hist["samples"])),
            ("Max confidence", "" if hist["max_confidence"] is None else f"{hist['max_confidence']:.2f}"),
            ("Boarded (local)", t(hist["boarded_at"]) or "Not boarded"),
            ("Verification status", outcome_label(hist["verification_status"]) if hist["verification_status"]
             else ""), ("Ticket", hist["ticket_id"] or "")]
    rows += [(f"Check {t(at)}", f"{outcome_label(outcome)} – {reason}") for _id, at, outcome, reason in
             hist["validations"]]
    return pd.DataFrame(rows, columns=["Field", "Value"])


def _body_fragment(ctx: PageContext) -> None:
    now = utc_now()
    period = ctx.period_at(now)
    cfg = ctx.cfg
    with shell.guard():
        with data.read_conn() as conn:
            values = compute_metrics(conn, period)
            with st.container(key="mini-kpis"):
                for column, label in zip(st.columns(3), ("Total Passenger", "Boarding Events", "Tracking IDs")):
                    with column:
                        metrics.metric_card(label, getattr(values, label.lower().replace(" ", "_")),
                                            METRIC_HELP[label])
            tables.render_boardings(recent_boardings(conn, period, list(ctx.statuses), cfg.dashboard.table_limit),
                                    ctx.tz)
            st.subheader("Detection sessions")
            sessions = recent_sessions(conn, period, now=now, stale_sec=cfg.runtime.heartbeat_stale_sec)
            if sessions.empty:
                st.caption("No detection sessions in this period.")
            else:
                st.dataframe(sessions_frame(sessions, ctx.tz), hide_index=True, width="stretch")
            st.subheader("Track ID lookup")
            st.caption("Track IDs are assigned per detection session, so choose the session too.")
            left, right = st.columns(2)
            track = left.number_input("Track ID", min_value=0, step=1, value=None, key="trk_lookup_id")
            options = list(sessions["session_id"]) if not sessions.empty else []
            labels = {sid: f"{tables.local_time(s, ctx.tz)} · {src}" for sid, s, src in
                      zip(sessions.get("session_id", []), sessions.get("started_at", []),
                          sessions.get("source_label", []))}
            session_id = right.selectbox("Session", options, format_func=labels.get, key="trk_lookup_session",
                                         index=None if not options else 0)
            if track is not None and session_id:
                hist = track_history(conn, session_id, int(track))
                if hist is None:
                    st.caption(f"Track ID {int(track)} was not seen in that session.")
                else:
                    st.dataframe(history_frame(hist, ctx.tz), hide_index=True, width="stretch")
            health = shell.current_health(conn, cfg, now)
            if health.online and health.session_id:
                window = max(10.0, 3 * cfg.runtime.heartbeat_stale_sec)
                tables.render_live_tracks(live_tracks(conn, health.session_id, now, window), ctx.tz)


ctx = shell.start_page("Passenger Tracking")
st.fragment(run_every=ctx.cfg.dashboard.refresh_sec)(_body_fragment)(ctx)
