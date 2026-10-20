"""Detection sessions, throttled detection records and boarding events.

The module-level helpers below are the only statements in the project that
write sessions, detection records or boarding events; ``PassengerService``
(live pipeline) and ``scripts/generate_dummy_data.py`` (demo data) both use them.

A *detected person* (a Track ID in a frame) is not a *boarding*. A boarding is
created only when a Track ID crosses the entry line in the configured
direction after being seen in at least ``boarding.min_frames_seen`` frames,
and at most once per (session, Track ID); ``UNIQUE(session_id, track_id)``
in the schema is the final owner of that rule. Every new boarding starts as
``VERIFICATION_REQUIRED``.
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Deque, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple, Union

from app.database import Database, DatabaseError, parse_iso, to_iso, transaction, utc_now
from app.entry_line import EntryEvent, EntryLineConfig, EntryLineDetector
from app.settings_schema import AppConfig

logger = logging.getLogger(__name__)

MAX_ERROR_LENGTH = 500
MAX_BUFFERED_ROWS = 5000
MAX_BOARDING_TRIES = 10
_WARN_INTERVAL_SEC = 30.0


def is_busy_error(exc: BaseException) -> bool:
    """True for SQLite 'database is locked' / 'busy' errors (recoverable by retrying)."""
    text = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and ("locked" in text or "busy" in text)


@contextmanager
def _join_or_begin(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run inside the caller's open transaction, or in a new IMMEDIATE one."""
    if conn.in_transaction:
        yield conn
    else:
        with transaction(conn):
            yield conn


# ----------------------------------------------------------------- SQL helpers
@dataclass(frozen=True)
class DetectionRow:
    session_id: str
    track_id: int
    frame_index: int
    detected_at: datetime  # aware
    confidence: float
    x1: float
    y1: float
    x2: float
    y2: float
    is_synthetic: bool = False


def insert_session(conn: sqlite3.Connection, *, session_id: str, source_type: str, source_label: str,
                   status: str, started_at: datetime, station: Optional[str], trip_id: Optional[str],
                   is_synthetic: bool = False) -> None:
    """INSERT OR IGNORE a session (idempotent for deterministic demo IDs)."""
    stamp = to_iso(started_at)
    with _join_or_begin(conn):
        conn.execute(
            "INSERT OR IGNORE INTO detection_sessions (session_id, source_type, source_label, status, started_at, "
            "last_heartbeat_at, ended_at, station, trip_id, is_synthetic) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
            (session_id, source_type, source_label, status, stamp, stamp, station, trip_id, int(is_synthetic)))


def insert_detections(conn: sqlite3.Connection, rows: Iterable[DetectionRow]) -> int:
    """INSERT OR IGNORE detection rows in one statement batch; returns the number inserted."""
    params = [(r.session_id, int(r.track_id), int(r.frame_index), to_iso(r.detected_at),
               min(1.0, max(0.0, float(r.confidence))), float(r.x1), float(r.y1), float(r.x2), float(r.y2),
               int(r.is_synthetic)) for r in rows]
    if not params:
        return 0
    with _join_or_begin(conn):
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO detection_records (session_id, track_id, frame_index, detected_at, confidence, "
            "x1, y1, x2, y2, is_synthetic) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", params)
        return conn.total_changes - before


def insert_boarding(conn: sqlite3.Connection, *, session_id: str, track_id: int, boarded_at: datetime,
                    direction: str, station: Optional[str], trip_id: Optional[str],
                    is_synthetic: bool = False) -> int:
    """Insert a boarding unless (session, track) already boarded; returns the (existing or new) ID."""
    with _join_or_begin(conn):
        conn.execute(
            "INSERT INTO boarding_events (session_id, track_id, boarded_at, direction, station, trip_id, "
            "is_synthetic) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(session_id, track_id) DO NOTHING",
            (session_id, int(track_id), to_iso(boarded_at), direction, station, trip_id, int(is_synthetic)))
        row = conn.execute("SELECT boarding_id FROM boarding_events WHERE session_id = ? AND track_id = ?",
                           (session_id, int(track_id))).fetchone()
    return int(row[0])


def update_heartbeat(conn: sqlite3.Connection, session_id: str, *, at: datetime, frames: int,
                     fps: Optional[float]) -> None:
    with _join_or_begin(conn):
        conn.execute("UPDATE detection_sessions SET last_heartbeat_at = ?, frames_processed = ?, fps = ? "
                     "WHERE session_id = ?", (to_iso(at), int(frames), fps, session_id))


def update_session_source(conn: sqlite3.Connection, session_id: str, *, source_type: str,
                          source_label: str) -> None:
    """Record the source actually opened (e.g. the webcam after a phone fallback)."""
    with _join_or_begin(conn):
        conn.execute("UPDATE detection_sessions SET source_type = ?, source_label = ? WHERE session_id = ?",
                     (source_type, source_label, session_id))


def session_started_at(conn: sqlite3.Connection, session_id: str) -> Optional[datetime]:
    """Start time (aware UTC) of a stored session, or None when it does not exist."""
    row = conn.execute("SELECT started_at FROM detection_sessions WHERE session_id = ?", (session_id,)).fetchone()
    return parse_iso(row[0]) if row else None


def end_session_row(conn: sqlite3.Connection, session_id: str, *, status: str, ended_at: datetime,
                    error: Optional[str] = None) -> None:
    """Mark a session STOPPED/FAILED (idempotent: re-running writes the same values)."""
    stamp = to_iso(ended_at)
    with _join_or_begin(conn):
        conn.execute("UPDATE detection_sessions SET status = ?, ended_at = ?, last_heartbeat_at = ?, "
                     "last_error = ? WHERE session_id = ?",
                     (status, stamp, stamp, error[:MAX_ERROR_LENGTH] if error else None, session_id))


_SYNTHETIC_TABLES = ("validation_events", "boarding_events", "detection_records", "detection_sessions",
                     "tickets", "passengers")


def delete_synthetic(conn: sqlite3.Connection) -> Dict[str, int]:
    """Delete every synthetic (demo) row in one IMMEDIATE transaction; returns per-table counts.

    A non-synthetic row that references a synthetic passenger (FK RESTRICT)
    makes the whole transaction roll back with ``sqlite3.IntegrityError``.
    """
    counts: Dict[str, int] = {}
    with _join_or_begin(conn):
        conn.execute("UPDATE tickets SET used_boarding_id = NULL WHERE used_boarding_id IN "
                     "(SELECT boarding_id FROM boarding_events WHERE is_synthetic = 1)")
        for table in _SYNTHETIC_TABLES:  # fixed identifiers, never user input
            counts[table] = conn.execute(f"DELETE FROM {table} WHERE is_synthetic = 1").rowcount
    return counts


# ------------------------------------------------------------------ throttle
class DetectionThrottle:
    """Buffers at most one detection row per Track ID per ``record_interval_sec`` (plus first sight)."""

    def __init__(self, session_id: str, record_interval_sec: float, max_rows: int = MAX_BUFFERED_ROWS) -> None:
        self.session_id = session_id
        self.interval = float(record_interval_sec)
        self.max_rows = int(max_rows)
        self._last: Dict[int, datetime] = {}
        self._buffer: Deque[DetectionRow] = deque()

    def __len__(self) -> int:
        return len(self._buffer)

    def collect(self, people: Sequence[Any], frame_index: int, now: datetime) -> int:
        """Buffer rows for the tracked people of one frame; returns how many were buffered."""
        added = 0
        for person in people:
            tid = getattr(person, "track_id", None)
            if tid is None:
                continue  # untracked boxes are never recorded
            last = self._last.get(int(tid))
            if last is not None and (now - last).total_seconds() < self.interval:
                continue
            self._last[int(tid)] = now
            x1, y1, x2, y2 = person.bbox
            self._buffer.append(DetectionRow(self.session_id, int(tid), int(frame_index), now,
                                             float(person.confidence), x1, y1, x2, y2))
            added += 1
        self._enforce_cap()
        return added

    def drain(self) -> List[DetectionRow]:
        rows = list(self._buffer)
        self._buffer.clear()
        return rows

    def requeue(self, rows: Sequence[DetectionRow]) -> None:
        """Put rows that could not be written back at the front (oldest first)."""
        self._buffer.extendleft(reversed(list(rows)))
        self._enforce_cap()

    def _enforce_cap(self) -> None:
        dropped = 0
        while len(self._buffer) > self.max_rows:
            self._buffer.popleft()
            dropped += 1
        if dropped:
            logger.warning("Detection buffer full; dropped %d oldest detection rows.", dropped)


# ------------------------------------------------------------- boarding rules
@dataclass(frozen=True)
class PendingCrossing:
    event: EntryEvent
    side: Optional[int]
    deadline: float


class BoardingRules:
    """Entry-line crossing + ``min_frames_seen`` + once per Track ID -> boarding events."""

    def __init__(self, entry_line_cfg: Union[EntryLineConfig, Mapping[str, Any], None], min_frames_seen: int) -> None:
        if not isinstance(entry_line_cfg, EntryLineConfig):
            entry_line_cfg = EntryLineConfig.from_dict(entry_line_cfg)
        self.entry_line = EntryLineDetector(entry_line_cfg)
        self.min_frames_seen = int(min_frames_seen)
        self.boarded: set = set()
        self.pending_crossings: Dict[int, PendingCrossing] = {}

    def update(self, people: Sequence[Any], now: float, frame_size: Tuple[int, int]) -> List[EntryEvent]:
        """Return the crossings confirmed as boardings in this frame (``timestamp`` = crossing time)."""
        events = self.entry_line.update(people, timestamp=now, frame_size=frame_size)
        by_id = {int(p.track_id): p for p in people if getattr(p, "track_id", None) is not None}
        confirmed: List[EntryEvent] = []

        for tid, pending in list(self.pending_crossings.items()):
            side = self.entry_line.current_side(tid)
            person = by_id.get(tid)
            if tid in self.boarded or now > pending.deadline or side is None or side != pending.side:
                del self.pending_crossings[tid]
                logger.debug("crossing not confirmed: Track ID %s", tid)
            elif person is not None and getattr(person, "hits", 1) >= self.min_frames_seen:
                del self.pending_crossings[tid]
                self.boarded.add(tid)
                confirmed.append(pending.event)

        for event in events:
            tid = int(event.track_id)
            if tid in self.boarded:
                continue
            person = by_id.get(tid)
            if person is not None and getattr(person, "hits", 1) >= self.min_frames_seen:
                self.boarded.add(tid)
                confirmed.append(event)
            else:
                self.pending_crossings[tid] = PendingCrossing(
                    event, self.entry_line.current_side(tid), now + self.entry_line.config.cooldown_seconds)
        return confirmed


# --------------------------------------------------------------- service
@dataclass
class BoardingInfo:
    boarding_id: int
    track_id: int
    boarded_at: datetime
    status: str = "VERIFICATION_REQUIRED"


class PassengerService:
    """One per detection process: owns the long-lived writer connection (``.conn``), pipeline thread only."""

    def __init__(self, db: Database, cfg: AppConfig, *, clock=utc_now) -> None:
        self.db = db
        self.cfg = cfg
        self._clock = clock
        self.conn = db.connect()
        self.session_id: Optional[str] = None
        self.throttle: Optional[DetectionThrottle] = None
        self.boardings: Dict[int, BoardingInfo] = {}  # by track_id
        self._queued: List[Tuple[EntryEvent, Optional[str], Optional[str], int]] = []
        self._last_warning = float("-inf")

    # ............................................................ session
    def create_session(self, source_type: str, source_label: str, station: Optional[str],
                       trip_id: Optional[str]) -> str:
        self.session_id = uuid.uuid4().hex
        insert_session(self.conn, session_id=self.session_id, source_type=source_type, source_label=source_label,
                       status="RUNNING", started_at=self._clock(), station=station, trip_id=trip_id)
        self.throttle = DetectionThrottle(self.session_id, self.cfg.detection.record_interval_sec)
        return self.session_id

    def update_source(self, source_type: str, source_label: str) -> None:
        if self.session_id is not None:
            update_session_source(self.conn, self.session_id, source_type=source_type, source_label=source_label)

    def heartbeat(self, frames: int, fps: Optional[float], now: Optional[datetime] = None) -> int:
        """Flush buffered detections and update the heartbeat in one transaction."""
        return self.flush(frames=frames, fps=fps, now=now)

    def end_session(self, status: str, error: Optional[str] = None, *, frames: Optional[int] = None,
                    fps: Optional[float] = None) -> None:
        if self.session_id is None:
            return
        try:
            self.flush(frames=frames, fps=fps)
        except sqlite3.Error as exc:
            logger.error("Could not flush detections at session end: %s", exc)
        end_session_row(self.conn, self.session_id, status=status, ended_at=self._clock(), error=error)

    def close(self) -> None:
        self.conn.close()

    # .......................................................... detections
    def record_detections(self, people: Sequence[Any], frame_index: int, now: datetime) -> int:
        if self.throttle is None:
            raise RuntimeError("create_session() must be called first")
        return self.throttle.collect(people, frame_index, now)

    def flush(self, *, frames: Optional[int] = None, fps: Optional[float] = None,
              now: Optional[datetime] = None) -> int:
        """Write buffered rows (+ heartbeat). On 'database is locked' the rows stay buffered."""
        if self.throttle is None or self.session_id is None:
            return 0
        rows = self.throttle.drain()
        if not rows and frames is None:
            return 0
        try:
            with transaction(self.conn):
                inserted = insert_detections(self.conn, rows)
                if frames is not None:
                    update_heartbeat(self.conn, self.session_id, at=now or self._clock(), frames=frames, fps=fps)
        except sqlite3.OperationalError as exc:
            if not is_busy_error(exc):
                raise
            self.throttle.requeue(rows)
            self._warn("Database busy; %d detection rows kept for the next flush (%s)", len(rows), exc)
            return 0
        return inserted

    # ............................................................ boardings
    def record_boarding(self, event: EntryEvent, *, station: Optional[str], trip_id: Optional[str]) -> Optional[int]:
        """Insert a boarding now; when the DB is busy queue it (see :meth:`retry_boardings`)."""
        return self._try_boarding(event, station, trip_id, tries=1)

    def retry_boardings(self) -> List[BoardingInfo]:
        """Retry queued boardings (call once per frame); returns the ones written now."""
        queued, self._queued = self._queued, []
        written = []
        for event, station, trip_id, tries in queued:
            if self._try_boarding(event, station, trip_id, tries=tries + 1) is not None:
                written.append(self.boardings[int(event.track_id)])
        return written

    @property
    def queued_boardings(self) -> int:
        return len(self._queued)

    def _try_boarding(self, event: EntryEvent, station, trip_id, *, tries: int) -> Optional[int]:
        if self.session_id is None:
            raise RuntimeError("create_session() must be called first")
        boarded_at = datetime.fromtimestamp(event.timestamp, tz=timezone.utc)
        try:
            boarding_id = insert_boarding(self.conn, session_id=self.session_id, track_id=int(event.track_id),
                                          boarded_at=boarded_at, direction=event.direction, station=station,
                                          trip_id=trip_id)
        except sqlite3.OperationalError as exc:
            if not is_busy_error(exc):
                raise
            if tries >= MAX_BOARDING_TRIES:
                raise DatabaseError(f"Could not record boarding for Track ID {event.track_id} after "
                                    f"{tries} attempts: {exc}") from exc
            self._queued.append((event, station, trip_id, tries))
            self._warn("Database busy; boarding for Track ID %s queued (%s)", event.track_id, exc)
            return None
        self.boardings.setdefault(int(event.track_id), BoardingInfo(boarding_id, int(event.track_id), boarded_at))
        return boarding_id

    def note_validation(self, boarding_id: int, outcome: str) -> None:
        """Mirror the validator's boarding precedence in memory (no DB query per frame)."""
        for info in self.boardings.values():
            if info.boarding_id == boarding_id:
                if info.status != "VALID" and outcome != "VERIFICATION_REQUIRED":
                    info.status = outcome
                return

    def pending_verifications(self, now: datetime, timeout: float) -> List[BoardingInfo]:
        """This session's boardings still VERIFICATION_REQUIRED after ``timeout`` seconds."""
        return [b for b in self.boardings.values()
                if b.status == "VERIFICATION_REQUIRED" and (now - b.boarded_at).total_seconds() >= timeout]

    def _warn(self, message: str, *args: Any) -> None:
        if time.monotonic() - self._last_warning >= _WARN_INTERVAL_SEC:
            self._last_warning = time.monotonic()
            logger.warning(message, *args)
