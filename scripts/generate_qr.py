"""Issue a ticket with a QR code, or rotate the QR token of an existing ticket.

    python scripts/generate_qr.py --boarding "Central Bus Stand" --destination "Airport"
        [--travel-date YYYY-MM-DD] [--valid-hours 12] [--passenger-ref PSG-0001]
        [--out data/qr_codes] [--show-token] [--db PATH] [--config PATH]
    python scripts/generate_qr.py --ticket-id TKT-XXXX --rotate [--out data/qr_codes]

The QR code contains only an opaque random token (``TPD1:<token>``); the
database stores only its SHA-256 hash. The token itself is printed only with
``--show-token``. Rotating a token makes every earlier QR code of that ticket
stop working. Exit codes: 0 ok, 1 usage/config/validation error, 3 database error.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

import _bootstrap  # noqa: F401  (puts the project root on sys.path)

from app.config import load_config, resolve_db_path, resolve_path
from app.database import Database, DatabaseError, utc_now
from app.qr_service import encode_payload, write_qr_png
from app.settings_schema import ConfigError
from app.ticket_validator import get_ticket, issue_ticket, normalize_ticket_id, rotate_token

DEFAULT_OUT_DIR = "data/qr_codes"
# Pseudonymous reference only (no spaces, so a real name is not accepted by accident).
PASSENGER_REF_PATTERN = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


def build_parser() -> _bootstrap.ArgumentParser:
    parser = _bootstrap.ArgumentParser(description="Issue a QR ticket or rotate a ticket's QR token.")
    parser.add_argument("--boarding", help="boarding station (issue mode)")
    parser.add_argument("--destination", help="destination station (issue mode)")
    parser.add_argument("--travel-date", help="local travel date YYYY-MM-DD (default: today)")
    parser.add_argument("--valid-hours", type=float, default=12.0, help="validity window in hours (default 12)")
    parser.add_argument("--passenger-ref", help="pseudonymous passenger ID such as PSG-0001 (never a name)")
    parser.add_argument("--ticket-id", help="existing ticket ID (rotate mode)")
    parser.add_argument("--rotate", action="store_true", help="replace the QR token of --ticket-id")
    parser.add_argument("--out", default=DEFAULT_OUT_DIR, help=f"PNG output folder (default {DEFAULT_OUT_DIR})")
    parser.add_argument("--show-token", action="store_true", help="also print the plaintext token")
    parser.add_argument("--db", help="database file (default: TPD_DB_PATH or database.path)")
    parser.add_argument("--config", help="config file (default: config/config.yaml or TPD_CONFIG)")
    return parser


def _check_mode(parser: _bootstrap.ArgumentParser, args) -> None:
    if args.rotate:
        if not args.ticket_id:
            parser.error("--rotate needs --ticket-id")
        if args.boarding or args.destination or args.travel_date or args.passenger_ref:
            parser.error("--rotate cannot be combined with issue options")
        return
    if args.ticket_id:
        parser.error("--ticket-id is only used with --rotate")
    if not args.boarding or not args.destination:
        parser.error("issue mode needs --boarding and --destination")
    if args.passenger_ref and not PASSENGER_REF_PATTERN.fullmatch(args.passenger_ref):
        parser.error("--passenger-ref must be 4-64 letters, digits, '-' or '_' (a pseudonymous ID, not a name)")


def _print_result(ticket_id: str, passenger_ref: str, png: Path, token: str, show_token: bool) -> None:
    print(f"Ticket ID: {ticket_id}")
    print(f"Passenger ref: {passenger_ref}")
    print(f"QR PNG: {png}")
    if show_token:
        print(f"Token: {token}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _check_mode(parser, args)
    try:
        cfg = load_config(args.config)
        db_path = resolve_db_path(args.db, cfg)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    out_dir = resolve_path(args.out)
    try:
        database = Database(db_path, cfg.database.busy_timeout_ms)
        database.initialize()
        conn = database.connect()
    except (DatabaseError, sqlite3.Error, OSError) as exc:
        print(f"Database error ({db_path}): {exc}", file=sys.stderr)
        return 3
    try:
        if args.rotate:
            ticket_id = normalize_ticket_id(args.ticket_id) or args.ticket_id
            token = rotate_token(conn, ticket_id)
            record = get_ticket(conn, ticket_id=ticket_id)
            assert record is not None
            print("QR token rotated; earlier QR codes of this ticket no longer validate.")
        else:
            tz = ZoneInfo(cfg.app.timezone)
            travel_date = args.travel_date or datetime.now(tz).date()
            record, token = issue_ticket(
                conn, route=cfg.route, tz=tz, boarding_station=args.boarding,
                destination_station=args.destination, travel_date=travel_date, valid_hours=args.valid_hours,
                passenger_ref=args.passenger_ref, now=utc_now())
            print(f"Ticket issued: {record.boarding_station} → {record.destination_station}, "
                  f"travel date {record.travel_date}")
        png = write_qr_png(encode_payload(token), out_dir / f"{record.ticket_id}.png")
        _print_result(record.ticket_id, record.passenger_ref, png, token, args.show_token)
        return 0
    except (ValueError, LookupError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except sqlite3.IntegrityError as exc:
        print(f"Error: ticket could not be stored ({exc})", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print(f"Database error ({db_path}): {exc}", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"Error: could not write the QR PNG ({exc})", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
