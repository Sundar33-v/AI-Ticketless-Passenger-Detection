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

-- Labelled model evaluations (app/evaluation.py is the only writer). Separate from operational counters.
CREATE TABLE IF NOT EXISTS model_evaluations (
  evaluation_id    INTEGER PRIMARY KEY AUTOINCREMENT,
  evaluated_at     TEXT NOT NULL,
  method           TEXT NOT NULL CHECK (method IN ('images', 'mot', 'import')),
  model_name       TEXT NOT NULL CHECK (length(model_name) BETWEEN 1 AND 200),
  model_version    TEXT NOT NULL CHECK (length(model_version) BETWEEN 1 AND 200),
  dataset_name     TEXT NOT NULL CHECK (length(dataset_name) BETWEEN 1 AND 200),
  test_set_size    INTEGER NOT NULL CHECK (test_set_size >= 1),
  gt_boxes         INTEGER NOT NULL CHECK (gt_boxes >= 0),
  pred_boxes       INTEGER NOT NULL CHECK (pred_boxes >= 0),
  tp INTEGER NOT NULL CHECK (tp >= 0),
  fp INTEGER NOT NULL CHECK (fp >= 0),
  fn INTEGER NOT NULL CHECK (fn >= 0),
  img_tp INTEGER CHECK (img_tp IS NULL OR img_tp >= 0),
  img_fp INTEGER CHECK (img_fp IS NULL OR img_fp >= 0),
  img_fn INTEGER CHECK (img_fn IS NULL OR img_fn >= 0),
  img_tn INTEGER CHECK (img_tn IS NULL OR img_tn >= 0),
  accuracy  REAL CHECK (accuracy  IS NULL OR (accuracy  BETWEEN 0 AND 1)),
  precision REAL CHECK (precision IS NULL OR (precision BETWEEN 0 AND 1)),
  recall    REAL CHECK (recall    IS NULL OR (recall    BETWEEN 0 AND 1)),
  f1        REAL CHECK (f1        IS NULL OR (f1        BETWEEN 0 AND 1)),
  iou_threshold    REAL NOT NULL CHECK (iou_threshold > 0 AND iou_threshold <= 1),
  conf_threshold   REAL CHECK (conf_threshold IS NULL OR (conf_threshold > 0 AND conf_threshold < 1)),
  positive_class   TEXT NOT NULL DEFAULT 'person' CHECK (positive_class = 'person'),
  averaging        TEXT NOT NULL DEFAULT 'micro' CHECK (averaging = 'micro'),
  config_json      TEXT NOT NULL,
  notes            TEXT CHECK (notes IS NULL OR length(notes) <= 500),
  CHECK (length(config_json) <= 20000),
  CHECK (tp + fp = pred_boxes AND tp + fn = gt_boxes),
  CHECK ((img_tp IS NULL AND img_fp IS NULL AND img_fn IS NULL AND img_tn IS NULL)
      OR (img_tp IS NOT NULL AND img_fp IS NOT NULL AND img_fn IS NOT NULL AND img_tn IS NOT NULL
          AND img_tp + img_fp + img_fn + img_tn = test_set_size)));
CREATE INDEX IF NOT EXISTS idx_model_evaluations_at ON model_evaluations(evaluated_at);

-- Manual (dashboard-entered) evaluation runs. app/metric_evaluation.py is the only writer.
-- Separate from model_evaluations (pipeline results) and from all operational tables.
CREATE TABLE IF NOT EXISTS metric_evaluations (
  run_id              INTEGER PRIMARY KEY AUTOINCREMENT,
  evaluated_at        TEXT NOT NULL,
  mode                TEXT NOT NULL DEFAULT 'manual' CHECK (mode = 'manual'),
  tp INTEGER NOT NULL CHECK (tp BETWEEN 0 AND 10000000),
  tn INTEGER NOT NULL CHECK (tn BETWEEN 0 AND 10000000),
  fp INTEGER NOT NULL CHECK (fp BETWEEN 0 AND 10000000),
  fn INTEGER NOT NULL CHECK (fn BETWEEN 0 AND 10000000),
  accuracy_pct  REAL CHECK (accuracy_pct  IS NULL OR accuracy_pct  BETWEEN 0 AND 100),
  precision_pct REAL CHECK (precision_pct IS NULL OR precision_pct BETWEEN 0 AND 100),
  recall_pct    REAL CHECK (recall_pct    IS NULL OR recall_pct    BETWEEN 0 AND 100),
  f1_pct        REAL CHECK (f1_pct        IS NULL OR f1_pct        BETWEEN 0 AND 100),
  model_name          TEXT CHECK (model_name IS NULL OR length(model_name) BETWEEN 1 AND 200),
  model_version       TEXT CHECK (model_version IS NULL OR length(model_version) BETWEEN 1 AND 200),
  dataset_description TEXT NOT NULL CHECK (length(dataset_description) BETWEEN 1 AND 200),
  CHECK (tp + tn + fp + fn >= 1));
CREATE INDEX IF NOT EXISTS idx_metric_evaluations_at ON metric_evaluations(evaluated_at);

-- Conductor read status of derived alert groups (app/alert_reads.py is the only writer; the dashboard
-- calls it from dashboard/components/notifications.py). Notifications themselves are derived live from
-- validation_events / boarding_events / detection_sessions; this table stores only how far each group was read.
CREATE TABLE IF NOT EXISTS alert_reads (
  alert_key    TEXT PRIMARY KEY CHECK (length(alert_key) BETWEEN 1 AND 200),
  last_at_seen TEXT NOT NULL,            -- to_iso() of the group's last_at when it was marked read
  read_at      TEXT NOT NULL);           -- to_iso() wall time of the mark
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
