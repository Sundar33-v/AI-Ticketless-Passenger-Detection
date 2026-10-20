"""Detection process entry point: video source -> YOLO/ByteTrack -> boarding rules -> SQLite.

Run from the project root (or via scripts\\run_detection.bat)::

    python -m app.main --source phone [--url http://<ip>:8080/video]
    python -m app.main --source webcam [--camera-index 1]
    python -m app.main --source video --path data/videos/bus_video.mp4   (or --choose-file)

The phone and the computer must be on the same reachable network and the
phone's camera streaming app must be running. Press q in the video window
(or Ctrl+C) to stop.

Exit codes: 0 finished/stopped, 1 configuration/usage, 2 video source,
3 database, 4 model, 5 unexpected error.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sqlite3
from pathlib import Path
from typing import Optional, Sequence

from app.alert_service import AlertService
from app.config import ConfigError, configure_logging, load_config, resolve_db_path, resolve_path
from app.database import Database, DatabaseError, utc_now
from app.detector import ModelNotFoundError, load_model
from app.passenger_service import BoardingRules, PassengerService
from app.pipeline import DetectionPipeline
from app.qr_service import QRScanner
from app.settings_schema import SOURCE_TYPES, AppConfig, CameraConfig, DetectionConfig, LoggingConfig
from app.ticket_validator import TicketValidator, build_journey
from app.tracker import PersonTracker, prepare_tracker_yaml
from app.video_source import CameraConnectionError, VideoSource, check_video_file, choose_video_file, redact_url

logger = logging.getLogger("app.main")

WINDOW_TITLE = "AI Ticketless Passenger Detection (press q to quit)"
EXIT_OK, EXIT_CONFIG, EXIT_SOURCE, EXIT_DATABASE, EXIT_MODEL, EXIT_UNEXPECTED = 0, 1, 2, 3, 4, 5


# ------------------------------------------------------------------------ CLI
def _camera_index(value: str) -> int:
    index = int(value)
    if not 0 <= index <= 16:
        raise argparse.ArgumentTypeError("camera index must be 0..16")
    return index


def _confidence(value: str) -> float:
    conf = float(value)
    if not 0.05 <= conf <= 0.95:
        raise argparse.ArgumentTypeError("confidence must be 0.05..0.95")
    return conf


def _non_negative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return number


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m app.main",
                                description="AI Ticketless Passenger Detection - detection process")
    p.add_argument("--config", default=None, help="config YAML (default config/config.yaml or $TPD_CONFIG)")
    p.add_argument("--source", default=None,
                   help="phone | webcam | video (a stream URL, webcam index or video path also works)")
    p.add_argument("--url", "--phone-url", dest="url", default=None,
                   help="Android camera stream URL (default http://192.0.0.4:8080/video)")
    p.add_argument("--camera-index", "--webcam-index", dest="camera_index", type=_camera_index, default=None,
                   help="webcam device index (default 0)")
    p.add_argument("--path", "--video", dest="path", default=None,
                   help="recorded video file (.mp4/.avi/.mov/.mkv); implies --source video")
    p.add_argument("--choose-file", action="store_true", help="pick the recorded video in a file dialog")
    p.add_argument("--fallback-webcam", action="store_true",
                   help="if the phone camera cannot be opened, use the webcam instead")
    p.add_argument("--model", default=None, help="YOLO weights (default models/yolov8n.pt)")
    p.add_argument("--conf", type=_confidence, default=None, help="person confidence threshold (0.05..0.95)")
    p.add_argument("--device", default=None, help="cpu | auto | cuda:N")
    p.add_argument("--db", default=None, help="SQLite database path (default data/ticketless.db)")
    p.add_argument("--station", default=None, help="current station for ticket checks (must be on the route)")
    p.add_argument("--trip-id", default=None, help="trip identifier (default <route>-<direction>-<date>)")
    p.add_argument("--no-qr", action="store_true", help="disable QR ticket scanning in camera frames")
    p.add_argument("--no-display", action="store_true", help="run without the OpenCV window")
    p.add_argument("--max-frames", type=_non_negative, default=0, help="stop after N frames (0 = unlimited)")
    p.add_argument("--export-mot", default=None, metavar="PATH",
                   help="write MOTChallenge-style tracks (frame,id,x,y,w,h,conf,-1,-1,-1) for evaluation")
    p.add_argument("--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args(argv)


def apply_source_overrides(camera_cfg: CameraConfig, args: argparse.Namespace) -> None:
    """Apply --source/--url/--camera-index/--path on top of the config (in memory, this run only)."""
    path = getattr(args, "path", None)
    if args.source is not None:
        value = str(args.source).strip()
        if value.lower() in SOURCE_TYPES:
            camera_cfg.select(value)
        else:  # legacy: --source <url | webcam index | video file>
            camera_cfg.source = value
    elif path:
        camera_cfg.select("video")
    if path:
        camera_cfg.video_path = path
    if getattr(args, "url", None):
        camera_cfg.phone_url = args.url
    if getattr(args, "camera_index", None) is not None:
        camera_cfg.webcam_index = int(args.camera_index)
    if getattr(args, "fallback_webcam", False):
        camera_cfg.fallback_to_webcam = True


def _apply_cli_config(cfg: AppConfig, args: argparse.Namespace) -> AppConfig:
    detection = dataclasses.asdict(cfg.detection)
    for key, value in (("model_path", args.model), ("confidence", args.conf), ("device", args.device)):
        if value is not None:
            detection[key] = value
    changes = {"detection": DetectionConfig.from_dict(detection)}
    if args.log_level:
        changes["logging"] = LoggingConfig.from_dict({**dataclasses.asdict(cfg.logging), "level": args.log_level})
    return dataclasses.replace(cfg, **changes)


def source_label(camera: CameraConfig, source_type: Optional[str] = None) -> str:
    """What is stored in detection_sessions.source_label (no credentials, file name only)."""
    source_type = source_type or camera.source_type
    if source_type == "webcam":
        return f"webcam {camera.webcam_index}"
    if source_type == "video":
        return Path(str(camera.video_path)).name
    return redact_url(camera.phone_url)


# ------------------------------------------------------------------------ run
def run(source: VideoSource, pipeline: DetectionPipeline, *, display: bool, max_frames: int = 0) -> int:
    """Frame loop. Raises CameraConnectionError if a live source is lost for good."""
    import cv2

    logger.info("Processing %s source. %s", source.source_type,
                "Press q in the video window to stop." if display else "Press Ctrl+C to stop.")
    try:
        for frame in source.frames():
            pipeline.process_frame(frame, utc_now())
            if display:
                try:
                    cv2.imshow(WINDOW_TITLE, pipeline.render(frame))
                    if cv2.waitKey(1) & 0xFF in (ord("q"), ord("Q")):
                        logger.info("Stopped by user (q).")
                        break
                except cv2.error as exc:
                    logger.warning("Display unavailable (%s); continuing headless.", exc)
                    display = False
            if max_frames and pipeline.frame_count >= max_frames:
                break
    except KeyboardInterrupt:
        logger.info("Stopped by user.")
    if source.finished:
        logger.info("Recorded video finished.")
    return EXIT_OK


def _print_banner(cfg: AppConfig, camera: CameraConfig, journey, db_path) -> None:
    line = "=" * 56
    print(f"{line}\nAI TICKETLESS PASSENGER DETECTION - detection process\n{line}\n"
          f"Source type: {camera.source_type}\nSource: {source_label(camera)}\n"
          f"Model: {resolve_path(cfg.detection.model_path)}\nPerson confidence: {cfg.detection.confidence}\n"
          f"Route: {journey.route_id} {journey.direction} | Station: {journey.current_station or '(not set)'} | "
          f"Trip: {journey.trip_id}\nDatabase: {db_path}\n{line}", flush=True)


def _session(cfg: AppConfig, camera: CameraConfig, args: argparse.Namespace, model, tracker_yaml,
             db: Database, db_path) -> int:
    """Steps 9-12: journey, session row, open the source, loop, clean up."""
    session_start = utc_now()
    journey = build_journey(cfg, now=session_start, station=args.station, trip_id=args.trip_id)
    _print_banner(cfg, camera, journey, db_path)
    service = PassengerService(db, cfg)
    source: Optional[VideoSource] = None
    mot = None
    status, error, code = "STOPPED", None, EXIT_OK
    pipeline: Optional[DetectionPipeline] = None
    try:
        service.create_session(camera.source_type, source_label(camera), journey.current_station, journey.trip_id)
        source = VideoSource(camera)
        try:
            source.open()
        except CameraConnectionError as exc:
            logger.error("%s", exc)
            status, error, code = "FAILED", str(exc), EXIT_SOURCE
            return code
        if source.source_type != camera.source_type:  # phone -> webcam fallback (--fallback-webcam)
            service.update_source(source.source_type, source_label(camera, source.source_type))
        if args.export_mot:
            mot_path = resolve_path(args.export_mot)
            mot_path.parent.mkdir(parents=True, exist_ok=True)
            mot = mot_path.open("w", encoding="utf-8", newline="\n")
        pipeline = DetectionPipeline(
            tracker=PersonTracker(model, cfg.tracking, cfg.detection, tracker_yaml), service=service,
            boarding_rules=BoardingRules(cfg.boarding.entry_line, cfg.boarding.min_frames_seen),
            validator=TicketValidator(tz=cfg.app.timezone), alerts=AlertService(), journey=journey, cfg=cfg,
            scanner=None if args.no_qr or not cfg.qr.enabled else QRScanner(), mot_writer=mot)
        try:
            code = run(source, pipeline, display=not args.no_display, max_frames=args.max_frames)
        except CameraConnectionError as exc:
            logger.error("%s", exc)
            status, error, code = "FAILED", str(exc), EXIT_SOURCE
        except (sqlite3.Error, DatabaseError) as exc:
            logger.error("Database error: %s", exc)
            status, error, code = "FAILED", str(exc), EXIT_DATABASE
        return code
    except BaseException as exc:
        status, error = "FAILED", f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if source is not None:
            source.release()
        if not args.no_display:
            _destroy_windows()
        if mot is not None:
            mot.close()
        try:
            frames = pipeline.frame_count if pipeline else None
            service.end_session(status, error, frames=frames, fps=pipeline.fps if pipeline else None)
        except Exception as exc:  # best effort only; the primary error keeps propagating
            logger.error("Could not close the detection session: %s", exc)
        service.close()
        if pipeline is not None:
            logger.info("Processed %d frames; %d boarding events this session.", pipeline.frame_count,
                        len(service.boardings))


def _destroy_windows() -> None:
    try:
        import cv2

        cv2.destroyAllWindows()
    except Exception:
        pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:                                                           # 2-4: config, logging, CLI overrides
        cfg = _apply_cli_config(load_config(args.config), args)
        configure_logging(cfg.logging, "app.log")
        camera = cfg.camera
        apply_source_overrides(camera, args)
        if args.choose_file:
            chosen = choose_video_file(camera.allowed_extensions)
            if not chosen:
                logger.error("No video file selected")
                return EXIT_CONFIG
            camera.select("video")
            camera.video_path = chosen
        db_path = resolve_db_path(args.db, cfg)
    except ConfigError as exc:
        logging.basicConfig(level=logging.INFO)
        logger.error("Configuration error: %s", exc)
        return EXIT_CONFIG

    if camera.source_type == "video":                              # 5: fail fast, before model/DB
        try:
            check_video_file(camera.video_path, camera.allowed_extensions)
        except CameraConnectionError as exc:
            logger.error("%s", exc)
            return EXIT_SOURCE
    try:                                                           # 6: tracker YAML
        runtime_dir = resolve_path(cfg.runtime.preview_path).parent
        tracker_yaml = prepare_tracker_yaml(cfg.tracking, cfg.detection, runtime_dir)
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        return EXIT_CONFIG
    try:                                                           # 7: model
        model = load_model(cfg.detection)
    except ModelNotFoundError as exc:
        logger.error("%s", exc)
        return EXIT_MODEL
    try:                                                           # 8: database
        db = Database(db_path, cfg.database.busy_timeout_ms)
        db.initialize()
    except (sqlite3.Error, DatabaseError, OSError) as exc:
        logger.error("Database error (%s): %s", db_path, exc)
        return EXIT_DATABASE
    try:                                                           # 9-12
        return _session(cfg, camera, args, model, tracker_yaml, db, db_path)
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        return EXIT_CONFIG
    except ValueError as exc:  # e.g. --station not on the route
        logger.error("%s", exc)
        return EXIT_CONFIG
    except (sqlite3.Error, DatabaseError) as exc:
        logger.error("Database error: %s", exc)
        return EXIT_DATABASE
    except Exception:
        logger.critical("Unexpected error", exc_info=True)
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())
