"""Per-frame orchestration: tracker -> throttle -> boarding -> QR link -> validator -> alerts.

``DetectionPipeline.process_frame`` is called once per frame by ``app.main``.
A person seen by the camera is only *detected*; a *boarding* is created when
the boarding rules confirm an entry-line crossing, and it starts as
"Ticket verification required". A QR code seen in the frame is validated only
when it can be linked to a boarded Track ID (its centre inside that person's
box, or, when it is inside no tracked person's box, exactly one boarding within
``qr.fallback_window_sec``); unlinked QR codes are shown on the overlay but not
recorded.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import IO, Any, Dict, List, Optional, Sequence, Set, Tuple

import cv2
import numpy as np

from app.alert_service import AlertService
from app.config import resolve_path
from app.database import utc_now
from app.overlay import OverlayState, draw_overlay
from app.passenger_service import BoardingInfo, BoardingRules, PassengerService, is_busy_error
from app.qr_service import parse_payload
from app.settings_schema import AppConfig
from app.ticket_validator import JourneyContext, TicketValidator

logger = logging.getLogger(__name__)

UNLINKED_QR_MESSAGE = "QR seen – not linked to a boarded passenger"


class DetectionPipeline:
    def __init__(self, *, tracker: Any, service: PassengerService, boarding_rules: BoardingRules,
                 validator: TicketValidator, alerts: AlertService, journey: JourneyContext, cfg: AppConfig,
                 scanner: Any = None, mot_writer: Optional[IO[str]] = None, clock=utc_now) -> None:
        self.tracker = tracker
        self.service = service
        self.rules = boarding_rules
        self.validator = validator
        self.alerts = alerts
        self.journey = journey
        self.cfg = cfg
        self.scanner = scanner
        self.mot_writer = mot_writer
        self._clock = clock
        self.qr_enabled = bool(cfg.qr.enabled and scanner is not None)
        self.preview_path: Optional[Path] = (resolve_path(cfg.runtime.preview_path)
                                             if cfg.runtime.preview_interval_sec > 0 else None)
        self.frame_count = 0
        self.last_people: Sequence[Any] = ()
        self.started_at: Optional[datetime] = None
        self._last_flush: Optional[datetime] = None
        self._last_preview: Optional[datetime] = None
        self._qr_seen: Dict[str, float] = {}             # token hash -> last seen (epoch s)
        self._validated: Set[Tuple[str, int]] = set()     # (token hash, boarding id)
        self._announced: Set[int] = set()                 # boarding ids announced as pending

    # --------------------------------------------------------------- frame
    def process_frame(self, frame: np.ndarray, now: Optional[datetime] = None) -> List[BoardingInfo]:
        """Process one BGR frame; returns the boardings recorded in this frame."""
        now = now or self._clock()
        if self.started_at is None:
            self.started_at = self._last_flush = now
        height, width = frame.shape[:2]
        frame_index = self.frame_count
        self.frame_count += 1

        people = self.tracker.track(frame)                                  # 1. detect + track
        self.last_people = people
        self.service.record_detections(people, frame_index, now)            # 2. throttled records

        new = self.service.retry_boardings()                                # 3. boardings
        for event in self.rules.update(people, now.timestamp(), (width, height)):
            boarding_id = self.service.record_boarding(event, station=self.journey.current_station,
                                                       trip_id=self.journey.trip_id)
            if boarding_id is not None:
                new.append(self.service.boardings[int(event.track_id)])
        for info in new:
            self.alerts.on_boarding(info.track_id, info.boarding_id)

        if self.qr_enabled and self.frame_count % self.cfg.qr.scan_every_n_frames == 0:
            self._scan(frame, people, now)                                   # 4. QR tickets

        if (now - self._last_flush).total_seconds() >= self.cfg.database.flush_interval_sec:
            self.flush(now)                                                  # 5. flush + heartbeat
        self._maybe_preview(frame, now)                                      # 6. dashboard preview
        if self.mot_writer is not None:                                      # 7. evaluation export
            self._write_mot(people)
        return new

    # ------------------------------------------------------------------ QR
    def _scan(self, frame: np.ndarray, people: Sequence[Any], now: datetime) -> None:
        try:
            detections = self.scanner.decode_frame(frame)
        except Exception as exc:  # QR decoding must never stop the camera loop
            logger.debug("QR decode failed: %s", exc)
            return
        ts = now.timestamp()
        for det in detections:
            parsed = parse_payload(det.payload)
            if not parsed.wellformed:
                logger.debug("Ignoring unrecognised QR payload")
                continue
            last = self._qr_seen.get(parsed.token_hash)
            if last is not None and ts - last < self.cfg.qr.rescan_cooldown_sec:
                continue
            self._qr_seen[parsed.token_hash] = ts
            info = self._link(getattr(det, "points", None), people, ts)
            if info is None:
                logger.info(UNLINKED_QR_MESSAGE)
                self.alerts.push(UNLINKED_QR_MESSAGE)
                continue
            key = (parsed.token_hash, info.boarding_id)
            if key in self._validated:
                continue
            try:
                result = self.validator.validate(
                    self.service.conn, det.payload, credential_type="qr_token", journey=self.journey,
                    channel="camera", session_id=self.service.session_id, boarding_id=info.boarding_id, now=now)
            except sqlite3.OperationalError as exc:
                if not is_busy_error(exc):
                    raise
                logger.warning("Database busy; QR check for Track ID %s will be retried (%s)", info.track_id, exc)
                self._qr_seen.pop(parsed.token_hash, None)
                continue
            self._validated.add(key)
            self.service.note_validation(info.boarding_id, result.outcome.value)
            self.alerts.on_validation(result, info.track_id)

    def _link(self, points, people: Sequence[Any], ts: float) -> Optional[BoardingInfo]:
        """Boarding of the person whose box contains the QR centre, else the single recent boarding.

        The recent-boarding fallback is used only when the QR cannot be placed on
        anyone (no corner points, or its centre is in no tracked person's box). A QR
        held by a tracked person who has not boarded is left unlinked, so it can never
        verify somebody else's boarding.
        """
        boardings = self.service.boardings
        if points:
            cx = sum(p[0] for p in points) / len(points)
            cy = sum(p[1] for p in points) / len(points)
            holders = [int(person.track_id) for person in people
                       if person.bbox[0] <= cx <= person.bbox[2] and person.bbox[1] <= cy <= person.bbox[3]]
            for track_id in holders:
                if track_id in boardings:
                    return boardings[track_id]
            if holders:
                return None
        window = self.cfg.qr.fallback_window_sec
        recent = [b for b in boardings.values() if 0 <= ts - b.boarded_at.timestamp() <= window]
        return recent[0] if len(recent) == 1 else None

    # ------------------------------------------------------- flush/preview
    @property
    def fps(self) -> Optional[float]:
        if self.started_at is None or self._last_flush is None:
            return None
        elapsed = (self._last_flush - self.started_at).total_seconds()
        return round(self.frame_count / elapsed, 2) if elapsed > 0 else None

    def flush(self, now: Optional[datetime] = None) -> None:
        """Write buffered detections + heartbeat and refresh the pending-verification messages."""
        now = now or self._clock()
        self._last_flush = now
        cutoff = now.timestamp() - self.cfg.qr.rescan_cooldown_sec  # older entries no longer suppress a scan
        self._qr_seen = {h: seen for h, seen in self._qr_seen.items() if seen > cutoff}
        self.service.heartbeat(self.frame_count, self.fps, now)
        pending = self.service.pending_verifications(now, self.cfg.alerts.verification_timeout_sec)
        fresh = [b for b in pending if b.boarding_id not in self._announced]
        self._announced.update(b.boarding_id for b in fresh)
        self.alerts.on_pending([b.track_id for b in fresh])

    def _maybe_preview(self, frame: np.ndarray, now: datetime) -> None:
        if self.preview_path is None:
            return
        if self._last_preview is not None and \
                (now - self._last_preview).total_seconds() < self.cfg.runtime.preview_interval_sec:
            return
        self._last_preview = now
        try:
            ok, buf = cv2.imencode(".jpg", self.render(frame.copy()))
            if not ok:
                return
            self.preview_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.preview_path.with_name(self.preview_path.name + ".tmp")
            tmp.write_bytes(buf.tobytes())
            os.replace(tmp, self.preview_path)
        except OSError as exc:  # e.g. Windows PermissionError while the dashboard reads the file
            logger.debug("Preview not written: %s", exc)

    def _write_mot(self, people: Sequence[Any]) -> None:
        for person in people:
            x1, y1, x2, y2 = person.bbox
            self.mot_writer.write(f"{self.frame_count},{person.track_id},{x1:.2f},{y1:.2f},{x2 - x1:.2f},"
                                  f"{y2 - y1:.2f},{person.confidence:.4f},-1,-1,-1\n")

    # ------------------------------------------------------------- overlay
    def overlay_state(self, frame_size: Tuple[int, int]) -> OverlayState:
        boardings = self.service.boardings
        valid = sum(1 for b in boardings.values() if b.status == "VALID")
        return OverlayState(
            people=self.last_people, line=self.rules.entry_line.line_endpoints(frame_size),
            status_by_track={tid: b.status for tid, b in boardings.items()}, boardings=len(boardings),
            valid=valid, needs_check=len(boardings) - valid, messages=self.alerts.messages)

    def render(self, frame: np.ndarray) -> np.ndarray:
        """Draw the overlay onto ``frame`` (in place) and return it."""
        height, width = frame.shape[:2]
        return draw_overlay(frame, self.overlay_state((width, height)))
