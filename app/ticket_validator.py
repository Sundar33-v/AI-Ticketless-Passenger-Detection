"""Ticket management and validation against SQLite records.

The validator never labels a person as travelling without a ticket. When no
credential has been presented, the outcome is ``VERIFICATION_REQUIRED``.
A QR code carries only an opaque token; the ticket record in the database is
the only source of truth (QR content is never trusted).

Rules (first match wins, see ``evaluate_ticket``):
  1. no credential                      -> VERIFICATION_REQUIRED
  2. malformed credential               -> INVALID
  3. no matching ticket record          -> INVALID
  4. ticket CANCELLED                   -> INVALID
  5. now >= expires_at                  -> EXPIRED  (expiry never depends on station position)
  6. now < valid_from                   -> INVALID
  7. route / station checks             -> WRONG_ROUTE (intermediate stops are valid: onward travel)
  8. USED and not a same-journey check  -> ALREADY_USED
  9. otherwise                          -> VALID

All ticket, passenger-reference and validation-event SQL lives in this module.
"""

from __future__ import annotations

import logging
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from enum import Enum
from typing import Any, Optional, Tuple, Union
from zoneinfo import ZoneInfo

from app.database import parse_iso, to_iso, transaction, utc_now
from app.qr_service import hash_token, new_token, parse_payload
from app.settings_schema import AppConfig, RouteConfig

logger = logging.getLogger(__name__)

TICKET_ID_MAX_LENGTH = 64
TICKET_ID_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,%d}$" % (TICKET_ID_MAX_LENGTH - 1))
CREDENTIAL_TYPES = ("qr_token", "ticket_id", "none")
CHANNELS = ("camera", "dashboard", "cli", "demo")
MAX_VALID_HOURS = 72.0


class Outcome(str, Enum):
    VALID = "VALID"
    INVALID = "INVALID"
    EXPIRED = "EXPIRED"
    WRONG_ROUTE = "WRONG_ROUTE"
    ALREADY_USED = "ALREADY_USED"
    VERIFICATION_REQUIRED = "VERIFICATION_REQUIRED"


def normalize_ticket_id(raw: Any) -> Optional[str]:
    """Canonical (stripped, upper-case) ticket ID, or None if empty/malformed."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str):
        return None
    candidate = raw.strip().upper()
    return candidate if TICKET_ID_PATTERN.fullmatch(candidate) else None


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _zone(tz: Union[ZoneInfo, str]) -> ZoneInfo:
    return tz if isinstance(tz, ZoneInfo) else ZoneInfo(str(tz))


# ---------------------------------------------------------------- records
@dataclass(frozen=True)
class TicketRecord:
    ticket_id: str
    passenger_ref: str
    route_id: str
    boarding_station: str
    destination_station: str
    travel_date: str  # local calendar date YYYY-MM-DD (informational)
    valid_from: datetime
    expires_at: datetime
    status: str  # ACTIVE | USED | CANCELLED
    qr_token_hash: Optional[str] = None
    used_at: Optional[datetime] = None
    used_trip_id: Optional[str] = None
    used_boarding_id: Optional[int] = None
    is_synthetic: bool = False

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TicketRecord":
        return cls(
            ticket_id=row["ticket_id"], passenger_ref=row["passenger_ref"], route_id=row["route_id"],
            boarding_station=row["boarding_station"], destination_station=row["destination_station"],
            travel_date=row["travel_date"], valid_from=parse_iso(row["valid_from"]),
            expires_at=parse_iso(row["expires_at"]), status=row["status"], qr_token_hash=row["qr_token_hash"],
            used_at=parse_iso(row["used_at"]) if row["used_at"] else None, used_trip_id=row["used_trip_id"],
            used_boarding_id=row["used_boarding_id"], is_synthetic=bool(row["is_synthetic"]),
        )


@dataclass(frozen=True)
class JourneyContext:
    """The bus journey a ticket is checked against. Build it with :func:`build_journey`."""

    route_id: str
    direction: str
    ordered_stations: Tuple[str, ...]  # already reversed for inbound journeys
    current_station: Optional[str]
    trip_id: str


def build_journey(cfg: AppConfig, *, now: datetime, station: Optional[str] = None,
                  trip_id: Optional[str] = None) -> JourneyContext:
    """The only constructor of :class:`JourneyContext` outside tests.

    ``station`` defaults to ``journey.current_station`` (None skips positional
    checks); ``trip_id`` defaults to ``journey.trip_id``, else
    ``<route_id>-<direction>-<local YYYY-MM-DD of now>``.
    """
    now = _aware(now, "now")
    route, journey = cfg.route, cfg.journey
    stations = tuple(route.stations)
    if journey.direction == "inbound":
        stations = tuple(reversed(stations))
    current = station.strip() if isinstance(station, str) and station.strip() else journey.current_station
    if current is not None and current not in stations:
        raise ValueError(f"Station '{current}' is not on route {route.route_id}")
    if not trip_id:
        trip_id = journey.trip_id
    if not trip_id:
        local_day = now.astimezone(ZoneInfo(cfg.app.timezone)).date()
        trip_id = f"{route.route_id}-{journey.direction}-{local_day:%Y-%m-%d}"
    return JourneyContext(route.route_id, journey.direction, stations, current, str(trip_id))


# ------------------------------------------------------------- evaluation
UNRECOGNISED_QR_REASON = "Unrecognised QR code"
MALFORMED_TICKET_REASON = "Malformed ticket ID"
NO_RECORD_QR_REASON = "No ticket record matches this QR code"  # produced by the f-string in evaluate_ticket


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    reason: str
    bind: bool = False  # mark the ticket USED / attach it to the boarding


def _local(value: datetime, tz: ZoneInfo) -> str:
    return value.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def evaluate_ticket(ticket: Optional[TicketRecord], journey: JourneyContext, now: datetime, *,
                    credential_present: bool, credential_wellformed: bool, boarding_id: Optional[int],
                    tz: ZoneInfo, credential_type: str = "qr_token") -> Decision:
    """Pure rule evaluation (no I/O, no clock); see the module docstring for the order."""
    label = "ticket ID" if credential_type == "ticket_id" else "QR code"
    if not credential_present:
        return Decision(Outcome.VERIFICATION_REQUIRED, "No ticket presented; ticket verification required")
    if not credential_wellformed:
        reason = MALFORMED_TICKET_REASON if credential_type == "ticket_id" else UNRECOGNISED_QR_REASON
        return Decision(Outcome.INVALID, reason)
    if ticket is None:
        return Decision(Outcome.INVALID, f"No ticket record matches this {label}")
    if ticket.status == "CANCELLED":
        return Decision(Outcome.INVALID, "Ticket cancelled")
    if now >= ticket.expires_at:
        return Decision(Outcome.EXPIRED, f"Ticket expired at {_local(ticket.expires_at, tz)}")
    if now < ticket.valid_from:
        return Decision(Outcome.INVALID, f"Ticket not valid yet (valid from {_local(ticket.valid_from, tz)})")

    stations = journey.ordered_stations
    if ticket.route_id != journey.route_id:
        return Decision(Outcome.WRONG_ROUTE, f"Ticket is for route {ticket.route_id}")
    current = journey.current_station
    if (ticket.boarding_station not in stations or ticket.destination_station not in stations
            or (current is not None and current not in stations)):
        return Decision(Outcome.WRONG_ROUTE, "Station not on this route")
    i_b, i_d = stations.index(ticket.boarding_station), stations.index(ticket.destination_station)
    if i_b >= i_d:
        return Decision(Outcome.WRONG_ROUTE, "Ticket is for the opposite direction")
    at_destination = False
    if current is not None:
        i_c = stations.index(current)
        if i_c < i_b:
            return Decision(Outcome.WRONG_ROUTE, "Bus has not reached the ticket's boarding station")
        if i_c > i_d:
            return Decision(Outcome.WRONG_ROUTE, "Passenger has travelled beyond the ticket destination")
        at_destination = i_c == i_d  # i(B) <= i(C) < i(D) is onward travel: valid

    if ticket.status == "USED":
        recheck = ticket.used_trip_id == journey.trip_id and (
            boarding_id is None or ticket.used_boarding_id is None or ticket.used_boarding_id == boarding_id)
        if not recheck:
            when = _local(ticket.used_at, tz) if ticket.used_at else "an earlier time"
            return Decision(Outcome.ALREADY_USED, f"Ticket already used on trip {ticket.used_trip_id} at {when}")
        bind = ticket.used_boarding_id is None and boarding_id is not None
        return Decision(Outcome.VALID, "Re-check: already validated on this trip", bind)
    reason = ("At destination (alighting)" if at_destination
              else f"Valid for {ticket.boarding_station} → {ticket.destination_station}")
    return Decision(Outcome.VALID, reason, True)


# -------------------------------------------------------------- queries
def get_ticket(conn: sqlite3.Connection, *, ticket_id: Optional[str] = None,
               token_hash: Optional[str] = None) -> Optional[TicketRecord]:
    """Look a ticket up by ID or by QR token hash (exactly one key)."""
    if (ticket_id is None) == (token_hash is None):
        raise ValueError("get_ticket needs exactly one of ticket_id or token_hash")
    if ticket_id is not None:
        normalized = normalize_ticket_id(ticket_id)
        if normalized is None:
            return None
        row = conn.execute("SELECT * FROM tickets WHERE ticket_id = ?", (normalized,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM tickets WHERE qr_token_hash = ?", (token_hash,)).fetchone()
    return TicketRecord.from_row(row) if row else None


def _clock_value(now: Optional[datetime]) -> datetime:
    return utc_now() if now is None else _aware(now, "now")


def issue_ticket(conn: sqlite3.Connection, *, route: RouteConfig, tz: Union[ZoneInfo, str],
                 boarding_station: str, destination_station: str, travel_date: Union[date, str],
                 valid_from: Optional[datetime] = None, expires_at: Optional[datetime] = None,
                 valid_hours: float = 12.0, passenger_ref: Optional[str] = None,
                 ticket_id: Optional[str] = None, token: Optional[str] = None, is_synthetic: bool = False,
                 now: Optional[datetime] = None) -> Tuple[TicketRecord, str]:
    """Create a ticket (and its pseudonymous passenger reference) and return ``(record, token)``.

    Only ``sha256(token)`` is stored. Default window: ``valid_from = max(now, local
    midnight of travel_date)``, ``expires_at = valid_from + valid_hours``.
    """
    tz = _zone(tz)
    now_utc = _clock_value(now)
    if isinstance(travel_date, str):
        travel_date = date.fromisoformat(travel_date)
    if not 0 < float(valid_hours) <= MAX_VALID_HOURS:
        raise ValueError(f"valid_hours must be in (0, {MAX_VALID_HOURS:g}], got {valid_hours}")
    for name, station in (("boarding_station", boarding_station), ("destination_station", destination_station)):
        if station not in route.stations:
            raise ValueError(f"{name} '{station}' is not on route {route.route_id}")
    if boarding_station == destination_station:
        raise ValueError("boarding_station and destination_station must differ")

    if valid_from is None:
        midnight = datetime.combine(travel_date, dtime(0, 0), tzinfo=tz).astimezone(timezone.utc)
        valid_from = max(now_utc, midnight)
    valid_from = _aware(valid_from, "valid_from")
    expires_at = (valid_from + timedelta(hours=float(valid_hours)) if expires_at is None
                  else _aware(expires_at, "expires_at"))
    if expires_at <= valid_from:
        raise ValueError("expires_at must be later than valid_from")
    if valid_from.astimezone(tz).date() != travel_date:
        raise ValueError(f"valid_from {_local(valid_from, tz)} is not on travel_date {travel_date:%Y-%m-%d}")

    if ticket_id is None:
        ticket_id = "TKT-" + secrets.token_hex(4).upper()
    elif normalize_ticket_id(ticket_id) is None:
        raise ValueError(f"Malformed ticket ID {ticket_id!r}")
    ticket_id = normalize_ticket_id(ticket_id)
    passenger_ref = passenger_ref or "PSG-" + secrets.token_hex(5).upper()
    token = token or new_token()
    stamp = to_iso(now_utc)
    with transaction(conn):
        conn.execute("INSERT OR IGNORE INTO passengers (passenger_ref, created_at, is_synthetic) VALUES (?, ?, ?)",
                     (passenger_ref, stamp, int(is_synthetic)))
        conn.execute(
            "INSERT INTO tickets (ticket_id, passenger_ref, route_id, boarding_station, destination_station, "
            "travel_date, valid_from, expires_at, status, qr_token_hash, created_at, updated_at, is_synthetic) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?)",
            (ticket_id, passenger_ref, route.route_id, boarding_station, destination_station,
             f"{travel_date:%Y-%m-%d}", to_iso(valid_from), to_iso(expires_at), hash_token(token),
             stamp, stamp, int(is_synthetic)))
        record = get_ticket(conn, ticket_id=ticket_id)
    assert record is not None
    return record, token


def _require_ticket(conn: sqlite3.Connection, ticket_id: str) -> TicketRecord:
    record = get_ticket(conn, ticket_id=ticket_id)
    if record is None:
        raise LookupError(f"Unknown ticket {ticket_id}")
    return record


def cancel_ticket(conn: sqlite3.Connection, ticket_id: str, *, now: Optional[datetime] = None) -> TicketRecord:
    """ACTIVE -> CANCELLED (the only way); CANCELLED is a no-op; USED -> ValueError; unknown -> LookupError."""
    normalized = normalize_ticket_id(ticket_id)
    if normalized is None:
        raise ValueError(f"Malformed ticket ID {ticket_id!r}")
    stamp = to_iso(_clock_value(now))
    with transaction(conn):
        record = _require_ticket(conn, normalized)
        if record.status == "CANCELLED":
            return record
        if record.status == "USED":
            raise ValueError("Cannot cancel a used ticket")
        conn.execute("UPDATE tickets SET status = 'CANCELLED', updated_at = ? WHERE ticket_id = ? "
                     "AND status = 'ACTIVE'", (stamp, normalized))
        return _require_ticket(conn, normalized)


def rotate_token(conn: sqlite3.Connection, ticket_id: str, *, now: Optional[datetime] = None) -> str:
    """Replace the ticket's QR token (old QR codes stop working); returns the new token."""
    normalized = normalize_ticket_id(ticket_id)
    if normalized is None:
        raise ValueError(f"Malformed ticket ID {ticket_id!r}")
    token = new_token()
    with transaction(conn):
        _require_ticket(conn, normalized)
        conn.execute("UPDATE tickets SET qr_token_hash = ?, updated_at = ? WHERE ticket_id = ?",
                     (hash_token(token), to_iso(_clock_value(now)), normalized))
    return token


# -------------------------------------------------------------- validation
@dataclass(frozen=True)
class ValidationResult:
    outcome: Outcome
    reason: str
    ticket_id: Optional[str] = None
    passenger_ref: Optional[str] = None
    validation_id: Optional[int] = None
    boarding_id: Optional[int] = None

    @property
    def is_valid(self) -> bool:
        return self.outcome is Outcome.VALID


@dataclass(frozen=True)
class _Credential:
    present: bool
    wellformed: bool
    credential_type: str
    token_hash: Optional[str] = None
    presented_ref: Optional[str] = None


def _parse_credential(credential: Optional[str], credential_type: str) -> _Credential:
    if credential_type not in CREDENTIAL_TYPES:
        raise ValueError(f"credential_type must be one of {', '.join(CREDENTIAL_TYPES)}")
    if credential_type == "none" or credential is None or not str(credential).strip():
        return _Credential(False, False, "none")
    text = str(credential)
    if credential_type == "qr_token":
        parsed = parse_payload(text)
        return _Credential(True, parsed.wellformed, "qr_token", parsed.token_hash)
    normalized = normalize_ticket_id(text)
    return _Credential(True, normalized is not None, "ticket_id", hash_token(text.strip().upper()), normalized)


class TicketValidator:
    """Validates credentials on a caller-owned connection, in one IMMEDIATE transaction."""

    def __init__(self, *, tz: Union[ZoneInfo, str], clock=utc_now) -> None:
        self.tz = _zone(tz)
        self._clock = clock

    def validate(self, conn: sqlite3.Connection, credential: Optional[str], *, credential_type: str,
                 journey: JourneyContext, channel: str, boarding_id: Optional[int] = None,
                 session_id: Optional[str] = None, event_key: Optional[str] = None, is_synthetic: bool = False,
                 now: Optional[datetime] = None) -> ValidationResult:
        if channel not in CHANNELS:
            raise ValueError(f"channel must be one of {', '.join(CHANNELS)}")
        cred = _parse_credential(credential, credential_type)
        now = _aware(now, "now") if now is not None else _aware(self._clock(), "clock()")

        with transaction(conn, immediate=True):
            if event_key is not None:
                row = conn.execute("SELECT * FROM validation_events WHERE event_key = ?", (event_key,)).fetchone()
                if row is not None:
                    return _result_from_row(row)
            boarding = None
            if boarding_id is not None:
                boarding = conn.execute("SELECT verification_status FROM boarding_events WHERE boarding_id = ?",
                                        (boarding_id,)).fetchone()
                if boarding is None:
                    raise ValueError("Unknown boarding event")
            ticket, decision = self._decide(conn, cred, journey, now, boarding_id)
            if decision.bind and ticket is not None and not self._bind(conn, ticket, journey, now, boarding_id):
                ticket, decision = self._decide(conn, cred, journey, now, boarding_id)  # state changed: once more
                if decision.bind and ticket is not None:
                    self._bind(conn, ticket, journey, now, boarding_id)
            cur = conn.execute(
                "INSERT INTO validation_events (event_key, validated_at, outcome, reason, credential_type, "
                "token_hash, presented_ref, ticket_id, passenger_ref, boarding_id, session_id, trip_id, station, "
                "channel, is_synthetic) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event_key, to_iso(now), decision.outcome.value, decision.reason, cred.credential_type,
                 cred.token_hash, cred.presented_ref, ticket.ticket_id if ticket else None,
                 ticket.passenger_ref if ticket else None, boarding_id, session_id, journey.trip_id,
                 journey.current_station, channel, int(is_synthetic)))
            validation_id = int(cur.lastrowid)
            if boarding is not None:
                self._update_boarding(conn, boarding_id, boarding["verification_status"], decision, ticket,
                                      validation_id)
        valid = decision.outcome is Outcome.VALID
        return ValidationResult(decision.outcome, decision.reason, ticket.ticket_id if ticket else None,
                                ticket.passenger_ref if ticket and valid else None, validation_id, boarding_id)

    def _decide(self, conn, cred: _Credential, journey, now, boarding_id) -> Tuple[Optional[TicketRecord], Decision]:
        ticket = None
        if cred.present and cred.wellformed:
            if cred.credential_type == "ticket_id":
                ticket = get_ticket(conn, ticket_id=cred.presented_ref)
            else:
                ticket = get_ticket(conn, token_hash=cred.token_hash)
        decision = evaluate_ticket(ticket, journey, now, credential_present=cred.present,
                                   credential_wellformed=cred.wellformed, boarding_id=boarding_id, tz=self.tz,
                                   credential_type=cred.credential_type)
        return ticket, decision

    @staticmethod
    def _bind(conn, ticket: TicketRecord, journey: JourneyContext, now: datetime, boarding_id) -> bool:
        stamp = to_iso(now)
        cur = conn.execute(
            "UPDATE tickets SET status = 'USED', used_at = COALESCE(used_at, ?), "
            "used_trip_id = COALESCE(used_trip_id, ?), used_boarding_id = ?, updated_at = ? "
            "WHERE ticket_id = ? AND (status = 'ACTIVE' OR (status = 'USED' AND used_boarding_id IS NULL))",
            (stamp, journey.trip_id, boarding_id, stamp, ticket.ticket_id))
        return cur.rowcount > 0

    @staticmethod
    def _update_boarding(conn, boarding_id: int, current: str, decision: Decision,
                         ticket: Optional[TicketRecord], validation_id: int) -> None:
        """M3 precedence: VALID is never overwritten; VERIFICATION_REQUIRED only records the
        validation id; any other outcome overwrites a non-VALID status."""
        if current == Outcome.VALID.value or decision.outcome is Outcome.VERIFICATION_REQUIRED:
            if current == Outcome.VALID.value and decision.outcome is not Outcome.VALID:
                logger.warning("Boarding #%s stays VALID; later check returned %s (%s)",
                               boarding_id, decision.outcome.value, decision.reason)
            conn.execute("UPDATE boarding_events SET last_validation_id = ? WHERE boarding_id = ?",
                         (validation_id, boarding_id))
        elif decision.outcome is Outcome.VALID:
            assert ticket is not None
            conn.execute("UPDATE boarding_events SET verification_status = 'VALID', last_validation_id = ?, "
                         "passenger_ref = ?, ticket_id = ? WHERE boarding_id = ?",
                         (validation_id, ticket.passenger_ref, ticket.ticket_id, boarding_id))
        else:
            conn.execute("UPDATE boarding_events SET verification_status = ?, last_validation_id = ? "
                         "WHERE boarding_id = ?", (decision.outcome.value, validation_id, boarding_id))


def _result_from_row(row: sqlite3.Row) -> ValidationResult:
    outcome = Outcome(row["outcome"])
    return ValidationResult(outcome, row["reason"], row["ticket_id"],
                            row["passenger_ref"] if outcome is Outcome.VALID else None,
                            int(row["validation_id"]), row["boarding_id"])
