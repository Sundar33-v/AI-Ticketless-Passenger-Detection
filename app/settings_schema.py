"""Configuration schema: section dataclasses, built-in defaults and validation.

Each section's ``from_dict(data, *, section)`` applies defaults, logs a WARNING
for unknown keys and raises :class:`ConfigError` starting with the dotted key
(``"camera.webcam.index: ..."``). Imports nothing from ``app`` (no cycles); the
camera constants (``SOURCE_TYPES``, ``DEFAULT_STREAM_URL`` ...) live only here.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Tuple, Union
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

DEFAULT_STREAM_URL = "http://192.0.0.4:8080/video"
DEFAULT_VIDEO_PATH = "data/videos/bus_video.mp4"
SOURCE_TYPES = ("phone", "webcam", "video")
VIDEO_EXTENSIONS = (".mp4", ".avi", ".mov", ".mkv")
STREAM_SCHEMES = ("http", "https", "rtsp", "rtmp")
DEFAULT_STATIONS = ("Central Bus Stand", "Market Road", "City Hospital", "College Gate", "Railway Station", "Airport")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
JOURNEY_DIRECTIONS = ("outbound", "inbound")
DASHBOARD_PERIODS = ("today", "last_24_hours", "yesterday", "last_7_days", "all", "custom")
BOARDING_DIRECTIONS = ("top_to_bottom", "bottom_to_top", "left_to_right", "right_to_left")
ENTRY_LINE_DEFAULTS: Mapping[str, Any] = {
    "orientation": "horizontal", "position": 0.5, "normalized": True, "span_start": None, "span_end": None,
    "entry_direction": "top_to_bottom", "cooldown_seconds": 2.0, "hysteresis_px": 5.0,
    "anchor": "bottom_center", "track_timeout_seconds": 30.0,
}
_DEVICE_RE = re.compile(r"^(cpu|auto|cuda:\d+)$")

SourceType = Union[str, int]


class ConfigError(ValueError):
    """Invalid configuration; message starts with the dotted key."""


# --------------------------------------------------------------------- helpers
def _mapping(data: Any, section: str) -> dict:
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise ConfigError(f"{section}: expected a mapping, got {type(data).__name__}")
    return dict(data)


def _warn_unknown(data: Mapping[str, Any], known: Iterable[str], section: str) -> None:
    unknown = sorted(set(data) - set(known))
    if unknown:
        logger.warning("Ignoring unknown config keys in %s: %s", section, unknown)


def _num(data: Mapping[str, Any], key: str, default: Any, section: str, lo: float, hi: float, *,
         integer: bool = False, allow_none: bool = False) -> Any:
    name, value = f"{section}.{key}", data.get(key, default)
    if value is None and allow_none:
        return None
    kind = "an integer" if integer else "a finite number"
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or (integer and isinstance(value, float) and not value.is_integer())):
        raise ConfigError(f"{name}: expected {kind}, got {value!r}")
    value = int(value) if integer else float(value)
    if not lo <= value <= hi:
        raise ConfigError(f"{name}: {value} is out of range {lo}..{hi}")
    return value


def _bool(data: Mapping[str, Any], key: str, default: bool, section: str) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{section}.{key}: expected true or false, got {value!r}")
    return value


def _str(data: Mapping[str, Any], key: str, default: Optional[str], section: str, *,
         allow_none: bool = False) -> Optional[str]:
    value = data.get(key, default)
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value.strip():
        if allow_none and isinstance(value, str):
            return None  # empty string means "not set"
        raise ConfigError(f"{section}.{key}: expected a non-empty string, got {value!r}")
    return value.strip()


def _choice(data: Mapping[str, Any], key: str, default: str, section: str, choices: Tuple[str, ...],
            *, upper: bool = False) -> str:
    value = _str(data, key, default, section)
    assert value is not None
    value = value.upper() if upper else value.lower()
    if value not in choices:
        raise ConfigError(f"{section}.{key}: invalid value {value!r}; expected one of {', '.join(choices)}")
    return value


def _validate_source_type(value: Any, section: str = "camera") -> str:
    text = str(value).strip().lower()
    if text not in SOURCE_TYPES:
        raise ConfigError(
            f"{section}.source_type: invalid value {value!r}; expected one of {', '.join(SOURCE_TYPES)}"
        )
    return text


def _normalize_source(source: SourceType) -> SourceType:
    """Convert numeric strings ("0") to int webcam indices; strip URLs/paths."""
    if isinstance(source, int):
        return source
    text = str(source).strip()
    return int(text) if text.isdigit() else text


def _is_network_url(source: SourceType) -> bool:
    return isinstance(source, str) and urlparse(source).scheme.lower() in STREAM_SCHEMES


def _extensions(value: Any, name: str) -> Tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)) or not value:
        raise ConfigError(f"{name}: expected a non-empty list such as [.mp4, .avi], got {value!r}")
    out = []
    for ext in value:
        if not isinstance(ext, str) or not ext.strip().lstrip("."):
            raise ConfigError(f"{name}: invalid extension {ext!r}")
        ext = ext.strip().lower()
        out.append(ext if ext.startswith(".") else f".{ext}")
    return tuple(dict.fromkeys(out))


def _apply_rule(data: Mapping[str, Any], key: str, default: Any, section: str, rule: tuple) -> Any:
    kind = rule[0]
    if kind in ("num", "int"):
        return _num(data, key, default, section, rule[1], rule[2], integer=kind == "int")
    if kind == "bool":
        return _bool(data, key, default, section)
    if kind in ("str", "optstr"):
        return _str(data, key, default, section, allow_none=kind == "optstr")
    if kind in ("choice", "upper"):
        return _choice(data, key, default, section, rule[1], upper=kind == "upper")
    raise AssertionError(f"unknown rule {rule!r}")  # pragma: no cover


class _Section:
    """Base for sections validated by ``_RULES``: field -> ``("num"|"int", lo, hi)``, ``("bool",)``,
    ``("str",)``, ``("optstr",)`` (None/empty allowed) or ``("choice"|"upper", choices)``.
    Dataclass field defaults are the built-in defaults."""

    _RULES: Mapping[str, tuple] = {}
    _NAME = ""

    @classmethod
    def from_dict(cls, data: Any = None, *, section: Optional[str] = None):
        section = section or cls._NAME
        data = _mapping(data, section)
        _warn_unknown(data, cls._RULES, section)
        values = {key: _apply_rule(data, key, getattr(cls, key), section, rule) for key, rule in cls._RULES.items()}
        return cls(**cls._check(values, section))

    @classmethod
    def _check(cls, values: dict, section: str) -> dict:
        """Hook for cross-key rules inside one section."""
        return values


# --------------------------------------------------------------------- sections
@dataclass(frozen=True)
class AppSection(_Section):
    timezone: str = "Asia/Kolkata"

    _NAME = "app"
    _RULES = {"timezone": ("str",)}

    @classmethod
    def _check(cls, values: dict, section: str) -> dict:
        try:
            ZoneInfo(values["timezone"])
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(f"{section}.timezone: unknown IANA time zone {values['timezone']!r}") from exc
        return values


@dataclass(frozen=True)
class LoggingConfig(_Section):
    level: str = "INFO"
    dir: str = "logs"

    _NAME = "logging"
    _RULES = {"level": ("upper", LOG_LEVELS), "dir": ("str",)}


@dataclass(frozen=True)
class DatabaseConfig(_Section):
    path: str = "data/ticketless.db"
    busy_timeout_ms: int = 5000
    flush_interval_sec: float = 1.0

    _NAME = "database"
    _RULES = {"path": ("str",), "busy_timeout_ms": ("int", 100, 60000), "flush_interval_sec": ("num", 0.2, 10)}


_CAMERA_FLAT_KEYS = ("fallback_to_webcam", "open_timeout_sec", "read_timeout_sec", "frame_width", "frame_height")
_CAMERA_SUB_KEYS = {"phone": ("url",), "webcam": ("index",), "video": ("path", "allowed_extensions"),
                    "reconnect": ("enabled", "attempts", "delay_seconds")}
# Legacy flat camera keys -> (sub-section, key) of the nested layout.
_CAMERA_LEGACY_KEYS = {
    "phone_url": ("phone", "url"), "webcam_index": ("webcam", "index"), "video_path": ("video", "path"),
    "reconnect_enabled": ("reconnect", "enabled"), "reconnect_attempts": ("reconnect", "attempts"),
    "reconnect_delay_sec": ("reconnect", "delay_seconds"),
}


@dataclass
class CameraConfig:
    """Video-source settings (mutable: the CLI applies overrides on top of it)."""

    source_type: str = "phone"
    phone_url: str = DEFAULT_STREAM_URL
    webcam_index: int = 0
    video_path: str = DEFAULT_VIDEO_PATH
    allowed_extensions: Tuple[str, ...] = VIDEO_EXTENSIONS

    fallback_to_webcam: bool = False  # only applies to the phone source
    open_timeout_sec: float = 10.0
    read_timeout_sec: float = 5.0
    reconnect_enabled: bool = True
    reconnect_attempts: int = 5
    reconnect_delay_sec: float = 2.0
    frame_width: Optional[int] = None
    frame_height: Optional[int] = None

    @property
    def source(self) -> SourceType:
        """The concrete OpenCV source for the selected ``source_type``."""
        if self.source_type == "webcam":
            return self.webcam_index
        if self.source_type == "video":
            return self.video_path
        return self.phone_url

    @source.setter
    def source(self, value: SourceType) -> None:
        """Legacy setter: infer ``source_type`` from an index / URL / file path."""
        value = _normalize_source(value)
        if isinstance(value, int):
            self.source_type, self.webcam_index = "webcam", value
        elif _is_network_url(value):
            self.source_type, self.phone_url = "phone", value
        else:
            self.source_type, self.video_path = "video", str(value)

    def select(self, source_type: str) -> None:
        self.source_type = _validate_source_type(source_type)

    @classmethod
    def from_dict(cls, data: Any = None, *, section: str = "camera") -> "CameraConfig":
        """Nested layout (``source_type`` + ``phone``/``webcam``/``video``/``reconnect``)
        or the older flat ``source:`` key."""
        data = _mapping(data, section)
        subs = {name: _mapping(data.pop(name, None), f"{section}.{name}") for name in _CAMERA_SUB_KEYS}
        for legacy, (sub, key) in _CAMERA_LEGACY_KEYS.items():
            if legacy in data:
                subs[sub].setdefault(key, data.pop(legacy))
        legacy_source = data.pop("source", None)
        source_type = data.pop("source_type", None)
        _warn_unknown(data, _CAMERA_FLAT_KEYS, section)
        for name, known in _CAMERA_SUB_KEYS.items():
            _warn_unknown(subs[name], known, f"{section}.{name}")
        phone, webcam, video, reconnect = (subs[n] for n in _CAMERA_SUB_KEYS)

        url = _str(phone, "url", DEFAULT_STREAM_URL, f"{section}.phone")
        if not _is_network_url(url):
            raise ConfigError(f"{section}.phone.url: {url!r} must start with one of "
                              f"{', '.join(s + '://' for s in STREAM_SCHEMES)}")
        cfg = cls(
            phone_url=url,
            webcam_index=_num(webcam, "index", 0, f"{section}.webcam", 0, 16, integer=True),
            video_path=_str(video, "path", DEFAULT_VIDEO_PATH, f"{section}.video"),
            allowed_extensions=_extensions(video.get("allowed_extensions", VIDEO_EXTENSIONS),
                                           f"{section}.video.allowed_extensions"),
            fallback_to_webcam=_bool(data, "fallback_to_webcam", False, section),
            open_timeout_sec=_num(data, "open_timeout_sec", 10.0, section, 1, 60),
            read_timeout_sec=_num(data, "read_timeout_sec", 5.0, section, 1, 60),
            reconnect_enabled=_bool(reconnect, "enabled", True, f"{section}.reconnect"),
            reconnect_attempts=_num(reconnect, "attempts", 5, f"{section}.reconnect", 1, 50, integer=True),
            reconnect_delay_sec=_num(reconnect, "delay_seconds", 2.0, f"{section}.reconnect", 0, 30),
            frame_width=_num(data, "frame_width", None, section, 160, 3840, integer=True, allow_none=True),
            frame_height=_num(data, "frame_height", None, section, 160, 3840, integer=True, allow_none=True),
        )
        if source_type is not None:
            cfg.source_type = _validate_source_type(source_type, section)
        elif legacy_source is not None:  # old config: camera.source: <url|index|path>
            cfg.source = legacy_source
        return cfg


@dataclass(frozen=True)
class DetectionConfig(_Section):
    model_path: str = "models/yolov8n.pt"
    device: str = "cpu"  # cpu | auto | cuda:N
    confidence: float = 0.35  # person acceptance + ByteTrack new_track_thresh (M1)
    iou: float = 0.5
    imgsz: int = 640
    record_interval_sec: float = 1.0

    _NAME = "detection"
    _RULES = {
        "model_path": ("str",), "device": ("str",), "confidence": ("num", 0.05, 0.95), "iou": ("num", 0.1, 0.95),
        "imgsz": ("int", 160, 1920), "record_interval_sec": ("num", 0.1, 60),
    }

    @classmethod
    def _check(cls, values: dict, section: str) -> dict:
        values["device"] = values["device"].lower()
        if not _DEVICE_RE.match(values["device"]):
            raise ConfigError(f"{section}.device: invalid value {values['device']!r}; expected cpu, auto or cuda:N")
        if values["imgsz"] % 32:
            raise ConfigError(f"{section}.imgsz: {values['imgsz']} must be a multiple of 32")
        return values


@dataclass(frozen=True)
class TrackingConfig(_Section):
    tracker_config: str = "config/bytetrack.yaml"
    max_missing_frames: int = 90
    # Confidence passed as conf= to model.track (ByteTrack needs low-score boxes, M1).
    input_confidence: float = 0.1

    _NAME = "tracking"
    _RULES = {"tracker_config": ("str",), "max_missing_frames": ("int", 1, 10000),
              "input_confidence": ("num", 0.01, 0.5)}


@dataclass(frozen=True)
class BoardingConfig:
    min_frames_seen: int = 3
    # Plain dict of EntryLineConfig fields; app.entry_line validates the geometry.
    entry_line: Mapping[str, Any] = field(default_factory=lambda: dict(ENTRY_LINE_DEFAULTS))

    @classmethod
    def from_dict(cls, data: Any = None, *, section: str = "boarding") -> "BoardingConfig":
        data = _mapping(data, section)
        _warn_unknown(data, ("min_frames_seen", "entry_line"), section)
        line = _mapping(data.get("entry_line"), f"{section}.entry_line")
        _warn_unknown(line, ENTRY_LINE_DEFAULTS, f"{section}.entry_line")
        merged = {**ENTRY_LINE_DEFAULTS, **{k: v for k, v in line.items() if k in ENTRY_LINE_DEFAULTS}}
        direction = merged["entry_direction"] = str(merged["entry_direction"]).strip().lower()
        key = f"{section}.entry_line.entry_direction"
        if direction == "both":
            raise ConfigError(f"{key}: 'both' is not allowed for boarding; "
                              "use top_to_bottom, bottom_to_top, left_to_right or right_to_left")
        if direction not in BOARDING_DIRECTIONS:
            raise ConfigError(f"{key}: invalid value {direction!r}; expected one of {', '.join(BOARDING_DIRECTIONS)}")
        frames = _num(data, "min_frames_seen", cls.min_frames_seen, section, 1, 300, integer=True)
        return cls(min_frames_seen=frames, entry_line=merged)


@dataclass(frozen=True)
class QRConfig(_Section):
    enabled: bool = True
    scan_every_n_frames: int = 3
    rescan_cooldown_sec: float = 5.0
    fallback_window_sec: float = 30.0

    _NAME = "qr"
    _RULES = {"enabled": ("bool",), "scan_every_n_frames": ("int", 1, 60),
              "rescan_cooldown_sec": ("num", 0, 3600), "fallback_window_sec": ("num", 0, 3600)}


@dataclass(frozen=True)
class RouteConfig:
    route_id: str = "R1"
    name: str = "City Express"
    stations: Tuple[str, ...] = DEFAULT_STATIONS

    @classmethod
    def from_dict(cls, data: Any = None, *, section: str = "route") -> "RouteConfig":
        data = _mapping(data, section)
        _warn_unknown(data, ("route_id", "name", "stations"), section)
        raw = data.get("stations", DEFAULT_STATIONS)
        if (isinstance(raw, str) or not isinstance(raw, (list, tuple))
                or not all(isinstance(s, str) and s.strip() for s in raw)):
            raise ConfigError(f"{section}.stations: expected a list of non-empty station names, got {raw!r}")
        stations = [s.strip() for s in raw]
        if len(stations) < 2:
            raise ConfigError(f"{section}.stations: at least 2 stations are required")
        duplicates = sorted({s for s in stations if stations.count(s) > 1})
        if duplicates:
            raise ConfigError(f"{section}.stations: duplicate station names {duplicates}")
        return cls(route_id=_str(data, "route_id", cls.route_id, section),
                   name=_str(data, "name", cls.name, section), stations=tuple(stations))


@dataclass(frozen=True)
class JourneyConfig(_Section):
    direction: str = "outbound"  # outbound = stations order; inbound = reversed
    current_station: Optional[str] = "City Hospital"  # None skips positional checks
    trip_id: Optional[str] = None  # None -> "<route_id>-<direction>-<local YYYY-MM-DD>"

    _NAME = "journey"
    _RULES = {"direction": ("choice", JOURNEY_DIRECTIONS), "current_station": ("optstr",), "trip_id": ("optstr",)}


@dataclass(frozen=True)
class AlertsConfig(_Section):
    verification_timeout_sec: float = 60.0

    _NAME = "alerts"
    _RULES = {"verification_timeout_sec": ("num", 5, 3600)}


@dataclass(frozen=True)
class RuntimeConfig(_Section):
    preview_path: str = "data/runtime/latest_frame.jpg"
    preview_interval_sec: float = 1.0  # 0 disables preview writing
    heartbeat_stale_sec: float = 10.0

    _NAME = "runtime"
    _RULES = {"preview_path": ("str",), "preview_interval_sec": ("num", 0, 60),
              "heartbeat_stale_sec": ("num", 1, 3600)}


@dataclass(frozen=True)
class DashboardConfig(_Section):
    refresh_sec: int = 5
    default_period: str = "today"
    table_limit: int = 50

    _NAME = "dashboard"
    _RULES = {"refresh_sec": ("int", 2, 60), "default_period": ("choice", DASHBOARD_PERIODS),
              "table_limit": ("int", 10, 500)}


_SECTIONS = {
    "app": AppSection, "logging": LoggingConfig, "database": DatabaseConfig, "camera": CameraConfig,
    "detection": DetectionConfig, "tracking": TrackingConfig, "boarding": BoardingConfig, "qr": QRConfig,
    "route": RouteConfig, "journey": JourneyConfig, "alerts": AlertsConfig, "runtime": RuntimeConfig,
    "dashboard": DashboardConfig,
}


@dataclass(frozen=True)
class AppConfig:
    app: AppSection = field(default_factory=AppSection)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    tracking: TrackingConfig = field(default_factory=TrackingConfig)
    boarding: BoardingConfig = field(default_factory=BoardingConfig)
    qr: QRConfig = field(default_factory=QRConfig)
    route: RouteConfig = field(default_factory=RouteConfig)
    journey: JourneyConfig = field(default_factory=JourneyConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    config_path: Optional[Path] = None

    @classmethod
    def from_dict(cls, data: Any = None, *, config_path: Optional[Path] = None) -> "AppConfig":
        """Build every section, then run the cross-section checks."""
        data = _mapping(data, "config")
        _warn_unknown(data, _SECTIONS, "config (top level)")
        sections = {name: klass.from_dict(data.get(name), section=name) for name, klass in _SECTIONS.items()}
        cfg = cls(**sections, config_path=config_path)
        _cross_check(cfg)
        return cfg


def _cross_check(cfg: AppConfig) -> None:
    station = cfg.journey.current_station
    if station is not None and station not in cfg.route.stations:
        raise ConfigError(f"journey.current_station: {station!r} is not on route {cfg.route.route_id} "
                          f"(stations: {', '.join(cfg.route.stations)})")
    if cfg.runtime.heartbeat_stale_sec < 2 * cfg.database.flush_interval_sec:
        raise ConfigError("runtime.heartbeat_stale_sec: must be >= 2 x database.flush_interval_sec")
