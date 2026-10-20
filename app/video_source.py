"""Video input layer: one ``VideoSource`` for the three configurable inputs.

``camera.source_type`` in config/config.yaml (or ``--source`` / ``TPD_SOURCE``):

  * ``"phone"``  - Android IP camera stream (default http://192.0.0.4:8080/video).
    The phone and the computer must be on the same reachable network and the
    phone's streaming app (e.g. "IP Webcam") must be running.
  * ``"webcam"`` - laptop/USB webcam (integer device index, default 0)
  * ``"video"``  - recorded video file (.mp4 / .avi / .mov / .mkv)

Every source yields BGR ``np.ndarray`` frames, so the detection pipeline does
not care where frames come from. Camera logic only: no detection or storage.
"""

from __future__ import annotations

import logging
import socket
import time
from pathlib import Path
from typing import Iterator, Optional, Sequence, Tuple, Union
from urllib.parse import urlparse

import cv2
import numpy as np

from app.config import DEFAULT_CONFIG_PATH, PROJECT_ROOT, load_config
from app.settings_schema import (
    DEFAULT_STREAM_URL,
    DEFAULT_VIDEO_PATH,
    SOURCE_TYPES,
    VIDEO_EXTENSIONS,
    CameraConfig,
    SourceType,
    _is_network_url,
)

__all__ = [
    "DEFAULT_CONFIG_PATH", "DEFAULT_STREAM_URL", "DEFAULT_VIDEO_PATH", "PROJECT_ROOT", "SOURCE_TYPES",
    "VIDEO_EXTENSIONS", "CameraConfig", "CameraConnectionError", "VideoSource", "check_video_file",
    "choose_video_file", "load_camera_config", "redact_url", "resolve_video_path",
]

logger = logging.getLogger(__name__)


class CameraConnectionError(RuntimeError):
    """Raised when a camera source (primary or fallback) cannot be opened or read."""


def load_camera_config(path: Union[str, Path, None] = None) -> CameraConfig:
    """The ``camera`` section of the full config (defaults < yaml < .env < env)."""
    return load_config(path).camera


# ------------------------------------------------------------------ helpers
def redact_url(url: str) -> str:
    """Remove ``user:password@`` from a URL before printing it."""
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return parsed._replace(netloc=f"***@{host}").geturl()
    return url


def resolve_video_path(path: Union[str, Path]) -> Path:
    """Relative video paths are resolved against the project root (never the CWD)."""
    p = Path(path).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p


def check_video_file(source: Union[str, Path], allowed_extensions: Sequence[str] = VIDEO_EXTENSIONS) -> Path:
    """Check a video path before any heavy work: extension first, then existence.

    Messages quote ``source`` exactly as configured. Returns the resolved path.
    """
    allowed = [e.lower() for e in allowed_extensions]
    suffix = Path(str(source)).suffix
    if suffix.lower() not in allowed:
        raise CameraConnectionError(
            f"Unsupported video file type '{suffix}': {source}\nAllowed: {', '.join(allowed)}"
        )
    resolved = resolve_video_path(source)
    if not resolved.is_file():
        raise CameraConnectionError(f"Video file not found:\n{source}")
    return resolved


def choose_video_file(allowed_extensions: Sequence[str] = VIDEO_EXTENSIONS,
                      initial_dir: Union[str, Path, None] = None) -> Optional[str]:
    """Open a tkinter file dialog filtered to the allowed video extensions.

    Returns the selected path, or None on Cancel or when tkinter is unavailable.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        logger.error("File dialog unavailable (tkinter missing: %s).", exc)
        return None
    patterns = " ".join(f"*{e}" for e in allowed_extensions)
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        logger.error("File dialog unavailable: %s", exc)
        return None
    try:
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askopenfilename(
            title="Select a recorded video",
            initialdir=str(initial_dir or PROJECT_ROOT / "data" / "videos"),
            filetypes=[("Video files", patterns), ("All files", "*.*")],
        )
    finally:
        root.destroy()
    return selected or None


def _phone_help(url: str, reason: str) -> str:
    return (
        f"Unable to connect to Android phone camera:\n{redact_url(url)}\n"
        f"Reason: {reason}\n\n"
        "Check that:\n"
        "1. The phone and computer are on the same Wi-Fi network.\n"
        "2. The camera streaming application is running.\n"
        "3. The IP address is correct (camera.phone.url in config/config.yaml, --url or TPD_PHONE_URL).\n"
        "4. The /video stream is available."
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


# ---------------------------------------------------------------- VideoSource
class VideoSource:
    """Frame provider wrapping ``cv2.VideoCapture`` for webcam / phone / video.

    Usage::

        cam = VideoSource("phone")          # or "webcam" / "video"; reads config.yaml
        cam.open()
        while True:
            ret, frame = cam.read()
            if not ret:
                break                        # end of recorded video
        cam.release()

        with VideoSource.from_config() as cam:
            for frame in cam.frames():
                ...

    ``read()`` raises :class:`CameraConnectionError` if a live source (webcam or
    phone) is lost and cannot be recovered within the configured reconnect attempts.
    """

    def __init__(
        self,
        config: Union[CameraConfig, str, None] = None,
        config_path: Union[str, Path, None] = None,
    ) -> None:
        if isinstance(config, CameraConfig):
            self.config = config
        else:
            self.config = load_camera_config(config_path)
            if config is not None:
                self.config.select(config)
        self._cap: Optional[cv2.VideoCapture] = None
        self.active_source: Optional[SourceType] = None
        self.active_type: Optional[str] = None
        self.finished = False  # True once a recorded video reached its end
        self._pending_frame: Optional[np.ndarray] = None

    @classmethod
    def from_config(cls, path: Union[str, Path, None] = None) -> "VideoSource":
        return cls(load_camera_config(path))

    # ----------------------------------------------------------- describe
    @property
    def source_type(self) -> str:
        return self.active_type or self.config.source_type

    def describe(self) -> Tuple[str, str]:
        """Return a printable ``(label, value)`` pair, e.g. ``("Source", url)``."""
        st = self.source_type
        if st == "webcam":
            index = self.active_source if isinstance(self.active_source, int) else self.config.webcam_index
            return "Camera index", str(index)
        if st == "video":
            return "Source", str(self.config.video_path)
        return "Source", redact_url(self.config.phone_url)

    def _log_source(self) -> None:
        st = self.source_type
        if st == "webcam":
            logger.info("Camera source: webcam | Index: %s", self.describe()[1])
        elif st == "video":
            logger.info("Camera source: recorded video | Video file: %s", self.config.video_path)
        else:
            logger.info("Camera source: Android phone | URL: %s", redact_url(self.config.phone_url))

    # --------------------------------------------------------------- open
    def open(self) -> "VideoSource":
        """Open the selected source (phone may fall back to the webcam if enabled)."""
        if self._cap is not None:
            return self
        cfg = self.config
        st = cfg.source_type
        self.finished = False
        try:
            self._cap = self._open_source(st, cfg.source)
            self.active_type, self.active_source = st, cfg.source
        except CameraConnectionError as primary_err:
            if st != "phone" or not cfg.fallback_to_webcam:
                raise
            logger.error("%s", primary_err)
            logger.warning("Falling back to local webcam (index %s) because camera.fallback_to_webcam "
                           "is enabled.", cfg.webcam_index)
            try:
                self._cap = self._open_source("webcam", cfg.webcam_index)
                self.active_type, self.active_source = "webcam", cfg.webcam_index
            except CameraConnectionError as fallback_err:
                raise CameraConnectionError(
                    f"{primary_err}\n\nWebcam fallback also failed: {fallback_err}"
                ) from fallback_err
        self._log_source()
        return self

    def _open_source(self, source_type: str, source: SourceType) -> cv2.VideoCapture:
        cfg = self.config
        if source_type == "phone":
            url = str(source)
            if not _is_network_url(url):
                raise CameraConnectionError(_phone_help(url, "not an http/https/rtsp/rtmp URL"))
            reason = _check_host_reachable(url, cfg.open_timeout_sec)
            if reason:
                raise CameraConnectionError(_phone_help(url, reason))
            params = [
                cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, int(cfg.open_timeout_sec * 1000),
                cv2.CAP_PROP_READ_TIMEOUT_MSEC, int(cfg.read_timeout_sec * 1000),
            ]
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG, params)
            if not cap.isOpened():  # retry with default backend
                cap.release()
                cap = cv2.VideoCapture(url)
            if not cap.isOpened():
                cap.release()
                raise CameraConnectionError(
                    _phone_help(url, "host reachable but the video stream could not be opened")
                )
        elif source_type == "webcam":
            index = int(source)
            cap = cv2.VideoCapture(index)
            if not cap.isOpened():
                cap.release()
                raise CameraConnectionError(
                    f"Could not open webcam at index {index}. Check that it is connected, "
                    "not used by another application, or set camera.webcam.index in config/config.yaml."
                )
        elif source_type == "video":
            path = check_video_file(str(source), cfg.allowed_extensions)
            cap = cv2.VideoCapture(str(path))
            if not cap.isOpened():
                cap.release()
                raise CameraConnectionError(
                    f"Could not open video file (unsupported codec or corrupt file): {source}"
                )
        else:
            raise CameraConnectionError(f"Unknown source type {source_type!r}")

        if source_type != "video":
            if cfg.frame_width:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(cfg.frame_width))
            if cfg.frame_height:
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(cfg.frame_height))
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # reduce latency on live streams

        ok, frame = cap.read()  # make sure frames actually arrive
        if not ok:
            cap.release()
            if source_type == "phone":
                raise CameraConnectionError(_phone_help(str(source), "no frames received"))
            if source_type == "video":
                raise CameraConnectionError(f"Video file contains no readable frames: {source}")
            raise CameraConnectionError(f"{source_type} source {source!r} opened but returned no frames.")
        self._pending_frame = frame  # don't drop the first frame
        return cap

    # --------------------------------------------------------------- read
    def is_opened(self) -> bool:
        return self._cap is not None and self._cap.isOpened()

    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        """Return ``(ret, frame)``.

        ``ret`` is False at the end of a recorded video (``self.finished`` is set).
        Live sources are reconnected on failure; if that fails,
        :class:`CameraConnectionError` is raised.
        """
        if self.finished:
            return False, None
        if self._cap is None:
            self.open()
        assert self._cap is not None
        if self._pending_frame is not None:
            frame, self._pending_frame = self._pending_frame, None
            return True, frame
        ok, frame = self._cap.read()
        if ok and frame is not None:
            return True, frame
        if self.active_type == "video":
            self.finished = True
            return False, None
        return True, self._reconnect()

    def _reconnect(self) -> np.ndarray:
        source_type, source = self.active_type, self.active_source
        assert source_type is not None and source is not None
        label = redact_url(source) if isinstance(source, str) else source
        if not self.config.reconnect_enabled:
            self._release_cap()
            raise CameraConnectionError(
                f"Lost connection to {source_type} source {label!r} (reconnect disabled)."
            )
        attempts = max(1, int(self.config.reconnect_attempts))
        last_err: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            logger.warning("Camera stream lost; reconnecting (%d/%d)...", attempt, attempts)
            self._release_cap()  # never keep more than one VideoCapture alive
            time.sleep(self.config.reconnect_delay_sec)
            try:
                self._cap = self._open_source(source_type, source)
                frame, self._pending_frame = self._pending_frame, None
                logger.info("Camera reconnected: %s", label)
                return frame
            except CameraConnectionError as exc:
                last_err = exc
        self._release_cap()
        raise CameraConnectionError(
            f"Lost connection to {source_type} source {label!r} after {attempts} reconnect attempts."
            + (f"\n{last_err}" if last_err else "")
        )

    def frames(self) -> Iterator[np.ndarray]:
        """Yield frames until the source ends."""
        while True:
            ret, frame = self.read()
            if not ret:
                return
            yield frame

    # ------------------------------------------------------------ cleanup
    def _release_cap(self) -> None:
        self._pending_frame = None
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
    """Standalone camera check: ``python -m app.video_source [webcam|phone|video]``."""
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        cam = VideoSource(sys.argv[1] if len(sys.argv) > 1 else None)
        with cam:
            for frame in cam.frames():
                cv2.imshow(f"Camera preview - {cam.source_type} (press q to quit)", frame)
                if cv2.waitKey(1) & 0xFF in (ord("q"), ord("Q")):
                    break
        if cam.finished:
            print("Recorded video finished.")
    except (CameraConnectionError, ValueError) as exc:
        logger.error("%s", exc)
        raise SystemExit(1)
    finally:
        cv2.destroyAllWindows()


if __name__ == "__main__":
    _preview()
