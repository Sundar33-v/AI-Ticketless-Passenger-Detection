"""Dashboard metrics and tables: pure read-only queries over a sqlite3 connection.

The seven metrics (card order): Valid, Invalid, Total Passenger, Total Detection,
Expired Tickets, Boarding Events, Tracking IDs. Every metric uses the same
half-open UTC period ``[start, end)``; ``None`` means unbounded.

No double counting across frames: detections are throttled before they are
written, boardings are unique per (session, Track ID), validation metrics count
distinct credentials, and Track IDs are scoped by detection session.
"""

from __future__ import annotations

import sqlite3
from dataclasses import astuple, dataclass, fields
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple, Union
from zoneinfo import ZoneInfo

import pandas as pd

from app.database import parse_iso, to_iso

PERIOD_CHOICES = ("today", "yesterday", "last_7_days", "all", "custom")
OUTCOMES = ("VALID", "INVALID", "EXPIRED", "WRONG_ROUTE", "ALREADY_USED", "VERIFICATION_REQUIRED")
INVALID_OUTCOMES = ("INVALID", "WRONG_ROUTE", "ALREADY_USED")

TOTAL_PASSENGER_CAPTION = (
    "Total Passenger = verified passengers (distinct passenger refs with a valid ticket) + boarded Track IDs "
    "not linked to a valid ticket (verification required or failed)."
)
METRIC_HELP: Dict[str, str] = {
    "Valid": "Distinct tickets with a VALID check in this period. Repeat scans of one ticket count once.",
    "Invalid": "Distinct credentials whose check returned Invalid, Wrong route or Already used. "
               "Expired tickets are counted separately.",
    "Total Passenger": TOTAL_PASSENGER_CAPTION + " A desk check that is not linked to the passenger's "
                       "boarding can count the same person twice; select the boarding on the Ticket Check "
                       "page to link them.",
    "Total Detection": "Recorded person-detection samples: the first sighting of each Track ID plus at most "
                       "one sample per record interval. This is not a head count.",
    "Expired Tickets": "Distinct tickets whose check failed because the ticket had expired.",
    "Boarding Events": "Entry-line crossings confirmed as boardings (at most one per Track ID per session).",
    "Tracking IDs": "Distinct Track IDs observed, counted per detection session. IDs can change after "
                    "occlusion or when a person leaves and re-enters.",
}


# --------------------------------------------------------------------- periods
@dataclass(frozen=True)
class Period:
    start: Optional[str] = None  # UTC ISO (to_iso), inclusive
    end: Optional[str] = None    # UTC ISO, exclusive

    def params(self) -> Dict[str, Optional[str]]:
        return {"start": self.start, "end": self.end}


def P(col: str) -> str:
    """Half-open period filter on ``col`` using the named parameters ``:start`` and ``:end``."""
    return f"(:start IS NULL OR {col} >= :start) AND (:end IS NULL OR {col} < :end)"


def _midnight(day: date, tz: ZoneInfo) -> str:
    return to_iso(datetime.combine(day, time(0, 0), tzinfo=tz))


def period_for(choice: str, tz: Union[ZoneInfo, str], now_utc: datetime,
               custom: Optional[Tuple[date, date]] = None) -> Period:
    """Convert a local-day choice into UTC bounds (today, yesterday, last_7_days, all, custom)."""
    tz = tz if isinstance(tz, ZoneInfo) else ZoneInfo(str(tz))
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    today = now_utc.astimezone(tz).date()
    one = timedelta(days=1)
    if choice == "today":
        return Period(_midnight(today, tz), _midnight(today + one, tz))
    if choice == "yesterday":
        return Period(_midnight(today - one, tz), _midnight(today, tz))
    if choice == "last_7_days":
        return Period(_midnight(today - timedelta(days=6), tz), _midnight(today + one, tz))
    if choice == "all":
        return Period(None, None)
    if choice == "custom":
        if not custom or len(custom) != 2:
            raise ValueError("custom period needs (start_date, end_date)")
        d1, d2 = custom
        if d1 > d2:
            d1, d2 = d2, d1
        return Period(_midnight(d1, tz), _midnight(d2 + one, tz))
    raise ValueError(f"Unknown period {choice!r}; expected one of {', '.join(PERIOD_CHOICES)}")


# --------------------------------------------------------------------- metrics
@dataclass(frozen=True)
class DashboardMetrics:
    valid: int = 0
    invalid: int = 0
    total_passenger: int = 0
    total_detection: int = 0
    expired_tickets: int = 0
    boarding_events: int = 0
    tracking_ids: int = 0

    LABELS = ("Valid", "Invalid", "Total Passenger", "Total Detection", "Expired Tickets", "Boarding Events",
              "Tracking IDs")

    def cards(self) -> List[Tuple[str, int, str]]:
        """``(label, value, help)`` for the seven cards, in display order."""
        return [(label, value, METRIC_HELP[label]) for label, value in zip(self.LABELS, astuple(self))]

    def is_empty(self) -> bool:
        return not any(astuple(self))


_DISTINCT_CREDENTIAL = ("SELECT COUNT(DISTINCT COALESCE(ticket_id, presented_ref, token_hash, "
                        "'evt:' || validation_id)) FROM validation_events WHERE {cond} AND " + P("validated_at"))
_QUERIES = {
    "valid": _DISTINCT_CREDENTIAL.format(cond="outcome = 'VALID'"),
    "invalid": _DISTINCT_CREDENTIAL.format(cond="outcome IN ('INVALID', 'WRONG_ROUTE', 'ALREADY_USED')"),
    "total_passenger": (
        "SELECT COUNT(*) FROM ("
        "SELECT passenger_ref AS k FROM validation_events WHERE outcome = 'VALID' AND passenger_ref IS NOT NULL "
        "AND " + P("validated_at") + " "
        "UNION SELECT passenger_ref FROM boarding_events WHERE passenger_ref IS NOT NULL AND " + P("boarded_at") +
        " UNION SELECT 'trk:' || session_id || ':' || track_id FROM boarding_events "
        "WHERE passenger_ref IS NULL AND " + P("boarded_at") + ")"),
    "total_detection": "SELECT COUNT(*) FROM detection_records WHERE " + P("detected_at"),
    "expired_tickets": _DISTINCT_CREDENTIAL.format(cond="outcome = 'EXPIRED'"),
    "boarding_events": "SELECT COUNT(*) FROM boarding_events WHERE " + P("boarded_at"),
    "tracking_ids": (
        "SELECT COUNT(*) FROM (SELECT session_id, track_id FROM detection_records WHERE " + P("detected_at") +
        " UNION SELECT session_id, track_id FROM boarding_events WHERE " + P("boarded_at") + ")"),
}


def compute_metrics(conn: sqlite3.Connection, period: Period) -> DashboardMetrics:
    params = period.params()
    values = {f.name: int(conn.execute(_QUERIES[f.name], params).fetchone()[0]) for f in fields(DashboardMetrics)}
    return DashboardMetrics(**values)


# ---------------------------------------------------------------------- tables
def _frame(conn: sqlite3.Connection, sql: str, params: Dict) -> pd.DataFrame:
    cur = conn.execute(sql, params)
    columns = [d[0] for d in cur.description]
    return pd.DataFrame([tuple(r) for r in cur.fetchall()], columns=columns)


def _in_clause(column: str, values: Optional[Sequence[str]], params: Dict, prefix: str) -> str:
    """``AND column IN (:p0, ...)`` for a validated list of outcome names ('' when not filtering)."""
    if values is None:
        return ""
    chosen = [v for v in values if v in OUTCOMES]
    if not chosen:
        return " AND 0"
    names = []
    for i, value in enumerate(chosen):
        params[f"{prefix}{i}"] = value
        names.append(f":{prefix}{i}")
    return f" AND {column} IN ({', '.join(names)})"


def recent_boardings(conn: sqlite3.Connection, period: Period, statuses: Optional[Sequence[str]] = None,
                     limit: int = 50) -> pd.DataFrame:
    """Newest boardings first. Columns: boarding_id, boarded_at, track_id, session_id, station,
    trip_id, verification_status, ticket_id, is_synthetic."""
    params = {**period.params(), "limit": int(limit)}
    sql = ("SELECT boarding_id, boarded_at, track_id, session_id, station, trip_id, verification_status, "
           "ticket_id, is_synthetic FROM boarding_events WHERE " + P("boarded_at")
           + _in_clause("verification_status", statuses, params, "s")
           + " ORDER BY boarded_at DESC, boarding_id DESC LIMIT :limit")
    return _frame(conn, sql, params)


def recent_validations(conn: sqlite3.Connection, period: Period, outcomes: Optional[Sequence[str]] = None,
                       limit: int = 50) -> pd.DataFrame:
    """Newest validations first; ``track_id`` comes from the linked boarding (NULL if none)."""
    params = {**period.params(), "limit": int(limit)}
    sql = ("SELECT v.validation_id, v.validated_at, v.outcome, v.reason, v.ticket_id, v.presented_ref, "
           "b.track_id, v.boarding_id, v.channel, v.station, v.trip_id, v.is_synthetic "
           "FROM validation_events v LEFT JOIN boarding_events b ON b.boarding_id = v.boarding_id WHERE "
           + P("v.validated_at") + _in_clause("v.outcome", outcomes, params, "o")
           + " ORDER BY v.validated_at DESC, v.validation_id DESC LIMIT :limit")
    return _frame(conn, sql, params)


def boardings_needing_resolution(conn: sqlite3.Connection, period: Period, limit: int) -> pd.DataFrame:
    """Boardings in the period whose status is not VALID (verification required or failed), newest first."""
    params = {**period.params(), "limit": int(limit)}
    sql = ("SELECT boarding_id, track_id, boarded_at, verification_status, session_id, trip_id, station "
           "FROM boarding_events WHERE verification_status <> 'VALID' AND " + P("boarded_at")
           + " ORDER BY boarded_at DESC, boarding_id DESC LIMIT :limit")
    return _frame(conn, sql, params)


# ---------------------------------------------------------------------- status
@dataclass(frozen=True)
class SourceStatus:
    running: bool  # latest session RUNNING with a fresh heartbeat
    session_id: Optional[str] = None
    status: Optional[str] = None
    source_label: Optional[str] = None
    started_at: Optional[datetime] = None
    last_heartbeat_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    last_error: Optional[str] = None
    station: Optional[str] = None
    trip_id: Optional[str] = None


def source_status(conn: sqlite3.Connection, now: datetime, stale_sec: float) -> SourceStatus:
    """State of the latest detection session; a RUNNING session with a stale heartbeat counts as stopped."""
    row = conn.execute("SELECT * FROM detection_sessions ORDER BY started_at DESC, rowid DESC LIMIT 1").fetchone()
    if row is None:
        return SourceStatus(False)
    heartbeat = parse_iso(row["last_heartbeat_at"])
    fresh = (now.astimezone(timezone.utc) - heartbeat).total_seconds() <= float(stale_sec)
    return SourceStatus(
        running=row["status"] == "RUNNING" and fresh, session_id=row["session_id"], status=row["status"],
        source_label=row["source_label"], started_at=parse_iso(row["started_at"]), last_heartbeat_at=heartbeat,
        ended_at=parse_iso(row["ended_at"]) if row["ended_at"] else None, last_error=row["last_error"],
        station=row["station"], trip_id=row["trip_id"])


def inherited_trip_id(conn: sqlite3.Connection, *, boarding_id: Optional[int], now: datetime,
                      stale_sec: float) -> Optional[str]:
    """trip_id for a desk (dashboard) or CLI check, so re-checks stay on the camera's trip.

    Order: the selected boarding's trip_id, else the trip_id of the latest
    RUNNING session with a fresh heartbeat, else None (``build_journey`` then
    falls back to ``journey.trip_id`` or the derived ``<route>-<direction>-<date>``).
    """
    if boarding_id is not None:
        row = conn.execute("SELECT trip_id FROM boarding_events WHERE boarding_id = ?",
                           (int(boarding_id),)).fetchone()
        if row is not None and row[0]:
            return str(row[0])
    status = source_status(conn, now, stale_sec)
    return status.trip_id if status.running and status.trip_id else None


SYNTHETIC_TABLES = ("passengers", "tickets", "detection_sessions", "detection_records", "boarding_events",
                    "validation_events")


def synthetic_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    """Demo rows (is_synthetic = 1) per data table (fixed identifiers, never user input)."""
    return {t: int(conn.execute(f"SELECT COUNT(*) FROM {t} WHERE is_synthetic = 1").fetchone()[0])
            for t in SYNTHETIC_TABLES}


def synthetic_row_count(conn: sqlite3.Connection) -> int:
    """Number of demo rows (is_synthetic = 1) across all data tables."""
    return sum(synthetic_counts(conn).values())
