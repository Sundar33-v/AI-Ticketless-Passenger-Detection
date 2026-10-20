"""SQLite persistence layer (stdlib ``sqlite3``): schema, connections, transactions, timestamps.

One database file (``data/ticketless.db`` by default, see ``app.config.resolve_db_path``)
holds tickets, pseudonymous passenger references, detection sessions, detection
records, boarding events and ticket-validation events. The detection process,
the dashboard and every script use this module, so they all read the same data.

Rules:
  * Timestamps are TEXT ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` (UTC, fixed width so string
    comparison is chronological), produced only by :func:`to_iso`.
  * Every SQL statement in the project is parameterized.
  * Whoever opens a connection closes it; connections are never shared across threads.
  * :func:`transaction` refuses to nest, so a hidden inner commit is impossible.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Union

__all__ = [
    "DatabaseError", "Database", "SCHEMA", "SCHEMA_VERSION", "MEMORY_DB", "transaction",
    "utc_now", "to_iso", "parse_iso",
]

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MEMORY_DB = ":memory:"
_ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


class DatabaseError(Exception):
    """Fatal database problem (e.g. the file was created by a newer version)."""


# ------------------------------------------------------------------ timestamps
def utc_now() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(timezone.utc)


def to_iso(value: datetime) -> str:
    """Serialize an aware datetime as fixed-width UTC ``YYYY-MM-DDTHH:MM:SS.ffffffZ``.

    Naive datetimes are rejected (``ValueError``); aware ones are converted to UTC.
    """
    if not isinstance(value, datetime):
        raise TypeError(f"to_iso expects a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive datetime; pass a timezone-aware datetime")
    return value.astimezone(timezone.utc).strftime(_ISO_FORMAT)


def parse_iso(text: str) -> datetime:
    """Parse a stored timestamp back into an aware UTC datetime."""
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ---------------------------------------------------------------------- schema
SCHEMA = """
CREATE TABLE IF NOT EXISTS system_settings (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS passengers (
  passenger_ref TEXT PRIMARY KEY CHECK (length(passenger_ref) BETWEEN 4 AND 64),
  created_at TEXT NOT NULL,
  is_synthetic INTEGER NOT NULL DEFAULT 0 CHECK (is_synthetic IN (0,1)));

CREATE TABLE IF NOT EXISTS tickets (
  ticket_id TEXT PRIMARY KEY,
  passenger_ref TEXT NOT NULL REFERENCES passengers(passenger_ref) ON DELETE RESTRICT,
  route_id TEXT NOT NULL,
  boarding_station TEXT NOT NULL,
  destination_station TEXT NOT NULL,
  travel_date TEXT NOT NULL,
  valid_from TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','USED','CANCELLED')),
  qr_token_hash TEXT UNIQUE,                       -- sha256 hex; NULL = no QR issued
  used_at TEXT, used_trip_id TEXT,
  used_boarding_id INTEGER,                        -- no FK (avoids a tickets<->boarding cycle); app-owned
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  is_synthetic INTEGER NOT NULL DEFAULT 0 CHECK (is_synthetic IN (0,1)),
  CHECK (boarding_station <> destination_station),
  CHECK (expires_at > valid_from));
CREATE INDEX IF NOT EXISTS idx_tickets_passenger ON tickets(passenger_ref);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_tickets_travel_date ON tickets(travel_date);

CREATE TABLE IF NOT EXISTS detection_sessions (
  session_id TEXT PRIMARY KEY,                     -- uuid4 hex
  source_type TEXT NOT NULL CHECK (source_type IN ('phone','webcam','video','demo')),
  source_label TEXT NOT NULL,                      -- redacted URL / "webcam 0" / file name only
  status TEXT NOT NULL CHECK (status IN ('RUNNING','STOPPED','FAILED')),
  started_at TEXT NOT NULL, last_heartbeat_at TEXT NOT NULL, ended_at TEXT,
  frames_processed INTEGER NOT NULL DEFAULT 0, fps REAL,
  station TEXT, trip_id TEXT, last_error TEXT,     -- last_error truncated to 500 chars
  is_synthetic INTEGER NOT NULL DEFAULT 0 CHECK (is_synthetic IN (0,1)));
CREATE INDEX IF NOT EXISTS idx_sessions_started ON detection_sessions(started_at);

CREATE TABLE IF NOT EXISTS detection_records (
  detection_id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES detection_sessions(session_id) ON DELETE CASCADE,
  track_id INTEGER NOT NULL CHECK (track_id >= 0),
  frame_index INTEGER NOT NULL CHECK (frame_index >= 0),
  detected_at TEXT NOT NULL,
  confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  x1 REAL NOT NULL, y1 REAL NOT NULL, x2 REAL NOT NULL, y2 REAL NOT NULL,
  is_synthetic INTEGER NOT NULL DEFAULT 0 CHECK (is_synthetic IN (0,1)),
  UNIQUE (session_id, track_id, frame_index));
CREATE INDEX IF NOT EXISTS idx_detections_time ON detection_records(detected_at);
CREATE INDEX IF NOT EXISTS idx_detections_track ON detection_records(session_id, track_id);

CREATE TABLE IF NOT EXISTS boarding_events (
  boarding_id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES detection_sessions(session_id) ON DELETE CASCADE,
  track_id INTEGER NOT NULL CHECK (track_id >= 0),
  boarded_at TEXT NOT NULL,
  direction TEXT NOT NULL,
  station TEXT, trip_id TEXT,
  verification_status TEXT NOT NULL DEFAULT 'VERIFICATION_REQUIRED' CHECK (verification_status IN
    ('VALID','INVALID','EXPIRED','WRONG_ROUTE','ALREADY_USED','VERIFICATION_REQUIRED')),
  passenger_ref TEXT REFERENCES passengers(passenger_ref) ON DELETE SET NULL,  -- set ONLY on VALID
  ticket_id TEXT REFERENCES tickets(ticket_id) ON DELETE SET NULL,            -- set ONLY on VALID
  last_validation_id INTEGER,
  is_synthetic INTEGER NOT NULL DEFAULT 0 CHECK (is_synthetic IN (0,1)),
  UNIQUE (session_id, track_id));                  -- de-duplication invariant (B8)
CREATE INDEX IF NOT EXISTS idx_boarding_time ON boarding_events(boarded_at);
CREATE INDEX IF NOT EXISTS idx_boarding_status ON boarding_events(verification_status, boarded_at);

CREATE TABLE IF NOT EXISTS validation_events (
  validation_id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_key TEXT UNIQUE,                           -- deterministic for demo rows, NULL for live rows
  validated_at TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK (outcome IN
    ('VALID','INVALID','EXPIRED','WRONG_ROUTE','ALREADY_USED','VERIFICATION_REQUIRED')),
  reason TEXT NOT NULL,
  credential_type TEXT NOT NULL CHECK (credential_type IN ('qr_token','ticket_id','none')),
  token_hash TEXT,                                 -- sha256 of presented token/payload, never plaintext
  presented_ref TEXT,                              -- normalized ticket ID typed by conductor (only if well-formed)
  ticket_id TEXT REFERENCES tickets(ticket_id) ON DELETE SET NULL,   -- only if a record was found
  passenger_ref TEXT REFERENCES passengers(passenger_ref) ON DELETE SET NULL,  -- ticket owner, if found
  boarding_id INTEGER REFERENCES boarding_events(boarding_id) ON DELETE SET NULL,
  session_id TEXT REFERENCES detection_sessions(session_id) ON DELETE SET NULL,
  trip_id TEXT, station TEXT,
  channel TEXT NOT NULL CHECK (channel IN ('camera','dashboard','cli','demo')),
  is_synthetic INTEGER NOT NULL DEFAULT 0 CHECK (is_synthetic IN (0,1)));
CREATE INDEX IF NOT EXISTS idx_validation_time ON validation_events(validated_at);
CREATE INDEX IF NOT EXISTS idx_validation_outcome ON validation_events(outcome, validated_at);
CREATE INDEX IF NOT EXISTS idx_validation_ticket ON validation_events(ticket_id);
CREATE INDEX IF NOT EXISTS idx_validation_boarding ON validation_events(boarding_id);
"""


def _statements(script: str) -> List[str]:
    """Split a SQL script into statements (``;`` inside ``--`` comments is ignored)."""
    statements, buffer = [], ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        raise ValueError(f"incomplete SQL statement: {buffer.strip()[:60]}")
    return statements


# ---------------------------------------------------------------- transactions
@contextmanager
def transaction(conn: sqlite3.Connection, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
    """``BEGIN [IMMEDIATE]`` ... ``COMMIT``; ``ROLLBACK`` and re-raise on any exception.

    Refuses to nest (``RuntimeError``) so an inner block can never commit an
    outer one's work by accident. The connection must use ``isolation_level=None``.
    """
    if conn.in_transaction:
        raise RuntimeError("transaction already open on this connection")
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    else:
        conn.execute("COMMIT")


# -------------------------------------------------------------------- database
class Database:
    """Holds the DB path and settings only (no open connection), so it is safe to cache.

    ``connect()`` returns a new read-write connection; ``read_connection()`` a new
    read-only one. Callers close what they open.
    """

    def __init__(self, path: Union[str, Path], busy_timeout_ms: int = 5000) -> None:
        if str(path) == MEMORY_DB:
            self.path: Union[str, Path] = MEMORY_DB
        else:
            self.path = Path(path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.busy_timeout_ms = int(busy_timeout_ms)

    @property
    def is_memory(self) -> bool:
        return self.path == MEMORY_DB

    def __repr__(self) -> str:
        return f"Database({str(self.path)!r})"

    def _configure(self, conn: sqlite3.Connection) -> sqlite3.Connection:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms:d}")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def connect(self) -> sqlite3.Connection:
        """New read-write connection: WAL, synchronous NORMAL, FKs on, autocommit mode."""
        conn = sqlite3.connect(str(self.path), timeout=self.busy_timeout_ms / 1000.0,
                               isolation_level=None, check_same_thread=False)
        try:
            self._configure(conn)
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        except BaseException:
            conn.close()
            raise
        return conn

    def read_connection(self) -> sqlite3.Connection:
        """New read-only connection (``mode=ro``): any write raises ``sqlite3.OperationalError``.

        Only ``busy_timeout``, ``foreign_keys`` and the row factory are set; the
        journal mode is never touched. ``:memory:`` returns a normal connection.
        """
        if self.is_memory:
            return self.connect()
        uri = Path(self.path).resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=self.busy_timeout_ms / 1000.0,
                               isolation_level=None, check_same_thread=False)
        try:
            self._configure(conn)
        except BaseException:
            conn.close()
            raise
        return conn

    def initialize(self) -> None:
        """Create all tables/indexes (idempotent) and record the schema version.

        Raises :class:`DatabaseError` if the file was created by a newer version.
        """
        conn = self.connect()
        try:
            with transaction(conn):
                for statement in _statements(SCHEMA):
                    conn.execute(statement)
                row = conn.execute("SELECT value FROM system_settings WHERE key = ?",
                                   ("schema_version",)).fetchone()
                if row is not None:
                    try:
                        stored = int(row["value"])
                    except (TypeError, ValueError):
                        raise DatabaseError(f"Unreadable schema_version {row['value']!r} in {self.path}") from None
                    if stored > SCHEMA_VERSION:
                        raise DatabaseError(
                            f"DB created by a newer version (schema_version {stored} > {SCHEMA_VERSION}): "
                            f"{self.path}")
                now = to_iso(utc_now())
                conn.execute(
                    "INSERT INTO system_settings (key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at "
                    "WHERE system_settings.value <> excluded.value",
                    ("schema_version", str(SCHEMA_VERSION), now))
                conn.execute(
                    "INSERT OR IGNORE INTO system_settings (key, value, updated_at) VALUES (?, ?, ?)",
                    ("created_at", now, now))
        finally:
            conn.close()
        logger.debug("Database initialized: %s", self.path)
