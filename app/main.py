"""Application entry point: camera -> YOLO/ByteTrack -> entry line -> SQLite.

Pipeline (one iteration per frame)::

    Android phone (IP Webcam) --OpenCV--> VideoSource
      -> PersonTracker   (YOLO, COCO class 0 "person" only, + ByteTrack Track IDs)
      -> EntryLineDetector (virtual entry-line crossing -> EntryEvent)
      -> PassengerRepository.create_entry (SQLite, status PENDING)
      -> QRScanner + TicketValidator (optional, ticket QR seen in the frame)
      -> PassengerRepository.validate_and_assign_ticket (VERIFIED / NEEDS_CHECK)

The Streamlit conductor dashboard (``app/dashboard/dashboard.py``) reads the
same SQLite database. No facial recognition is performed anywhere: a Track ID
is only an anonymous per-session handle from ByteTrack.

Run from the project root::

    python -m app.main                               # phone camera from config.yaml
    python -m app.main --source 0                    # local webcam
    python -m app.main --source "http://192.0.0.4:8080/video" --no-display
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow `python app/main.py` as well as `-m`
    sys.path.insert(0, str(PROJECT_ROOT))

from app.camera import CameraConnectionError, VideoSource, load_camera_config  # noqa: E402
from app.camera.video_source import DEFAULT_CONFIG_PATH  # noqa: E402
from app.database import PassengerRepository, init_db  # noqa: E402
from app.passenger import EntryEvent, EntryLineConfig, EntryLineDetector  # noqa: E402
from app.tracking import PersonTracker, TrackedPerson, TrackerConfig  # noqa: E402

logger = logging.getLogger("app.main")

WINDOW_TITLE = "Ticketless Passenger Detection (press q to quit)"


# --------------------------------------------------------------------- config
@dataclass
class QRLinkConfig:
    """How QR tickets seen by the camera are linked to passenger events."""

    enabled: bool = True
    scan_every_n_frames: int = 3  # QR decoding is relatively expensive
    rescan_cooldown_sec: float = 5.0  # ignore the same payload for this long
    # If a QR is not inside any tracked person's box, link it to the latest
    # entry only when exactly one entry happened within this window.
    fallback_window_sec: float = 30.0

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "QRLinkConfig":
        data = dict(data or {})
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


@dataclass
class AppConfig:
    raw: Dict[str, Any] = field(default_factory=dict)
    config_path: Optional[Path] = None

    def section(self, *names: str) -> Dict[str, Any]:
        """First present top-level section among ``names`` (config is owned by several modules)."""
        for name in names:
            value = self.raw.get(name)
            if isinstance(value, dict):
                return value
        return {}


def load_app_config(path: Optional[Path]) -> AppConfig:
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not path.is_file():
        logger.warning("Config file %s not found; using defaults.", path)
        return AppConfig({}, None)
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML not installed; using defaults.")
        return AppConfig({}, path)
    with path.open("r", encoding="utf-8") as fh:
        return AppConfig(yaml.safe_load(fh) or {}, path)


# ------------------------------------------------------------------- pipeline
class PassengerPipeline:
    """Glue between the modules owned by the camera/detection/tracking/entry/db/ticket layers."""

    def __init__(
        self,
        tracker: PersonTracker,
        entry_detector: EntryLineDetector,
        repository: PassengerRepository,
        validator: Any = None,
        qr_scanner: Any = None,
        qr_config: Optional[QRLinkConfig] = None,
        clock=time.time,
    ) -> None:
        self.tracker = tracker
        self.entry_detector = entry_detector
        self.repository = repository
        self.validator = validator
        self.qr_scanner = qr_scanner
        self.qr_config = qr_config or QRLinkConfig(enabled=qr_scanner is not None)
        self._clock = clock
        self.frame_count = 0
        # Session-local map Track ID -> (event_id, entry epoch seconds).
        self.track_events: Dict[int, Tuple[int, float]] = {}
        self._qr_last_seen: Dict[str, float] = {}
        self.last_people: List[TrackedPerson] = []
        self.last_messages: List[str] = []
        self._counts: Optional[Dict[str, int]] = None

    # ............................................................... per frame
    def process_frame(self, frame) -> List[EntryEvent]:
        now = self._clock()
        self.frame_count += 1
        height, width = frame.shape[:2]

        people = self.tracker.track(frame)
        self.last_people = people
        events = self.entry_detector.update(people, timestamp=now, frame_size=(width, height))
        for event in events:
            self._record_entry(event)

        if (
            self.qr_config.enabled
            and self.qr_scanner is not None
            and self.validator is not None
            and self.frame_count % max(1, int(self.qr_config.scan_every_n_frames)) == 0
        ):
            self._scan_tickets(frame, people, now)
        return events

    def _record_entry(self, event: EntryEvent) -> None:
        entry_time = datetime.fromtimestamp(event.timestamp, tz=timezone.utc)
        db_event = self.repository.create_entry(int(event.track_id), entry_time=entry_time)
        self.track_events[int(event.track_id)] = (db_event.event_id, event.timestamp)
        msg = f"Entry: Track ID {event.track_id} -> event #{db_event.event_id} (PENDING)"
        logger.info(msg)
        self._push_message(msg)

    # ................................................................ tickets
    def _scan_tickets(self, frame, people: Sequence[TrackedPerson], now: float) -> None:
        try:
            detections = self.qr_scanner.decode_frame(frame)
        except Exception as exc:  # never let QR decoding kill the camera loop
            logger.debug("QR decode failed: %s", exc)
            return
        for det in detections:
            last = self._qr_last_seen.get(det.payload)
            if last is not None and now - last < self.qr_config.rescan_cooldown_sec:
                continue
            self._qr_last_seen[det.payload] = now
            event_id = self._event_for_qr(det, people, now)
            if event_id is None:
                logger.info("QR %r seen but no passenger event to link it to.", det.ticket_id)
                continue
            self.assign_ticket(event_id, det.ticket_id if det.ticket_id else det.payload)

    def _event_for_qr(self, det: Any, people: Sequence[TrackedPerson], now: float) -> Optional[int]:
        points = getattr(det, "points", None)
        if points:
            cx = sum(p[0] for p in points) / len(points)
            cy = sum(p[1] for p in points) / len(points)
            for person in people:
                x1, y1, x2, y2 = person.bbox
                if x1 <= cx <= x2 and y1 <= cy <= y2 and person.track_id in self.track_events:
                    return self.track_events[person.track_id][0]
        recent = [
            eid
            for eid, ts in self.track_events.values()
            if now - ts <= self.qr_config.fallback_window_sec
        ]
        return recent[0] if len(recent) == 1 else None

    def assign_ticket(self, event_id: int, raw_ticket_id: Any) -> Optional[str]:
        try:
            event, result = self.repository.validate_and_assign_ticket(
                event_id, raw_ticket_id, self.validator
            )
        except Exception as exc:
            logger.error("Ticket validation failed for event #%s: %s", event_id, exc)
            return None
        msg = (
            f"Ticket {result.ticket_id or raw_ticket_id!r} -> event #{event_id}: "
            f"{result.status.value} ({result.reason}); passenger {event.passenger_status.value}"
        )
        logger.info(msg)
        self._push_message(msg)
        return msg

    def _push_message(self, msg: str) -> None:
        self.last_messages = (self.last_messages + [msg])[-3:]

    # ................................................................ overlay
    def draw_overlay(self, frame):
        import cv2

        h, w = frame.shape[:2]
        p1, p2 = self.entry_detector.line_endpoints((w, h))
        cv2.line(frame, p1, p2, (0, 255, 255), 2)
        cv2.putText(frame, "ENTRY LINE", (p1[0] + 5, max(15, p1[1] - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1, cv2.LINE_AA)
        for person in self.last_people:
            x1, y1, x2, y2 = (int(v) for v in person.bbox)
            entered = person.track_id in self.track_events
            color = (0, 200, 0) if entered else (255, 160, 0)
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            label = f"ID {person.track_id} {person.confidence:.2f}" + (" ENTERED" if entered else "")
            cv2.putText(frame, label, (x1, max(15, y1 - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
        if self._counts is None or self.frame_count % 15 == 0:  # avoid a DB query every frame
            self._counts = self.repository.status_counts()
        counts = self._counts
        summary = (
            f"Events {sum(counts.values())} | Verified {counts.get('VERIFIED', 0)} | "
            f"Pending {counts.get('PENDING', 0)} | Needs check {counts.get('NEEDS_CHECK', 0)}"
        )
        cv2.putText(frame, summary, (10, h - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (255, 255, 255), 2, cv2.LINE_AA)
        for i, msg in enumerate(reversed(self.last_messages)):
            cv2.putText(frame, msg[:90], (10, 22 + 20 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                        (255, 255, 255), 1, cv2.LINE_AA)
        return frame


# ---------------------------------------------------------------------- build
def _parse_source(value: Optional[str]):
    if value is None:
        return None
    text = value.strip()
    return int(text) if text.isdigit() else text


def build_pipeline(args: argparse.Namespace, cfg: AppConfig) -> Tuple[VideoSource, PassengerPipeline]:
    camera_cfg = load_camera_config(cfg.config_path) if cfg.config_path else load_camera_config()
    if args.source is not None:
        camera_cfg.source = _parse_source(args.source)
    if args.no_fallback:
        camera_cfg.fallback_to_webcam = False
    source = VideoSource(camera_cfg)

    tracker_cfg = TrackerConfig.from_dict(cfg.section("tracking", "tracker"))
    detection_cfg = cfg.section("detection")
    if "model_path" in detection_cfg and "model_path" not in cfg.section("tracking", "tracker"):
        tracker_cfg.model_path = detection_cfg["model_path"]
    if args.model:
        tracker_cfg.model_path = args.model
    if args.conf is not None:
        tracker_cfg.conf = args.conf
    if args.device:
        tracker_cfg.device = args.device
    tracker = PersonTracker(tracker_cfg)

    entry_detector = EntryLineDetector(EntryLineConfig.from_dict(cfg.section("entry_line", "entry")))

    db_section = cfg.section("database")
    db = init_db(args.db or db_section.get("path") or None)
    repository = PassengerRepository(db)
    logger.info("Database: %s", db.url)

    validator = scanner = None
    qr_cfg = QRLinkConfig.from_dict(cfg.section("qr", "qr_scanner"))
    if args.no_qr:
        qr_cfg.enabled = False
    ticket_section = cfg.section("tickets", "ticket")
    tickets_path = args.tickets or ticket_section.get("path") or ticket_section.get("tickets_path")
    try:
        from app.ticket import QRScanner, TicketValidator

        validator = TicketValidator(tickets_path)
        logger.info("Loaded %d tickets from %s", len(validator), validator.tickets_path)
        if qr_cfg.enabled:
            scanner = QRScanner(ticket_section.get("qr_backend", "auto"))
    except FileNotFoundError as exc:
        logger.warning("Ticket list not found (%s); QR verification disabled. "
                       "Generate one with: python -m app.ticket.generate_test_tickets", exc)
        qr_cfg.enabled = False
    except Exception as exc:
        logger.warning("Ticket validation unavailable (%s); QR verification disabled.", exc)
        qr_cfg.enabled = False

    pipeline = PassengerPipeline(tracker, entry_detector, repository, validator, scanner, qr_cfg)
    return source, pipeline


def run(source: VideoSource, pipeline: PassengerPipeline, display: bool, max_frames: int = 0) -> int:
    import cv2

    started = time.time()
    try:
        source.open()
    except CameraConnectionError as exc:
        logger.error("%s", exc)
        return 2
    logger.info("Running on source %r. %s", source.active_source,
                "Press q in the video window to stop." if display else "Press Ctrl+C to stop.")
    try:
        for frame in source.frames():
            pipeline.process_frame(frame)
            if display:
                try:
                    cv2.imshow(WINDOW_TITLE, pipeline.draw_overlay(frame))
                    if cv2.waitKey(1) & 0xFF == ord("q"):
                        break
                except cv2.error as exc:
                    logger.warning("Display unavailable (%s); continuing headless.", exc)
                    display = False
            if max_frames and pipeline.frame_count >= max_frames:
                break
    except CameraConnectionError as exc:
        logger.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        logger.info("Stopped by user.")
    finally:
        source.release()
        if display:
            try:
                cv2.destroyAllWindows()
            except Exception:
                pass
    elapsed = max(1e-6, time.time() - started)
    logger.info("Processed %d frames (%.1f FPS), %d entry events this session.",
                pipeline.frame_count, pipeline.frame_count / elapsed, len(pipeline.track_events))
    return 0


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AI Ticketless Passenger Detection - detection pipeline")
    p.add_argument("--config", type=Path, default=None, help="config.yaml path (default: config/config.yaml)")
    p.add_argument("--source", default=None,
                   help="override camera source: phone URL (default http://192.0.0.4:8080/video), "
                        "webcam index (0), or video file path")
    p.add_argument("--no-fallback", action="store_true", help="do not fall back to the local webcam")
    p.add_argument("--model", default=None, help="YOLO weights (default yolov8n.pt)")
    p.add_argument("--conf", type=float, default=None, help="person confidence threshold")
    p.add_argument("--device", default=None, help="inference device, e.g. cpu or cuda:0")
    p.add_argument("--db", default=None, help="SQLite DB path (default data/passengers.db)")
    p.add_argument("--tickets", default=None, help="tickets.json path (default data/tickets.json)")
    p.add_argument("--no-qr", action="store_true", help="disable QR ticket scanning in camera frames")
    p.add_argument("--no-display", action="store_true", help="run without the OpenCV preview window")
    p.add_argument("--max-frames", type=int, default=0, help="stop after N frames (0 = unlimited)")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_app_config(args.config)
    try:
        source, pipeline = build_pipeline(args, cfg)
    except ValueError as exc:  # e.g. invalid entry_line settings
        logger.error("Invalid configuration: %s", exc)
        return 1
    return run(source, pipeline, display=not args.no_display, max_frames=args.max_frames)


if __name__ == "__main__":
    raise SystemExit(main())
