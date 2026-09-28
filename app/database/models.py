"""ORM models and status enums for passenger events.

Status semantics
----------------
TicketStatus is the outcome of the most recent QR ticket validation for an event
(``None`` = no ticket scanned yet).

PassengerStatus is the operational state of the passenger:

* PENDING      - entered, no verified ticket yet (the default; NOT an accusation).
* VERIFIED     - a VALID ticket has been linked to this event.
* NEEDS_CHECK  - a scan failed or the passenger exited without a verified ticket.
                 This only flags the event for a human/conductor check; camera
                 detection alone never proves that someone is ticketless.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import CheckConstraint, DateTime, Enum as SAEnum, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.database import Base

TICKET_ID_MAX_LENGTH = 64


class TicketStatus(str, enum.Enum):
    VALID = "VALID"
    INVALID = "INVALID"
    USED = "USED"
    NOT_FOUND = "NOT_FOUND"


class PassengerStatus(str, enum.Enum):
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    NEEDS_CHECK = "NEEDS_CHECK"


def utcnow() -> datetime:
    """Naive UTC timestamp (SQLite has no timezone storage; all times are UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _enum_column(enum_cls: type[enum.Enum], name: str) -> SAEnum:
    # Stored as VARCHAR + CHECK constraint (portable, readable in the sqlite shell).
    return SAEnum(enum_cls, name=name, native_enum=False, create_constraint=True, length=16)


class PassengerEvent(Base):
    """One passenger entry/exit event tied to a tracker ID."""

    __tablename__ = "passenger_events"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Tracker IDs can be reused across sessions/restarts, so not unique.
    track_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    entry_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    exit_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    ticket_id: Mapped[Optional[str]] = mapped_column(
        String(TICKET_ID_MAX_LENGTH), nullable=True, index=True
    )
    ticket_status: Mapped[Optional[TicketStatus]] = mapped_column(
        _enum_column(TicketStatus, "ticket_status"), nullable=True
    )
    passenger_status: Mapped[PassengerStatus] = mapped_column(
        _enum_column(PassengerStatus, "passenger_status"),
        nullable=False,
        default=PassengerStatus.PENDING,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        # A ticket can be VALID for at most one event; a second event presenting
        # it must end up USED. Enforced in the DB as well as in the repository.
        Index(
            "uq_passenger_events_valid_ticket",
            "ticket_id",
            unique=True,
            sqlite_where=text("ticket_status = 'VALID'"),
        ),
        CheckConstraint(
            "exit_time IS NULL OR exit_time >= entry_time", name="ck_exit_after_entry"
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        def iso(value: Optional[datetime]) -> Optional[str]:
            return value.isoformat() if value else None

        return {
            "event_id": self.event_id,
            "track_id": self.track_id,
            "entry_time": iso(self.entry_time),
            "exit_time": iso(self.exit_time),
            "ticket_id": self.ticket_id,
            "ticket_status": self.ticket_status.value if self.ticket_status else None,
            "passenger_status": self.passenger_status.value if self.passenger_status else None,
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
        }

    def __repr__(self) -> str:
        return (
            f"PassengerEvent(event_id={self.event_id}, track_id={self.track_id}, "
            f"ticket_id={self.ticket_id!r}, ticket_status={self.ticket_status}, "
            f"passenger_status={self.passenger_status})"
        )
