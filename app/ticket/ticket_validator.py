"""Validate QR ticket IDs against ``data/tickets.json``.

tickets.json format::

    {
      "tickets": [
        {
          "ticket_id": "TKT-0001",
          "status": "ACTIVE",                      # ACTIVE | USED | CANCELLED
          "valid_from": "2026-01-01T00:00:00+00:00",   # optional, ISO-8601
          "valid_until": "2027-01-01T00:00:00+00:00",  # optional, ISO-8601
          "ticket_type": "SINGLE"                  # optional, informational
        }
      ]
    }

A bare JSON list of ticket objects is also accepted. Naive timestamps are UTC.

Validation outcome (``TicketStatus``), checked in this order:

1. INVALID   - empty/malformed ticket ID.
2. NOT_FOUND - well-formed ID that is not in tickets.json.
3. INVALID   - ticket is CANCELLED, not yet valid, or expired.
4. USED      - ticket marked USED in tickets.json, already consumed by this
               validator (``mark_used``), or reported used by ``is_used``
               (e.g. already linked to another passenger event in the DB).
5. VALID     - otherwise.

The validator never mutates tickets.json.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Union

from app.database.models import TICKET_ID_MAX_LENGTH, TicketStatus

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TICKETS_PATH = PROJECT_ROOT / "data" / "tickets.json"

# Upper-case letters, digits, '-' and '_'; 3..64 chars; must start alphanumeric.
TICKET_ID_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,%d}$" % (TICKET_ID_MAX_LENGTH - 1))

RECORD_ACTIVE = "ACTIVE"
RECORD_USED = "USED"
RECORD_CANCELLED = "CANCELLED"
RECORD_STATUSES = frozenset({RECORD_ACTIVE, RECORD_USED, RECORD_CANCELLED})


def normalize_ticket_id(raw: Any) -> Optional[str]:
    """Return the canonical (stripped, upper-case) ticket ID, or None if malformed."""
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str):
        return None
    candidate = raw.strip().upper()
    return candidate if TICKET_ID_PATTERN.fullmatch(candidate) else None


def _parse_datetime(value: Any, field: str, ticket_id: str) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError(f"Ticket {ticket_id}: '{field}' must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Ticket {ticket_id}: invalid '{field}' timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _as_utc(value: Optional[datetime]) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class TicketRecord:
    ticket_id: str
    status: str = RECORD_ACTIVE
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    ticket_type: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any], index: int = 0) -> "TicketRecord":
        if not isinstance(data, dict):
            raise ValueError(f"Ticket entry #{index} must be an object")
        ticket_id = normalize_ticket_id(data.get("ticket_id"))
        if ticket_id is None:
            raise ValueError(f"Ticket entry #{index}: invalid ticket_id {data.get('ticket_id')!r}")

        status = str(data.get("status", RECORD_ACTIVE)).strip().upper()
        if status not in RECORD_STATUSES:
            raise ValueError(
                f"Ticket {ticket_id}: unknown status {status!r} "
                f"(expected one of {sorted(RECORD_STATUSES)})"
            )

        valid_from = _parse_datetime(data.get("valid_from"), "valid_from", ticket_id)
        valid_until = _parse_datetime(data.get("valid_until"), "valid_until", ticket_id)
        if valid_from and valid_until and valid_until < valid_from:
            raise ValueError(f"Ticket {ticket_id}: valid_until is before valid_from")

        ticket_type = data.get("ticket_type")
        return cls(
            ticket_id=ticket_id,
            status=status,
            valid_from=valid_from,
            valid_until=valid_until,
            ticket_type=str(ticket_type) if ticket_type is not None else None,
        )


@dataclass(frozen=True)
class ValidationResult:
    raw_value: Optional[str]
    ticket_id: Optional[str]  # normalized ID; None when malformed
    status: TicketStatus
    reason: str
    ticket: Optional[TicketRecord] = None

    @property
    def is_valid(self) -> bool:
        return self.status is TicketStatus.VALID


def load_ticket_records(path: Union[str, Path]) -> List[TicketRecord]:
    """Load and validate ticket records from a JSON file."""
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    return parse_ticket_records(payload)


def parse_ticket_records(payload: Any) -> List[TicketRecord]:
    if isinstance(payload, dict):
        entries = payload.get("tickets")
    else:
        entries = payload
    if not isinstance(entries, list):
        raise ValueError("tickets.json must be a list or an object with a 'tickets' list")
    return [TicketRecord.from_dict(entry, i) for i, entry in enumerate(entries)]


class TicketValidator:
    """Thread-safe validator for ticket IDs read from QR codes."""

    def __init__(
        self,
        tickets_path: Optional[Union[str, Path]] = None,
        records: Optional[Iterable[TicketRecord]] = None,
    ) -> None:
        self.tickets_path: Optional[Path] = None
        if records is None:
            self.tickets_path = Path(tickets_path) if tickets_path else DEFAULT_TICKETS_PATH
            records = load_ticket_records(self.tickets_path)
        self._lock = threading.Lock()
        self._used: set[str] = set()
        self._tickets: Dict[str, TicketRecord] = {}
        self._set_records(records)

    @classmethod
    def from_dicts(cls, tickets: Iterable[Dict[str, Any]]) -> "TicketValidator":
        return cls(records=parse_ticket_records(list(tickets)))

    def _set_records(self, records: Iterable[TicketRecord]) -> None:
        tickets: Dict[str, TicketRecord] = {}
        for record in records:
            if record.ticket_id in tickets:
                raise ValueError(f"Duplicate ticket_id {record.ticket_id!r}")
            tickets[record.ticket_id] = record
        self._tickets = tickets

    def reload(self) -> None:
        """Re-read tickets.json (keeps the in-memory 'used' set)."""
        if self.tickets_path is None:
            raise RuntimeError("Validator was built from in-memory records; nothing to reload")
        records = load_ticket_records(self.tickets_path)
        with self._lock:
            self._set_records(records)

    def __len__(self) -> int:
        return len(self._tickets)

    def __contains__(self, ticket_id: object) -> bool:
        return normalize_ticket_id(ticket_id) in self._tickets

    def get(self, ticket_id: Any) -> Optional[TicketRecord]:
        normalized = normalize_ticket_id(ticket_id)
        return self._tickets.get(normalized) if normalized else None

    def mark_used(self, ticket_id: Any) -> None:
        normalized = normalize_ticket_id(ticket_id)
        if normalized:
            with self._lock:
                self._used.add(normalized)

    def validate(
        self,
        raw_ticket_id: Any,
        *,
        now: Optional[datetime] = None,
        is_used: Optional[Callable[[str], bool]] = None,
        mark_used: bool = False,
    ) -> ValidationResult:
        """Validate a ticket ID.

        :param now: evaluation time (defaults to current UTC time).
        :param is_used: optional external check, e.g. "is this ticket already
            linked to another passenger event?".
        :param mark_used: consume the ticket in this validator if VALID, so the
            next validation of the same ID returns USED.
        """
        raw_value = raw_ticket_id.decode("utf-8", "replace") if isinstance(
            raw_ticket_id, bytes
        ) else (raw_ticket_id if isinstance(raw_ticket_id, str) else None)

        ticket_id = normalize_ticket_id(raw_ticket_id)
        if ticket_id is None:
            return ValidationResult(raw_value, None, TicketStatus.INVALID, "Malformed or empty ticket ID")

        record = self._tickets.get(ticket_id)
        if record is None:
            return ValidationResult(raw_value, ticket_id, TicketStatus.NOT_FOUND, "Ticket ID not in ticket list")

        if record.status == RECORD_CANCELLED:
            return ValidationResult(raw_value, ticket_id, TicketStatus.INVALID, "Ticket cancelled", record)

        current = _as_utc(now)
        if record.valid_from and current < record.valid_from:
            return ValidationResult(raw_value, ticket_id, TicketStatus.INVALID, "Ticket not yet valid", record)
        if record.valid_until and current > record.valid_until:
            return ValidationResult(raw_value, ticket_id, TicketStatus.INVALID, "Ticket expired", record)

        with self._lock:
            if record.status == RECORD_USED or ticket_id in self._used:
                return ValidationResult(raw_value, ticket_id, TicketStatus.USED, "Ticket already used", record)
            if is_used is not None and is_used(ticket_id):
                return ValidationResult(
                    raw_value, ticket_id, TicketStatus.USED, "Ticket already linked to another passenger", record
                )
            if mark_used:
                self._used.add(ticket_id)

        return ValidationResult(raw_value, ticket_id, TicketStatus.VALID, "Ticket valid", record)
