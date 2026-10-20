"""Validate a ticket from the command line (QR image, token or typed ticket ID).

    python scripts/validate_qr.py --image data/qr_codes/TKT-XXXX.png
    python scripts/validate_qr.py --token <token> | --ticket-id TKT-XXXX
        [--station "City Hospital"] [--boarding-id N] [--trip-id T] [--db PATH] [--config PATH]

Only the opaque token in the QR code is used; it is checked against the ticket
record in the database. Every run records one validation event (channel ``cli``).
The trip is, in order: ``--trip-id``, the selected boarding's trip, the trip of
the running detection session, else the configured/derived trip.
Exit codes: 0 VALID, 10 any other outcome, 1 usage/config error, unreadable
image, unknown boarding or database busy, 3 database error.
"""

from __future__ import annotations

import sqlite3
import sys
from typing import Optional, Sequence, Tuple

import _bootstrap  # noqa: F401  (puts the project root on sys.path)

from app.alert_service import outcome_label
from app.analytics import inherited_trip_id
from app.config import load_config, resolve_db_path
from app.database import Database, DatabaseError, utc_now
from app.passenger_service import is_busy_error
from app.qr_service import QRScanner
from app.settings_schema import ConfigError
from app.ticket_validator import TicketValidator, build_journey

EXIT_VALID = 0
EXIT_USAGE = 1
EXIT_DATABASE = 3
EXIT_NOT_VALID = 10


class CredentialError(Exception):
    """The credential could not be read (no / several QR codes, unreadable image)."""


def build_parser() -> _bootstrap.ArgumentParser:
    parser = _bootstrap.ArgumentParser(description="Validate a ticket against the database.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--image", help="QR code image (PNG/JPG)")
    source.add_argument("--token", help="QR token (with or without the TPD1: prefix)")
    source.add_argument("--ticket-id", help="ticket ID typed by the conductor")
    parser.add_argument("--station", help="current station (default: journey.current_station)")
    parser.add_argument("--boarding-id", type=int, help="boarding event to link the check to")
    parser.add_argument("--trip-id", help="trip ID (default: inherited, see above)")
    parser.add_argument("--db", help="database file (default: TPD_DB_PATH or database.path)")
    parser.add_argument("--config", help="config file (default: config/config.yaml or TPD_CONFIG)")
    return parser


def read_credential(args) -> Tuple[str, str]:
    """``(credential, credential_type)`` from --image / --token / --ticket-id."""
    if args.ticket_id is not None:
        return args.ticket_id, "ticket_id"
    if args.token is not None:
        return args.token, "qr_token"
    try:
        codes = QRScanner().decode_image(args.image)
    except ValueError as exc:
        raise CredentialError(f"{exc}: {args.image}") from None
    if not codes:
        raise CredentialError("No QR code found in the image")
    if len(codes) > 1:
        raise CredentialError("Several QR codes found; use an image with one QR code")
    return codes[0].payload, "qr_token"


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
        db_path = resolve_db_path(args.db, cfg)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    try:
        credential, credential_type = read_credential(args)
    except CredentialError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    try:
        database = Database(db_path, cfg.database.busy_timeout_ms)
        database.initialize()
        conn = database.connect()
    except (DatabaseError, sqlite3.Error, OSError) as exc:
        print(f"Database error ({db_path}): {exc}", file=sys.stderr)
        return EXIT_DATABASE
    try:
        now = utc_now()
        trip_id = args.trip_id or inherited_trip_id(conn, boarding_id=args.boarding_id, now=now,
                                                     stale_sec=cfg.runtime.heartbeat_stale_sec)
        journey = build_journey(cfg, now=now, station=args.station, trip_id=trip_id)
        result = TicketValidator(tz=cfg.app.timezone).validate(
            conn, credential, credential_type=credential_type, journey=journey, channel="cli",
            boarding_id=args.boarding_id, now=now)
    except ValueError as exc:  # station not on route, unknown boarding event
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except sqlite3.OperationalError as exc:
        if is_busy_error(exc):
            print("Database busy, try again", file=sys.stderr)
            return EXIT_USAGE
        print(f"Database error ({db_path}): {exc}", file=sys.stderr)
        return EXIT_DATABASE
    except sqlite3.Error as exc:
        print(f"Database error ({db_path}): {exc}", file=sys.stderr)
        return EXIT_DATABASE
    finally:
        conn.close()
    print(f"{outcome_label(result.outcome)} ({result.outcome.value}): {result.reason}")
    if result.ticket_id:
        print(f"Ticket ID: {result.ticket_id}")
    print(f"Trip: {journey.trip_id}  Station: {journey.current_station or '(not set)'}  "
          f"Validation #{result.validation_id}")
    return EXIT_VALID if result.is_valid else EXIT_NOT_VALID


if __name__ == "__main__":
    sys.exit(main())
