"""Passenger-tracking queries: live tracks, detection sessions and Track ID history (read-only)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import pandas as pd

from app.analytics import SESSION_AT, P, Period, _frame
from app.database import parse_iso, to_iso
from app.system_status import is_camera_error


def live_tracks(conn: sqlite3.Connection, session_id: str, now: datetime, window_sec: float) -> pd.DataFrame:
    """Track IDs of ``session_id`` with a detection record in the last ``window_sec`` seconds.

    Columns: track_id, last_seen, confidence (of the latest sample), boarded_at, verification_status.
    """
    params = {"sid": session_id, "since": to_iso(now - timedelta(seconds=float(window_sec)))}
    # SQLite returns the bare column `confidence` from the row holding MAX(detected_at).
    sql = ("SELECT d.track_id, MAX(d.detected_at) AS last_seen, d.confidence, b.boarded_at, b.verification_status "
           "FROM detection_records d LEFT JOIN boarding_events b "
           "ON b.session_id = d.session_id AND b.track_id = d.track_id "
           "WHERE d.session_id = :sid AND d.detected_at >= :since GROUP BY d.track_id ORDER BY last_seen DESC")
    return _frame(conn, sql, params)


def session_state(status: str, last_heartbeat_at: Optional[str], last_error: Optional[str], now: datetime,
                  stale_sec: float) -> str:
    """Per-row state label, same rule as the header (RUNNING + stale heartbeat -> Not responding)."""
    if status == "RUNNING":
        fresh = last_heartbeat_at and (now - parse_iso(last_heartbeat_at)).total_seconds() <= stale_sec
        return "Running" if fresh else "Not responding"
    if status == "FAILED":
        return "Camera disconnected" if is_camera_error(last_error) else "Failed"
    return "Stopped"


def recent_sessions(conn: sqlite3.Connection, period: Period, *, now: datetime, stale_sec: float,
                    limit: int = 20) -> pd.DataFrame:
    """Sessions in the period (``P(SESSION_AT)``, the alert rule) plus any still RUNNING, newest first."""
    params = {**period.params(), "limit": int(limit)}
    sql = ("SELECT s.session_id, s.started_at, s.source_label, s.status, s.last_heartbeat_at, s.ended_at, "
           "s.frames_processed, s.fps, s.station, s.trip_id, s.last_error, "
           "COALESCE(bc.n, 0) AS boardings FROM detection_sessions s "
           "LEFT JOIN (SELECT session_id, COUNT(*) AS n FROM boarding_events GROUP BY session_id) bc "
           "ON bc.session_id = s.session_id "
           "WHERE s.session_id IN (SELECT session_id FROM detection_sessions WHERE (" + P(SESSION_AT)
           + " OR status = 'RUNNING')) ORDER BY s.started_at DESC, s.rowid DESC LIMIT :limit")
    df = _frame(conn, sql, params)
    df["state"] = [session_state(r.status, r.last_heartbeat_at, r.last_error, now, stale_sec)
                   for r in df.itertuples(index=False)]
    return df


def track_history(conn: sqlite3.Connection, session_id: str, track_id: int) -> Optional[Dict[str, Any]]:
    """First/last seen, samples, max confidence, the boarding and linked checks; None if never seen."""
    params = {"sid": session_id, "tid": int(track_id)}
    det = conn.execute("SELECT MIN(detected_at), MAX(detected_at), COUNT(*), MAX(confidence) FROM detection_records "
                       "WHERE session_id = :sid AND track_id = :tid", params).fetchone()
    board = conn.execute("SELECT boarding_id, boarded_at, verification_status, ticket_id FROM boarding_events "
                         "WHERE session_id = :sid AND track_id = :tid", params).fetchone()
    if not det[2] and board is None:
        return None
    validations = []
    if board is not None:
        validations = conn.execute(
            "SELECT validation_id, validated_at, outcome, reason FROM validation_events WHERE boarding_id = ? "
            "ORDER BY validated_at DESC, validation_id DESC", (int(board[0]),)).fetchall()
    return {"first_seen": det[0], "last_seen": det[1], "samples": int(det[2]), "max_confidence": det[3],
            "boarding_id": board[0] if board else None, "boarded_at": board[1] if board else None,
            "verification_status": board[2] if board else None, "ticket_id": board[3] if board else None,
            "validations": [tuple(v) for v in validations]}
