"""Video input layer for AI Ticketless Passenger Detection.

Provides a single ``VideoSource`` class that yields BGR frames from either:
  * an Android IP camera stream (e.g. the "IP Webcam" app: http://<phone-ip>:8080/video)
  * a local webcam (integer device index) as an optional fallback

This module contains camera logic only - no detection, tracking or storage.
"""

from __future__ import annotations

import logging
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional, Union
from urllib.parse import urlparse

import cv2
import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_STREAM_URL = "http://192.0.0.4:8080/video"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"

SourceType = Union[str, int]


class CameraConnectionError(RuntimeError):
    """Raised when no camera source (primary or fallback) can be opened."""


@dataclass
class CameraConfig:
    source: SourceType = DEFAULT_STREAM_URL
    fallback_to_webcam: bool = True
    webcam_index: int = 0
    open_timeout_sec: float = 10.0
    read_timeout_sec: float = 5.0
    reconnect_attempts: int = 3
    reconnect_delay_sec: float = 2.0
    frame_width: Optional[int] = None
    frame_height: Optional[int] = None

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "CameraConfig":
        data = dict(data or {})
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        unknown = set(data) - known
        if unknown:
            logger.warning("Ignoring unknown camera config keys: %s", sorted(unknown))
        cfg = cls(**{k: v for k, v in data.items() if k in known})
        cfg.source = _normalize_source(cfg.source)
        cfg.webcam_index = int(cfg.webcam_index)
        return cfg


def load_camera_config(path: Union[str, Path, None] = None) -> CameraConfig:
    """Read the ``camera`` section of config.yaml. Falls back to defaults if absent."""
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not path.is_file():
        logger.warning("Config file %s not found; using default camera settings.", path)
        return CameraConfig()
    try:
        import yaml  # PyYAML
    except ImportError:
        logger.warning("PyYAML not installed; using default camera settings.")
        return CameraConfig()
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return CameraConfig.from_dict(data.get("camera"))


def _normalize_source(source: SourceType) -> SourceType:
    """Convert numeric strings ("0") to int webcam indices; strip URLs."""
    if isinstance(source, int):
        return source
    text = str(source).strip()
    return int(text) if text.isdigit() else text


def _is_network_url(source: SourceType) -> bool:
    return isinstance(source, str) and urlparse(source).scheme.lower() in {
        "http", "https", "rtsp", "rtmp",
    }


def _network_help(url: str, reason: str) -> str:
    return (
        f"Could not connect to the phone camera stream at {url} ({reason}).\n"
        "  - Make sure the phone and the laptop are connected to the SAME Wi-Fi network/hotspot.\n"
        "  - Make sure the IP camera app is running on the phone and the server is started.\n"
        "  - Open the URL in a laptop browser to confirm it is reachable.\n"
        "  - Check the IP address shown in the phone app and update camera.source in config/config.yaml."
    )


def _check_host_reachable(url: str, timeout: float) -> Optional[str]:
    """Quick TCP probe so unreachable phones fail fast with a clear reason."""
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return "invalid URL"
    default_port = {"https": 443, "rtsp": 554, "rtmp": 1935}.get(parsed.scheme.lower(), 80)
    port = parsed.port or default_port
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except socket.timeout:
        return f"connection to {host}:{port} timed out"
    except OSError as exc:
        return f"cannot reach {host}:{port}: {exc.strerror or exc}"


class VideoSource:
    """Frame provider wrapping ``cv2.VideoCapture`` with fallback and reconnect.

    Usage::

        with VideoSource.from_config() as cam:
            for frame in cam.frames():
                ...
    """

    def __init__(self, config: Optional[CameraConfig] = None) -> None:
        self.config = config or CameraConfig()
        self._cap: Optional[cv2.VideoCapture] = None
        self.active_source: Optional[SourceType] = None

    @classmethod
    def from_config(cls, path: Union[str, Path, None] = None) -> "VideoSource":
        return cls(load_camera_config(path))

    # ------------------------------------------------------------------ open
    def open(self) -> "VideoSource":
        """Open the primary source, falling back to the webcam if configured."""
        primary = self.config.source
        try:
            self._cap = self._open_source(primary)
            self.active_source = primary
        except CameraConnectionError as primary_err:
            fallback = self.config.webcam_index
            if not self.config.fallback_to_webcam or primary == fallback:
                raise
            logger.error("%s", primary_err)
            logger.warning("Falling back to local webcam (index %s).", fallback)
            try:
                self._cap = self._open_source(fallback)
                self.active_source = fallback
            except CameraConnectionError as fallback_err:
                raise CameraConnectionError(
                    f"{primary_err}\n\nWebcam fallback also failed: {fallback_err}"
                ) from fallback_err
        logger.info("Camera opened: %s", self.active_source)
        return self

    def _open_source(self, source: SourceType) -> cv2.VideoCapture:
        cfg = self.config
        if _is_network_url(source):
            assert isinstance(source, str)
            reason = _check_host_reachable(source, cfg.open_timeout_sec)
            if reason:
                raise CameraConnectionError(_network_help(source, reason))
            params = [
                cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, int(cfg.open_timeout_sec * 1000),
                cv2.CAP_PROP_READ_TIMEOUT_MSEC, int(cfg.read_timeout_sec * 1000),
            ]
            cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG, params)
            if not cap.isOpened():  # retry with default backend
                cap.release()
                cap = cv2.VideoCapture(source)
            if not cap.isOpened():
                cap.release()
                raise CameraConnectionError(
                    _network_help(source, "host reachable but video stream could not be opened; "
                                          "check the URL path, e.g. /video")
                )
        elif isinstance(source, int):
            cap = cv2.VideoCapture(source)
            if not cap.isOpened():
                cap.release()
                raise CameraConnectionError(
                    f"Could not open local webcam at index {source}. "
                    "Check that it is connected and not used by another application."
                )
        else:
            # Local video file path (useful for offline testing)
            if not Path(source).exists():
                raise CameraConnectionError(f"Video source not found: {source}")
            cap = cv2.VideoCapture(source)
            if not cap.isOpened():
                cap.release()
                raise CameraConnectionError(f"Could not open video file: {source}")

        if cfg.frame_width:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(cfg.frame_width))
        if cfg.frame_height:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(cfg.frame_height))
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # reduce latency on live streams

        ok, _ = cap.read()
        if not ok:
            cap.release()
            if _is_network_url(source):
                raise CameraConnectionError(_network_help(str(source), "no frames received"))
            raise CameraConnectionError(f"Source {source!r} opened but returned no frames.")
        return cap

    # ------------------------------------------------------------------ read
    @property
    def is_opened(self) -> bool:
        return self._cap is not None and self._cap.isOpened()

    def read(self) -> Optional[np.ndarray]:
        """Return the next BGR frame, reconnecting live sources on failure.

        Returns ``None`` at the end of a video file. Raises
        ``CameraConnectionError`` if a live source is lost and cannot be recovered.
        """
        if self._cap is None:
            self.open()
        assert self._cap is not None
        ok, frame = self._cap.read()
        if ok:
            return frame
        if isinstance(self.active_source, str) and not _is_network_url(self.active_source):
            return None  # end of video file
        return self._reconnect()

    def _reconnect(self) -> np.ndarray:
        source = self.active_source
        assert source is not None
        attempts = max(1, int(self.config.reconnect_attempts))
        last_err: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            logger.warning("Camera stream lost; reconnecting (%d/%d)...", attempt, attempts)
            self._release_cap()
            time.sleep(self.config.reconnect_delay_sec)
            try:
                self._cap = self._open_source(source)
                ok, frame = self._cap.read()
                if ok:
                    logger.info("Camera reconnected: %s", source)
                    return frame
            except CameraConnectionError as exc:
                last_err = exc
        self._release_cap()
        raise CameraConnectionError(
            f"Lost connection to camera {source!r} after {attempts} reconnect attempts."
            + (f"\n{last_err}" if last_err else "")
        )

    def frames(self) -> Iterator[np.ndarray]:
        """Yield frames until the source ends."""
        while True:
            frame = self.read()
            if frame is None:
                return
            yield frame

    # --------------------------------------------------------------- cleanup
    def _release_cap(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def release(self) -> None:
        self._release_cap()
        self.active_source = None

    def __enter__(self) -> "VideoSource":
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.release()

    def __del__(self) -> None:  # best-effort cleanup
        try:
            self._release_cap()
        except Exception:
            pass


def _preview() -> None:
    """Standalone camera check: ``python -m app.camera.video_source``. Press q to quit."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        with VideoSource.from_config() as cam:
            for frame in cam.frames():
                cv2.imshow(f"Camera preview - {cam.active_source}", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
    except CameraConnectionError as exc:
        logger.error("%s", exc)
        raise SystemExit(1)
    finally:
        cv2.destroyAllWindows()


if __name__ == "__main__":
    _preview()
