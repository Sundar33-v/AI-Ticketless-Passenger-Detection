"""Data access for passenger events.

Status rules (see ``models`` for definitions):

* ``create_entry``               -> PENDING, no ticket.
* ``validate_and_assign_ticket`` -> VALID ticket => VERIFIED;
                                    INVALID / USED / NOT_FOUND => NEEDS_CHECK.
                                    A VERIFIED event is never downgraded by a later failed scan.
* ``record_exit``                -> PENDING at exit => NEEDS_CHECK (manual check),
                                    never an automatic "ticketless" verdict.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.database import Database
from app.database.models import (
    TICKET_ID_MAX_LENGTH,
    PassengerEvent,
    PassengerStatus,
    TicketStatus,
    utcnow,
)

if TYPE_CHECKING:  # avoid a hard import cycle at runtime
    from app.ticket.ticket_validator import TicketValidator, ValidationResult


class EventNotFoundError(LookupError):
    pass


def _to_naive_utc(value: Optional[datetime]) -> datetime:
    if value is None:
        return utcnow()
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _get_event_or_raise(session: Session, event_id: int) -> PassengerEvent:
    event = session.get(PassengerEvent, event_id)
    if event is None:
        raise EventNotFoundError(f"Passenger event {event_id} not found")
    return event


def _ticket_in_use(session: Session, ticket_id: str, exclude_event_id: Optional[int]) -> bool:
    stmt = select(PassengerEvent.event_id).where(
        PassengerEvent.ticket_id == ticket_id,
        PassengerEvent.ticket_status == TicketStatus.VALID,
    )
    if exclude_event_id is not None:
        stmt = stmt.where(PassengerEvent.event_id != exclude_event_id)
    return session.execute(stmt.limit(1)).first() is not None


class PassengerRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------------ entry/exit
    def create_entry(self, track_id: int, entry_time: Optional[datetime] = None) -> PassengerEvent:
        """Record a passenger entry. Status starts as PENDING (not ticketless)."""
        if not isinstance(track_id, int) or isinstance(track_id, bool) or track_id < 0:
            raise ValueError(f"track_id must be a non-negative int, got {track_id!r}")
        with self._db.session() as session:
            event = PassengerEvent(
                track_id=track_id,
                entry_time=_to_naive_utc(entry_time),
                passenger_status=PassengerStatus.PENDING,
            )
            session.add(event)
            session.flush()
            return event

    def record_exit(self, event_id: int, exit_time: Optional[datetime] = None) -> PassengerEvent:
        """Record exit. Idempotent: a second call keeps the first exit time."""
        with self._db.session() as session:
            event = _get_event_or_raise(session, event_id)
            if event.exit_time is not None:
                return event
            exit_at = _to_naive_utc(exit_time)
            if exit_at < event.entry_time:
                raise ValueError("exit_time cannot be earlier than entry_time")
            event.exit_time = exit_at
            if event.passenger_status is PassengerStatus.PENDING:
                event.passenger_status = PassengerStatus.NEEDS_CHECK
            session.flush()
            return event

    # ------------------------------------------------------------------ tickets
    def validate_and_assign_ticket(
        self,
        event_id: int,
        raw_ticket_id: Any,
        validator: "TicketValidator",
        now: Optional[datetime] = None,
    ) -> Tuple[PassengerEvent, "ValidationResult"]:
        """Validate a scanned ticket and link the outcome to the event.

        A ticket already VALID on a *different* event is reported as USED.
        Re-scanning the same ticket for the same event stays VALID.
        """
        try:
            with self._db.session() as session:
                event = _get_event_or_raise(session, event_id)
                result = validator.validate(
                    raw_ticket_id,
                    now=now,
                    is_used=lambda tid: _ticket_in_use(session, tid, exclude_event_id=event_id),
                )
                self._apply_result(event, result)
                session.flush()
                return event, result
        except IntegrityError:
            # Lost a race: another event claimed this ticket between check and write.
            result = dataclasses.replace(
                result,
                status=TicketStatus.USED,
                reason="Ticket already linked to another passenger",
            )
            with self._db.session() as session:
                event = _get_event_or_raise(session, event_id)
                self._apply_result(event, result)
                session.flush()
                return event, result

    @staticmethod
    def _apply_result(event: PassengerEvent, result: "ValidationResult") -> None:
        if event.passenger_status is PassengerStatus.VERIFIED and not result.is_valid:
            return  # keep the verified ticket; a bad re-scan doesn't revoke it
        stored_id = result.ticket_id or (result.raw_value or "").strip()[:TICKET_ID_MAX_LENGTH] or None
        event.ticket_id = stored_id
        event.ticket_status = result.status
        event.passenger_status = (
            PassengerStatus.VERIFIED if result.is_valid else PassengerStatus.NEEDS_CHECK
        )

    def is_ticket_in_use(self, ticket_id: str, exclude_event_id: Optional[int] = None) -> bool:
        with self._db.session() as session:
            return _ticket_in_use(session, ticket_id, exclude_event_id)

    def set_passenger_status(self, event_id: int, status: PassengerStatus) -> PassengerEvent:
        """Manual override (e.g. conductor verified the passenger by hand)."""
        status = PassengerStatus(status)
        with self._db.session() as session:
            event = _get_event_or_raise(session, event_id)
            event.passenger_status = status
            session.flush()
            return event

    def mark_needs_check(self, event_id: int) -> PassengerEvent:
        return self.set_passenger_status(event_id, PassengerStatus.NEEDS_CHECK)

    # ------------------------------------------------------------------ queries
    def get_event(self, event_id: int) -> Optional[PassengerEvent]:
        with self._db.session() as session:
            return session.get(PassengerEvent, event_id)

    def get_open_event_for_track(self, track_id: int) -> Optional[PassengerEvent]:
        """Most recent event for this track that has not exited yet."""
        with self._db.session() as session:
            stmt = (
                select(PassengerEvent)
                .where(PassengerEvent.track_id == track_id, PassengerEvent.exit_time.is_(None))
                .order_by(PassengerEvent.entry_time.desc(), PassengerEvent.event_id.desc())
                .limit(1)
            )
            return session.execute(stmt).scalar_one_or_none()

    def list_events(
        self,
        passenger_status: Optional[PassengerStatus] = None,
        ticket_status: Optional[TicketStatus] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[PassengerEvent]:
        """Newest first."""
        limit = max(1, min(int(limit), 10_000))
        offset = max(0, int(offset))
        with self._db.session() as session:
            stmt = select(PassengerEvent)
            if passenger_status is not None:
                stmt = stmt.where(PassengerEvent.passenger_status == PassengerStatus(passenger_status))
            if ticket_status is not None:
                stmt = stmt.where(PassengerEvent.ticket_status == TicketStatus(ticket_status))
            stmt = stmt.order_by(PassengerEvent.entry_time.desc(), PassengerEvent.event_id.desc())
            return list(session.execute(stmt.limit(limit).offset(offset)).scalars())

    def status_counts(self) -> Dict[str, int]:
        """Counts per passenger status (all statuses present, zero-filled)."""
        counts = {status.value: 0 for status in PassengerStatus}
        with self._db.session() as session:
            rows = session.execute(
                select(PassengerEvent.passenger_status, func.count()).group_by(
                    PassengerEvent.passenger_status
                )
            )
            for status, count in rows:
                counts[status.value] = count
        return counts
