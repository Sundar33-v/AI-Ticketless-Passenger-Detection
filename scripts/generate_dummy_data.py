"""Write a deterministic, clearly marked demo dataset into the application database.

    python scripts/generate_dummy_data.py [--date YYYY-MM-DD] [--db PATH] [--config PATH]
        [--seed 20260115] [--qr-dir data/qr_codes/demo] [--no-qr-images]
    python scripts/generate_dummy_data.py --reset [--generate]

Every demo row has ``is_synthetic = 1`` and lives in the same database the
detection process and the dashboard use (there is no separate demo database).
Running the script twice changes nothing; ``--reset`` removes only demo rows.
When demo data for another day already exists, nothing is written and the
script names that day; ``--reset --generate`` moves the demo data to the new day.
This file contains no SQL: every write goes through the application's own
functions in ``app.ticket_validator`` and ``app.passenger_service``.

Expected dashboard values for the demo day D: Valid 7, Invalid 4, Total Passenger 16,
Total Detection 50, Expired Tickets 2, Boarding Events 15, Tracking IDs 16
(all time: 8 / 4 / 18 / 56 / 2 / 17 / 18).
Exit codes: 0 ok, 1 usage/config error or a failed reset, 3 database error.
"""

from __future__ import annotations

import random
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

import _bootstrap  # noqa: F401  (puts the project root on sys.path)

from app.analytics import synthetic_counts
from app.config import load_config, resolve_db_path, resolve_path
from app.database import Database, DatabaseError
from app.passenger_service import (DetectionRow, delete_synthetic, end_session_row, insert_boarding,
                                   insert_detections, insert_session, session_started_at)
from app.qr_service import demo_token, encode_payload, write_qr_png
from app.settings_schema import DEFAULT_STATIONS, AppConfig, ConfigError
from app.ticket_validator import TicketValidator, build_journey, cancel_ticket, get_ticket, issue_ticket

DEFAULT_SEED = 20260115
DEFAULT_QR_DIR = "data/qr_codes/demo"
DEMO_ROUTE = "R1"
DEMO_DIRECTION = "outbound"
DEMO_STATION = "City Hospital"
CENTRAL, MARKET, HOSPITAL, COLLEGE, RAILWAY, AIRPORT = DEFAULT_STATIONS
SESSION_TODAY, SESSION_YESTERDAY = "demo-session-01", "demo-session-00"
SOURCE_LABEL = "Demo data (synthetic)"


@dataclass(frozen=True)
class DemoTicket:
    number: int
    boarding: str
    destination: str
    day_offset: int  # travel date = D + day_offset
    valid_from: time
    expires_at: time

    @property
    def ticket_id(self) -> str:
        return f"DEMO-TKT-{self.number:03d}"

    @property
    def passenger_ref(self) -> str:
        return f"DEMO-PSG-{self.number:03d}"


_DAY = (time(5, 0), time(23, 0))
TICKETS = (
    DemoTicket(1, CENTRAL, AIRPORT, 0, *_DAY),       # VALID at an intermediate stop
    DemoTicket(2, MARKET, RAILWAY, 0, *_DAY),        # VALID
    DemoTicket(3, CENTRAL, COLLEGE, 0, *_DAY),       # VALID
    DemoTicket(4, HOSPITAL, AIRPORT, 0, *_DAY),      # VALID, boards at the current station
    DemoTicket(5, CENTRAL, HOSPITAL, 0, *_DAY),      # VALID, at destination
    DemoTicket(6, CENTRAL, AIRPORT, 0, *_DAY),       # cancelled -> INVALID
    DemoTicket(7, CENTRAL, AIRPORT, -1, time(6, 0), time(22, 0)),  # yesterday's ticket -> EXPIRED
    DemoTicket(8, CENTRAL, AIRPORT, 0, time(0, 0), time(6, 0)),    # expired this morning -> EXPIRED
    DemoTicket(9, CENTRAL, MARKET, 0, *_DAY),        # beyond destination -> WRONG_ROUTE
    DemoTicket(10, CENTRAL, AIRPORT, 0, *_DAY),      # VALID, then presented again -> ALREADY_USED
    DemoTicket(11, MARKET, AIRPORT, 0, *_DAY),       # VALID desk check (no boarding)
    DemoTicket(12, CENTRAL, AIRPORT, -1, *_DAY),     # VALID on D-1 (session 00)
)
CANCELLED_TICKET = "DEMO-TKT-006"
UNKNOWN_TICKET_ID = "DEMO-TKT-999"
# Track ID -> credential presented after boarding (session 01); tracks 13-15 present nothing.
SESSION_TODAY_CHECKS = {1: "DEMO-TKT-001", 2: "DEMO-TKT-002", 3: "DEMO-TKT-003", 4: "DEMO-TKT-004",
                        5: "DEMO-TKT-005", 6: "DEMO-TKT-006", 7: "DEMO-TKT-007", 8: "DEMO-TKT-008",
                        9: "DEMO-TKT-009", 10: "DEMO-TKT-010", 11: "DEMO-TKT-010", 12: UNKNOWN_TICKET_ID}


# ----------------------------------------------------------------- arguments
def build_parser() -> _bootstrap.ArgumentParser:
    parser = _bootstrap.ArgumentParser(description="Generate or remove the synthetic demo dataset.")
    parser.add_argument("--db", help="database file (default: TPD_DB_PATH or database.path)")
    parser.add_argument("--config", help="config file (default: config/config.yaml or TPD_CONFIG)")
    parser.add_argument("--date", type=date.fromisoformat, help="demo day D, YYYY-MM-DD (default: today)")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help=f"random seed (default {DEFAULT_SEED})")
    parser.add_argument("--reset", action="store_true", help="delete all synthetic rows (only those)")
    parser.add_argument("--generate", action="store_true", help="with --reset: generate again afterwards")
    parser.add_argument("--qr-dir", default=DEFAULT_QR_DIR, help=f"demo QR PNG folder (default {DEFAULT_QR_DIR})")
    parser.add_argument("--no-qr-images", action="store_true", help="do not write demo QR PNG files")
    return parser


def existing_demo_day(conn: sqlite3.Connection, tz: ZoneInfo) -> Optional[date]:
    """Local demo day D of the demo data already in the database (None when there is none)."""
    started = session_started_at(conn, SESSION_TODAY)
    return started.astimezone(tz).date() if started is not None else None


def config_mismatch(cfg: AppConfig) -> Optional[str]:
    """The demo outcomes assume route R1 outbound with the six default stations in order."""
    stations = [s for s in cfg.route.stations if s in DEFAULT_STATIONS]
    if (cfg.route.route_id == DEMO_ROUTE and tuple(stations) == DEFAULT_STATIONS
            and cfg.journey.direction == DEMO_DIRECTION):
        return None
    return (f"Demo data requires route {DEMO_ROUTE} {DEMO_DIRECTION} with the default stations; "
            f"current config: {cfg.route.route_id}/{cfg.journey.direction}")


# ------------------------------------------------------------------ dataset
class DemoWriter:
    """Writes the demo dataset for day ``day`` through the application functions."""

    def __init__(self, conn: sqlite3.Connection, cfg: AppConfig, day: date, seed: int) -> None:
        self.conn, self.cfg, self.day, self.seed = conn, cfg, day, seed
        self.tz = ZoneInfo(cfg.app.timezone)
        self.rng = random.Random(seed)
        self.validator = TicketValidator(tz=self.tz)
        self.direction = str(cfg.boarding.entry_line.get("entry_direction", "top_to_bottom"))

    def at(self, day_offset: int, hour: int, minute: int, second: int = 0) -> datetime:
        local_day = self.day + timedelta(days=day_offset)
        return datetime.combine(local_day, time(hour, minute, second), tzinfo=self.tz)

    def credential(self, ticket_id: str) -> str:
        return encode_payload(demo_token(self.seed, ticket_id))

    def trip_id(self, started_at: datetime, day_offset: int) -> str:
        local_day = self.day + timedelta(days=day_offset)
        return build_journey(self.cfg, now=started_at, station=DEMO_STATION,
                             trip_id=f"{DEMO_ROUTE}-{DEMO_DIRECTION}-{local_day:%Y-%m-%d}").trip_id

    # tickets ------------------------------------------------------------
    def write_tickets(self) -> None:
        for t in TICKETS:
            if get_ticket(self.conn, ticket_id=t.ticket_id) is not None:
                continue
            valid_from = self.at(t.day_offset, t.valid_from.hour, t.valid_from.minute)
            issue_ticket(self.conn, route=self.cfg.route, tz=self.tz, boarding_station=t.boarding,
                         destination_station=t.destination, travel_date=valid_from.date(), valid_from=valid_from,
                         expires_at=self.at(t.day_offset, t.expires_at.hour, t.expires_at.minute),
                         passenger_ref=t.passenger_ref, ticket_id=t.ticket_id,
                         token=demo_token(self.seed, t.ticket_id), is_synthetic=True, now=valid_from)
        cancelled = get_ticket(self.conn, ticket_id=CANCELLED_TICKET)
        if cancelled is not None and cancelled.status != "CANCELLED":
            cancel_ticket(self.conn, CANCELLED_TICKET, now=self.at(0, 5, 0))

    # sessions -----------------------------------------------------------
    def detections(self, session_id: str, track_id: int, first: datetime, samples: int) -> List[DetectionRow]:
        rows = []
        for k in range(samples):
            x1 = self.rng.uniform(80.0, 480.0)
            y1 = self.rng.uniform(40.0, 200.0)
            rows.append(DetectionRow(session_id, track_id, track_id * 1000 + k, first + timedelta(seconds=k),
                                     round(self.rng.uniform(0.55, 0.95), 3), round(x1, 1), round(y1, 1),
                                     round(x1 + self.rng.uniform(60.0, 110.0), 1),
                                     round(y1 + self.rng.uniform(180.0, 260.0), 1), is_synthetic=True))
        return rows

    def board(self, session_id: str, track_id: int, boarded_at: datetime, trip_id: str) -> int:
        insert_detections(self.conn, self.detections(session_id, track_id, boarded_at - timedelta(seconds=3), 3))
        return insert_boarding(self.conn, session_id=session_id, track_id=track_id, boarded_at=boarded_at,
                               direction=self.direction, station=DEMO_STATION, trip_id=trip_id, is_synthetic=True)

    def check(self, credential: Optional[str], credential_type: str, *, at: datetime, trip_id: str,
              event_key: str, session_id: Optional[str] = None, boarding_id: Optional[int] = None) -> None:
        journey = build_journey(self.cfg, now=at, station=DEMO_STATION, trip_id=trip_id)
        self.validator.validate(self.conn, credential, credential_type=credential_type, journey=journey,
                                channel="demo", boarding_id=boarding_id, session_id=session_id,
                                event_key=event_key, is_synthetic=True, now=at)

    def write_session_today(self) -> None:
        sid, started = SESSION_TODAY, self.at(0, 6, 59, 57)
        trip = self.trip_id(started, 0)
        insert_session(self.conn, session_id=sid, source_type="demo", source_label=SOURCE_LABEL, status="RUNNING",
                       started_at=started, station=DEMO_STATION, trip_id=trip, is_synthetic=True)
        for track in range(1, 16):
            boarded_at = self.at(0, 7, 0) + timedelta(minutes=10 * (track - 1))
            boarding_id = self.board(sid, track, boarded_at, trip)
            ticket = SESSION_TODAY_CHECKS.get(track)
            if ticket is None:
                continue  # tracks 13-15 stay "Ticket verification required"
            if ticket == UNKNOWN_TICKET_ID:
                credential, kind = ticket, "ticket_id"  # typed ticket ID that has no record
            else:
                credential, kind = self.credential(ticket), "qr_token"
            self.check(credential, kind, at=boarded_at + timedelta(minutes=2), trip_id=trip,
                       event_key=f"demo:{sid}:{track}", session_id=sid, boarding_id=boarding_id)
        insert_detections(self.conn, self.detections(sid, 16, self.at(0, 9, 40), 5))  # detected, never boards
        self.check("DEMO-TKT-011", "ticket_id", at=self.at(0, 9, 45), trip_id=trip, event_key="demo:desk:T011")
        end_session_row(self.conn, sid, status="STOPPED", ended_at=self.at(0, 9, 25))

    def write_session_yesterday(self) -> None:
        sid, started = SESSION_YESTERDAY, self.at(-1, 7, 4, 57)
        trip = self.trip_id(started, -1)
        insert_session(self.conn, session_id=sid, source_type="demo", source_label=SOURCE_LABEL, status="RUNNING",
                       started_at=started, station=DEMO_STATION, trip_id=trip, is_synthetic=True)
        first = self.board(sid, 1, self.at(-1, 7, 5), trip)
        self.check(self.credential("DEMO-TKT-012"), "qr_token", at=self.at(-1, 7, 10), trip_id=trip,
                   event_key=f"demo:{sid}:1", session_id=sid, boarding_id=first)
        self.board(sid, 2, self.at(-1, 7, 15), trip)
        end_session_row(self.conn, sid, status="STOPPED", ended_at=self.at(-1, 7, 20))

    def write_qr_images(self, qr_dir) -> int:
        for t in TICKETS:
            write_qr_png(self.credential(t.ticket_id), qr_dir / f"{t.ticket_id}.png")
        return len(TICKETS)

    def write_all(self) -> None:
        self.write_tickets()
        self.write_session_yesterday()
        self.write_session_today()


# --------------------------------------------------------------------- main
def _print_counts(title: str, counts: Dict[str, int]) -> None:
    print(title)
    for table, count in counts.items():
        print(f"  {table:<20} {count}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    generate = args.generate or not args.reset
    try:
        cfg = load_config(args.config)
        db_path = resolve_db_path(args.db, cfg)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    if generate:
        problem = config_mismatch(cfg)
        if problem:  # checked before the database is even opened: nothing is written
            print(problem, file=sys.stderr)
            return 1
    day = args.date or datetime.now(ZoneInfo(cfg.app.timezone)).date()
    try:
        database = Database(db_path, cfg.database.busy_timeout_ms)
        database.initialize()
        conn = database.connect()
    except (DatabaseError, sqlite3.Error, OSError) as exc:
        print(f"Database error ({db_path}): {exc}", file=sys.stderr)
        return 3
    try:
        print(f"Database: {db_path}")
        if args.reset:
            try:
                deleted = delete_synthetic(conn)
            except sqlite3.IntegrityError as exc:
                print(f"Reset failed, nothing was deleted: real records still reference demo passengers ({exc})",
                      file=sys.stderr)
                return 1
            _print_counts("Deleted synthetic rows:", deleted)
        if generate:
            writer = DemoWriter(conn, cfg, day, args.seed)
            existing = existing_demo_day(conn, writer.tz)
            if existing is not None and existing != day:  # the demo keys carry no date: never mix two days
                print(f"Demo data already exists for {existing:%Y-%m-%d} (and "
                      f"{existing - timedelta(days=1):%Y-%m-%d}); nothing was written for {day:%Y-%m-%d}.")
                print(f"To move the demo data to {day:%Y-%m-%d}, run: python scripts/generate_dummy_data.py "
                      f"--reset --generate --date {day:%Y-%m-%d}")
            else:
                writer.write_all()
                print(f"Demo data for {day:%Y-%m-%d} (and {day - timedelta(days=1):%Y-%m-%d}) is in place.")
            if not args.no_qr_images:
                qr_dir = resolve_path(args.qr_dir)
                print(f"Wrote {writer.write_qr_images(qr_dir)} demo QR PNGs to {qr_dir}")
        _print_counts("Synthetic rows per table:", synthetic_counts(conn))
        return 0
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print(f"Database error ({db_path}): {exc}", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"Error: could not write demo QR PNGs ({exc})", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
