"""Conductor alerts: all user-facing outcome wording lives here.

A boarded person whose ticket has not been checked is shown as
"Ticket verification required" - never as a person without a ticket. Only a
completed check against the ticket database produces a failure outcome.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Deque, Dict, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from app.analytics import SESSION_AT, P, Period, credential_key
from app.database import parse_iso, to_iso
from app.settings_schema import AppConfig
from app.system_status import format_age, is_camera_error

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
    key: str = ""
    validation_id: Optional[int] = None
    reason: Optional[str] = None


def verification_alerts(conn: sqlite3.Connection, period: Period, now: datetime, cfg: AppConfig) -> List[Alert]:
    """Failed checks in the period + boardings still unverified after the timeout, newest first."""
    tz = ZoneInfo(cfg.app.timezone)
    limit = cfg.dashboard.table_limit
    params = {**period.params(), "limit": limit}
    failures = conn.execute(
        "SELECT v.validation_id, v.validated_at, v.outcome, v.reason, v.boarding_id, b.track_id "
        "FROM validation_events v "
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
                    r["boarding_id"], validation_id=r["validation_id"], reason=r["reason"]) for r in failures]
    for r in unverified:
        boarded = parse_iso(r["boarded_at"])
        alerts.append(Alert(boarded, "verification_required",
                            required_message(r["track_id"], boarded.astimezone(tz).strftime("%H:%M")),
                            "VERIFICATION_REQUIRED", r["track_id"], r["boarding_id"]))
    alerts.sort(key=lambda a: a.at, reverse=True)
    return alerts[:limit]


# ------------------------------------------------------------- grouped alerts
GROUP_CAP = 1000
SEVERITIES = ("error", "warning", "info")
_TRACKING_RE = re.compile(r"(?i)\b(track|tracker|bytetrack|ultralytics|model)\b")
_REASON_KIND = "CASE WHEN v.outcome = 'INVALID' AND v.reason IN (:uqr1, :uqr2) THEN 'uqr' ELSE 'other' END"
_GROUP_WHERE = ("v.outcome IN ('INVALID', 'EXPIRED', 'WRONG_ROUTE', 'ALREADY_USED') AND " + P("v.validated_at"))


@dataclass(frozen=True)
class AlertGroup:
    key: str                 # stable identity used for read state
    source: str              # validation | boarding | session
    match: Tuple             # validation: (outcome, reason_kind, b, cred); boarding: (boarding_id,); session: (id,)
    kind: str                # invalid_ticket | expired_ticket | unrecognised_qr | unverified_passenger
                             # | detection_stopped | camera_disconnected | tracking_failure
    severity: str            # error | warning | info
    title: str
    message: str             # newest underlying message
    first_at: datetime
    last_at: datetime
    count: int               # number of underlying records (exact)
    track_id: Optional[int] = None
    ticket_ref: Optional[str] = None
    outcome: Optional[str] = None
    detail: str = ""         # message without the title (notification panel); "" means use ``message``


@dataclass(frozen=True)
class AlertRecord:
    at: datetime
    record_id: str           # "val:<id>" / "brd:<id>" / "ses:<id>"
    state: str               # outcome or session status


@dataclass(frozen=True)
class AlertGroupsResult:
    groups: List[AlertGroup]
    truncated: bool = False


def _qr_params() -> Dict[str, str]:
    from app.ticket_validator import NO_RECORD_QR_REASON, UNRECOGNISED_QR_REASON

    return {"uqr1": UNRECOGNISED_QR_REASON, "uqr2": NO_RECORD_QR_REASON}


def classify_session(row: Mapping, now: datetime, stale_sec: float) -> Optional[Tuple[str, str, str]]:
    """``(kind, severity, title)`` for one detection session, or None (one kind per session)."""
    status, error = row["status"], row["last_error"] or ""
    if status == "FAILED":
        if is_camera_error(error):
            return "camera_disconnected", "error", "Camera disconnected"
        if _TRACKING_RE.search(error):
            return "tracking_failure", "error", "Tracking failure"
        return "detection_stopped", "error", "Detection failed"
    if status == "RUNNING":
        seen = parse_iso(row["last_heartbeat_at"] or row["started_at"])
        if (now - seen).total_seconds() > float(stale_sec):
            return "detection_stopped", "warning", "Detection not responding"
        return None
    if status == "STOPPED":
        return "detection_stopped", "info", "Detection stopped"
    return None


def _validation_groups(conn: sqlite3.Connection, period: Period) -> List[AlertGroup]:
    cred = credential_key("v")
    params = {**period.params(), **_qr_params(), "cap": GROUP_CAP + 1}
    rows = conn.execute(
        f"SELECT v.outcome, {_REASON_KIND} AS reason_kind, COALESCE(v.boarding_id, 'desk') AS b, {cred} AS cred, "
        "COUNT(*) AS n, MIN(v.validated_at) AS first_at, MAX(v.validated_at) AS last_at, "
        "MAX(v.validation_id) AS newest_id, MAX(bo.track_id) AS track_id "
        "FROM validation_events v LEFT JOIN boarding_events bo ON bo.boarding_id = v.boarding_id "
        f"WHERE {_GROUP_WHERE} GROUP BY v.outcome, reason_kind, b, cred "
        "ORDER BY last_at DESC, newest_id DESC LIMIT :cap", params).fetchall()
    newest: Dict[int, Tuple[str, Optional[str]]] = {}
    ids = [int(r["newest_id"]) for r in rows]
    if ids:
        marks = ", ".join("?" for _ in ids)
        for vid, reason, ref in conn.execute(
                f"SELECT validation_id, reason, COALESCE(ticket_id, presented_ref) FROM validation_events "
                f"WHERE validation_id IN ({marks})", ids):
            newest[int(vid)] = (reason, ref)
    groups = []
    for r in rows:
        outcome, kind_sql, b = r["outcome"], r["reason_kind"], r["b"]
        reason, ref = newest.get(int(r["newest_id"]), ("", None))
        if kind_sql == "uqr":
            key, kind, severity, title = f"uqr:{b}:{r['cred']}", "unrecognised_qr", "error", "Unrecognised QR code"
        elif outcome == "EXPIRED":
            key, kind, severity, title = f"exp:{b}:{r['cred']}", "expired_ticket", "warning", "Expired ticket"
        else:
            title = "Invalid ticket" if outcome == "INVALID" else outcome_label(outcome)
            key, kind, severity = f"inv:{outcome}:{b}:{r['cred']}", "invalid_ticket", "error"
        detail = outcome_label(outcome) + (f" – {reason}" if reason else "")
        groups.append(AlertGroup(key, "validation", (outcome, kind_sql, b, r["cred"]), kind, severity, title,
                                 failure_message(outcome, reason, r["track_id"]), parse_iso(r["first_at"]),
                                 parse_iso(r["last_at"]), int(r["n"]), r["track_id"], ref, outcome, detail))
    return groups


def alert_groups(conn: sqlite3.Connection, period: Period, now: datetime, cfg: AppConfig) -> AlertGroupsResult:
    """Deduplicated alerts in the period, newest first (counts are exact SQL aggregates).

    ``GROUP_CAP`` applies per source (validation, boarding, session), so the result holds
    at most 3 x ``GROUP_CAP`` groups; ``truncated`` is set when any source hit its cap.

    The header chip is the authoritative current detection state; session
    alerts follow the selected period (``analytics.SESSION_AT``).
    """
    tz = ZoneInfo(cfg.app.timezone)
    groups = _validation_groups(conn, period)
    truncated = len(groups) > GROUP_CAP
    groups = groups[:GROUP_CAP]
    cutoff = to_iso(now - timedelta(seconds=cfg.alerts.verification_timeout_sec))
    boardings = conn.execute(
        "SELECT boarding_id, track_id, boarded_at FROM boarding_events "
        "WHERE verification_status = 'VERIFICATION_REQUIRED' AND boarded_at <= :cutoff AND " + P("boarded_at")
        + " ORDER BY boarded_at DESC, boarding_id DESC LIMIT :cap",
        {**period.params(), "cutoff": cutoff, "cap": GROUP_CAP + 1}).fetchall()
    truncated |= len(boardings) > GROUP_CAP
    for r in boardings[:GROUP_CAP]:
        at = parse_iso(r["boarded_at"])
        boarded_local = at.astimezone(tz).strftime("%H:%M")
        groups.append(AlertGroup(f"unv:{r['boarding_id']}", "boarding", (int(r["boarding_id"]),),
                                 "unverified_passenger", "warning", OUTCOME_LABELS["VERIFICATION_REQUIRED"],
                                 required_message(r["track_id"], boarded_local), at, at, 1,
                                 r["track_id"], None, "VERIFICATION_REQUIRED",
                                 f"Boarded {boarded_local}. Please check the passenger's ticket."))
    sessions = conn.execute(
        "SELECT session_id, status, started_at, ended_at, last_heartbeat_at, last_error, source_label "
        "FROM detection_sessions WHERE " + P(SESSION_AT) + " ORDER BY " + SESSION_AT + " DESC LIMIT :cap",
        {**period.params(), "cap": GROUP_CAP + 1}).fetchall()
    truncated |= len(sessions) > GROUP_CAP
    stale = cfg.runtime.heartbeat_stale_sec
    for r in sessions[:GROUP_CAP]:
        found = classify_session(r, now, stale)
        if found is None:
            continue
        kind, severity, title = found
        at = parse_iso(r["ended_at"] or r["last_heartbeat_at"] or r["started_at"])
        if r["status"] == "STOPPED":
            detail = "Stopped cleanly"
        elif r["status"] == "RUNNING":
            detail = f"no heartbeat for {format_age((now - at).total_seconds())}"
        else:
            detail = (r["last_error"] or "").splitlines()[0][:160] if r["last_error"] else "unknown error"
        prefix = {"camera_disconnected": "cam", "tracking_failure": "trk"}.get(kind, "stop")
        text = f"{r['source_label']} · {detail}"
        groups.append(AlertGroup(f"{prefix}:{r['session_id']}", "session", (r["session_id"],), kind, severity,
                                 title, text, at, at, 1, None, None, r["status"], text))
    groups.sort(key=lambda g: g.last_at, reverse=True)
    return AlertGroupsResult(groups, truncated)


def group_records(conn: sqlite3.Connection, group: AlertGroup, period: Period, limit: int = 20) -> List[AlertRecord]:
    """Underlying records of one group, newest first (at most ``limit``)."""
    if group.source == "boarding":
        return [AlertRecord(group.last_at, f"brd:{group.match[0]}", group.outcome or "")]
    if group.source == "session":
        return [AlertRecord(group.last_at, f"ses:{str(group.match[0])[:8]}", group.outcome or "")]
    outcome, reason_kind, b, cred = group.match
    params = {**period.params(), **_qr_params(), "outcome": outcome, "reason_kind": reason_kind, "b": b,
              "cred": cred, "limit": int(limit)}
    rows = conn.execute(
        "SELECT v.validation_id, v.validated_at, v.outcome FROM validation_events v WHERE v.outcome = :outcome "
        f"AND ({_REASON_KIND}) = :reason_kind AND COALESCE(v.boarding_id, 'desk') = :b "
        f"AND {credential_key('v')} = :cred AND " + P("v.validated_at")
        + " ORDER BY v.validated_at DESC, v.validation_id DESC LIMIT :limit", params).fetchall()
    return [AlertRecord(parse_iso(r["validated_at"]), f"val:{r['validation_id']}", r["outcome"]) for r in rows]


def unread_count(groups: Sequence[AlertGroup], read: Mapping[str, str]) -> int:
    """Error/warning groups never marked read, or with a newer ``last_at`` since (info is never counted)."""
    return sum(1 for g in groups if g.severity in ("error", "warning")
               and (g.key not in read or to_iso(g.last_at) > read[g.key]))


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
