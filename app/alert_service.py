"""Conductor alerts: all user-facing outcome wording lives here.

A boarded person whose ticket has not been checked is shown as
"Ticket verification required" - never as a person without a ticket. Only a
completed check against the ticket database produces a failure outcome.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Deque, List, Optional, Sequence
from zoneinfo import ZoneInfo

from app.analytics import P, Period
from app.database import parse_iso, to_iso
from app.settings_schema import AppConfig

logger = logging.getLogger(__name__)

OUTCOME_LABELS = {
    "VALID": "Valid",
    "INVALID": "Invalid",
    "EXPIRED": "Expired",
    "WRONG_ROUTE": "Wrong route",
    "ALREADY_USED": "Already used",
    "VERIFICATION_REQUIRED": "Ticket verification required",
}
FAILURE_OUTCOMES = ("INVALID", "EXPIRED", "WRONG_ROUTE", "ALREADY_USED")


def outcome_label(outcome: object) -> str:
    key = getattr(outcome, "value", outcome)
    return OUTCOME_LABELS.get(str(key), str(key))


def failure_message(outcome: object, reason: str, track_id: Optional[int]) -> str:
    target = f" for Track ID {track_id}" if track_id is not None else ""
    return f"Ticket verification failed{target}: {outcome_label(outcome)} – {reason}"


def required_message(track_id: int, boarded_local: str) -> str:
    return (f"Ticket verification required for Track ID {track_id} (boarded {boarded_local}). "
            "Please check the passenger's ticket.")


def valid_message(reason: str, track_id: Optional[int]) -> str:
    target = f" for Track ID {track_id}" if track_id is not None else ""
    return f"Ticket valid{target}: {reason}"


@dataclass(frozen=True)
class Alert:
    at: datetime
    kind: str  # "verification_failed" | "verification_required"
    message: str
    outcome: str
    track_id: Optional[int] = None
    boarding_id: Optional[int] = None


def verification_alerts(conn: sqlite3.Connection, period: Period, now: datetime, cfg: AppConfig) -> List[Alert]:
    """Failed checks in the period + boardings still unverified after the timeout, newest first."""
    tz = ZoneInfo(cfg.app.timezone)
    limit = cfg.dashboard.table_limit
    params = {**period.params(), "limit": limit}
    failures = conn.execute(
        "SELECT v.validated_at, v.outcome, v.reason, v.boarding_id, b.track_id FROM validation_events v "
        "LEFT JOIN boarding_events b ON b.boarding_id = v.boarding_id "
        "WHERE v.outcome IN ('INVALID', 'EXPIRED', 'WRONG_ROUTE', 'ALREADY_USED') AND " + P("v.validated_at")
        + " ORDER BY v.validated_at DESC, v.validation_id DESC LIMIT :limit", params).fetchall()
    cutoff = to_iso(now - timedelta(seconds=cfg.alerts.verification_timeout_sec))
    unverified = conn.execute(
        "SELECT boarding_id, track_id, boarded_at FROM boarding_events "
        "WHERE verification_status = 'VERIFICATION_REQUIRED' AND boarded_at <= :cutoff AND " + P("boarded_at")
        + " ORDER BY boarded_at DESC, boarding_id DESC LIMIT :limit", {**params, "cutoff": cutoff}).fetchall()

    alerts = [Alert(parse_iso(r["validated_at"]), "verification_failed",
                    failure_message(r["outcome"], r["reason"], r["track_id"]), r["outcome"], r["track_id"],
                    r["boarding_id"]) for r in failures]
    for r in unverified:
        boarded = parse_iso(r["boarded_at"])
        alerts.append(Alert(boarded, "verification_required",
                            required_message(r["track_id"], boarded.astimezone(tz).strftime("%H:%M")),
                            "VERIFICATION_REQUIRED", r["track_id"], r["boarding_id"]))
    alerts.sort(key=lambda a: a.at, reverse=True)
    return alerts[:limit]


class AlertService:
    """Pipeline side: logs outcomes and keeps the last few overlay messages."""

    def __init__(self, max_messages: int = 3) -> None:
        self._messages: Deque[str] = deque(maxlen=max_messages)

    @property
    def messages(self) -> List[str]:
        return list(self._messages)

    def push(self, message: str) -> None:
        self._messages.append(message)

    def on_boarding(self, track_id: int, boarding_id: int) -> str:
        message = f"Boarding: Track ID {track_id} -> boarding #{boarding_id} (Ticket verification required)"
        logger.info(message)
        self.push(message)
        return message

    def on_validation(self, result, track_id: Optional[int]) -> str:
        """Log a validation result (WARNING for failures) and queue an overlay message."""
        outcome = getattr(result.outcome, "value", result.outcome)
        if outcome == "VALID":
            message = valid_message(result.reason, track_id)
            logger.info(message)
        elif outcome == "VERIFICATION_REQUIRED":
            message = f"{OUTCOME_LABELS[outcome]}: Track ID {track_id}"
            logger.info(message)
        else:
            message = failure_message(outcome, result.reason, track_id)
            logger.warning(message)
        self.push(message)
        return message

    def on_pending(self, track_ids: Sequence[int]) -> None:
        for tid in track_ids:
            message = f"Ticket verification required: ID {tid}"
            logger.info(message)
            self.push(message)
