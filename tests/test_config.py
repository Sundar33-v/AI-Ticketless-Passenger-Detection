"""Tests for app.settings_schema (validation) and app.config (layering, paths, logging)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import app.config as config
from app.config import PROJECT_ROOT, load_config, resolve_db_path, resolve_path
from app.settings_schema import (
    AppConfig,
    BoardingConfig,
    CameraConfig,
    ConfigError,
    DatabaseConfig,
    DetectionConfig,
    LoggingConfig,
    RouteConfig,
    TrackingConfig,
)


def _yaml(tmp_path: Path, text: str, name: str = "config.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------------------ defaults
def test_defaults_match_design(cfg):
    assert cfg.camera.source_type == "phone"
    assert cfg.camera.phone_url == "http://192.0.0.4:8080/video"
    assert cfg.camera.fallback_to_webcam is False
    assert cfg.detection.confidence == 0.35 and cfg.detection.model_path == "models/yolov8n.pt"
    assert cfg.tracking.input_confidence == 0.1
    assert cfg.tracking.tracker_config == "config/bytetrack.yaml"
    assert cfg.database.path == "data/ticketless.db"
    assert cfg.boarding.entry_line["entry_direction"] == "top_to_bottom"
    assert cfg.journey.current_station in cfg.route.stations
    assert len(cfg.route.stations) == 6


def test_missing_yaml_uses_defaults_with_warning(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        cfg = load_config(tmp_path / "nope.yaml")
    assert "not found" in caplog.text
    assert cfg.config_path is None and cfg.camera.webcam_index == 0


def test_unknown_keys_warn(tmp_path, caplog):
    path = _yaml(tmp_path, "detection:\n  confidence: 0.4\n  colour: red\nextra: {}\n")
    with caplog.at_level(logging.WARNING):
        cfg = load_config(path)
    assert cfg.detection.confidence == 0.4
    assert "colour" in caplog.text and "extra" in caplog.text


# --------------------------------------------------------------- validation
@pytest.mark.parametrize("data, key", [
    ({"detection": {"confidence": 1.5}}, "detection.confidence"),
    ({"detection": {"confidence": "high"}}, "detection.confidence"),
    ({"detection": {"imgsz": 650}}, "detection.imgsz"),
    ({"detection": {"device": "gpu"}}, "detection.device"),
    ({"database": {"busy_timeout_ms": 10}}, "database.busy_timeout_ms"),
    ({"camera": {"webcam": {"index": 99}}}, "camera.webcam.index"),
    ({"camera": {"reconnect": {"attempts": 0}}}, "camera.reconnect.attempts"),
    ({"camera": {"phone": {"url": "ftp://x/video"}}}, "camera.phone.url"),
    ({"camera": {"fallback_to_webcam": "yes"}}, "camera.fallback_to_webcam"),
    ({"tracking": {"input_confidence": 0.6}}, "tracking.input_confidence"),
    ({"tracking": {"input_confidence": 0.001}}, "tracking.input_confidence"),
    ({"logging": {"level": "LOUD"}}, "logging.level"),
    ({"app": {"timezone": "Mars/Olympus"}}, "app.timezone"),
    ({"journey": {"direction": "sideways"}}, "journey.direction"),
    ({"dashboard": {"default_period": "decade"}}, "dashboard.default_period"),
    ({"route": {"stations": ["A", "B", "A"]}}, "route.stations"),
    ({"route": {"stations": ["Only one"]}}, "route.stations"),
    ({"qr": "on"}, "qr"),
])
def test_errors_name_the_dotted_key(data, key):
    with pytest.raises(ConfigError) as exc:
        AppConfig.from_dict(data)
    assert str(exc.value).startswith(f"{key}:"), str(exc.value)


def test_config_error_is_value_error():
    assert issubclass(ConfigError, ValueError)


def test_input_confidence_range_bounds():
    assert TrackingConfig.from_dict({"input_confidence": 0.01}).input_confidence == 0.01
    assert TrackingConfig.from_dict({"input_confidence": 0.5}).input_confidence == 0.5


def test_boarding_rejects_both_direction():
    with pytest.raises(ConfigError, match="'both' is not allowed for boarding"):
        BoardingConfig.from_dict({"entry_line": {"entry_direction": "both"}})
    cfg = BoardingConfig.from_dict({"entry_line": {"entry_direction": "Bottom_To_Top", "position": 0.6}})
    assert cfg.entry_line["entry_direction"] == "bottom_to_top"
    assert cfg.entry_line["position"] == 0.6 and cfg.entry_line["anchor"] == "bottom_center"


def test_station_must_be_on_route():
    with pytest.raises(ConfigError, match=r"^journey.current_station:"):
        AppConfig.from_dict({"journey": {"current_station": "Moon Base"}})
    assert AppConfig.from_dict({"journey": {"current_station": None}}).journey.current_station is None


def test_heartbeat_must_cover_two_flushes():
    with pytest.raises(ConfigError) as exc:
        AppConfig.from_dict({"database": {"flush_interval_sec": 5}, "runtime": {"heartbeat_stale_sec": 9}})
    assert str(exc.value) == "runtime.heartbeat_stale_sec: must be >= 2 x database.flush_interval_sec"
    AppConfig.from_dict({"database": {"flush_interval_sec": 5}, "runtime": {"heartbeat_stale_sec": 10}})


def test_route_strips_names():
    assert RouteConfig.from_dict({"stations": [" A ", "B"]}).stations == ("A", "B")


def test_invalid_yaml_raises(tmp_path):
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(_yaml(tmp_path, "camera: [unclosed\n"))


# --------------------------------------------------------------- precedence
def test_precedence_defaults_yaml_dotenv_env(tmp_path):
    path = _yaml(tmp_path, "camera:\n  source_type: webcam\n  webcam: {index: 1}\n"
                           "detection:\n  device: cpu\n")
    dotenv = tmp_path / ".env"
    dotenv.write_text('# comment\nTPD_CAMERA_INDEX=2\nTPD_DEVICE="auto"\nOTHER=1\n', encoding="utf-8")

    assert load_config(path, environ={}, dotenv_path=tmp_path / "none.env").camera.webcam_index == 1
    from_dotenv = load_config(path, environ={}, dotenv_path=dotenv)
    assert (from_dotenv.camera.webcam_index, from_dotenv.detection.device) == (2, "auto")
    from_env = load_config(path, environ={"TPD_CAMERA_INDEX": "3"}, dotenv_path=dotenv)
    assert (from_env.camera.webcam_index, from_env.detection.device) == (3, "auto")
    assert from_env.camera.source_type == "webcam"  # YAML value untouched


def test_default_dotenv_path_is_patched_module_attribute(tmp_path, monkeypatch):
    dotenv = tmp_path / "x.env"
    dotenv.write_text("TPD_LOG_LEVEL=debug\n", encoding="utf-8")
    monkeypatch.setattr(config, "DOTENV_PATH", dotenv)
    assert load_config(tmp_path / "none.yaml", environ={}).logging.level == "DEBUG"


def test_all_env_overrides(tmp_path):
    env = {
        "TPD_SOURCE": "video", "TPD_PHONE_URL": "rtsp://10.0.0.2/live", "TPD_VIDEO_PATH": "clip.mov",
        "TPD_MODEL_PATH": "models/other.pt", "TPD_DEVICE": "cuda:1", "TPD_DB_PATH": "data/x.db",
        "TPD_LOG_LEVEL": "warning", "TPD_CURRENT_STATION": "Airport",
    }
    cfg = load_config(tmp_path / "none.yaml", environ=env)
    assert cfg.camera.source_type == "video" and cfg.camera.video_path == "clip.mov"
    assert cfg.camera.phone_url == "rtsp://10.0.0.2/live"
    assert (cfg.detection.model_path, cfg.detection.device) == ("models/other.pt", "cuda:1")
    assert cfg.database.path == "data/x.db" and cfg.logging.level == "WARNING"
    assert cfg.journey.current_station == "Airport"
    none_station = load_config(tmp_path / "none.yaml", environ={"TPD_CURRENT_STATION": "none"})
    assert none_station.journey.current_station is None


def test_camera_index_must_be_integer(tmp_path):
    with pytest.raises(ConfigError, match="^TPD_CAMERA_INDEX:"):
        load_config(tmp_path / "none.yaml", environ={"TPD_CAMERA_INDEX": "abc"})


def test_invalid_env_source_names_key(tmp_path):
    with pytest.raises(ConfigError, match=r"^camera.source_type:"):
        load_config(tmp_path / "none.yaml", environ={"TPD_SOURCE": "cctv"})


def test_tpd_config_selects_file(tmp_path):
    path = _yaml(tmp_path, "qr:\n  enabled: false\n", name="alt.yaml")
    cfg = load_config(environ={"TPD_CONFIG": str(path)})
    assert cfg.qr.enabled is False and cfg.config_path == path.resolve()


# -------------------------------------------------------------------- paths
def test_relative_paths_resolve_against_project_root():
    assert resolve_path("data/x.mp4") == (PROJECT_ROOT / "data" / "x.mp4").resolve()
    absolute = Path("C:/somewhere/else.mp4") if Path("C:/").exists() else Path("/somewhere/else.mp4")
    assert resolve_path(absolute) == absolute.resolve()


def test_resolve_db_path_order(tmp_path, monkeypatch):
    cfg = AppConfig.from_dict({"database": {"path": "data/from_cfg.db"}})
    monkeypatch.setenv("TPD_DB_PATH", str(tmp_path / "env.db"))
    assert resolve_db_path(str(tmp_path / "cli.db"), cfg) == (tmp_path / "cli.db").resolve()
    assert resolve_db_path(None, cfg) == (tmp_path / "env.db").resolve()
    monkeypatch.delenv("TPD_DB_PATH")
    assert resolve_db_path(None, cfg) == PROJECT_ROOT / "data" / "from_cfg.db"
    assert resolve_db_path() == PROJECT_ROOT / "data" / "ticketless.db"  # computed only, never opened
    assert resolve_db_path(":memory:") == ":memory:"
    assert resolve_db_path("  ", cfg) == PROJECT_ROOT / "data" / "from_cfg.db"


def test_resolve_db_path_rejects_bad_suffix_and_directories(tmp_path):
    with pytest.raises(ConfigError, match="^database.path:"):
        resolve_db_path(str(tmp_path / "data.txt"))
    with pytest.raises(ConfigError, match="directory"):
        resolve_db_path(str(tmp_path))
    assert resolve_db_path(str(tmp_path / "a.sqlite3")).suffix == ".sqlite3"


def test_isolated_env_points_default_db_into_tmp(tmp_path):
    assert resolve_db_path() == (tmp_path / "default.db").resolve()


def test_database_config_from_dict():
    assert DatabaseConfig.from_dict({"flush_interval_sec": 2}).flush_interval_sec == 2.0


# ------------------------------------------------------------------ camera
def test_camera_config_legacy_flat_keys():
    cam = CameraConfig.from_dict({"phone_url": "http://1.2.3.4:8080/video", "webcam_index": 3})
    assert cam.phone_url == "http://1.2.3.4:8080/video" and cam.webcam_index == 3


def test_camera_allowed_extensions_normalized():
    cam = CameraConfig.from_dict({"video": {"allowed_extensions": ["MP4", ".Avi"]}})
    assert cam.allowed_extensions == (".mp4", ".avi")


def test_detection_config_valid_devices():
    for device in ("cpu", "auto", "cuda:0", "CUDA:2"):
        assert DetectionConfig.from_dict({"device": device}).device == device.lower()


# ----------------------------------------------------------------- logging
def test_configure_logging_is_idempotent(tmp_path):
    root = logging.getLogger()
    before = list(root.handlers)
    level = root.level
    try:
        cfg = LoggingConfig(level="DEBUG", dir=str(tmp_path / "logs"))
        config.configure_logging(cfg, filename="t.log")
        config.configure_logging(cfg, filename="t.log")
        ours = [h for h in root.handlers if (h.get_name() or "").startswith("tpd-")]
        assert sorted(h.get_name() for h in ours) == ["tpd-console", "tpd-file:t.log"]
        assert (tmp_path / "logs" / "t.log").exists()
    finally:
        for handler in [h for h in root.handlers if h not in before]:
            root.removeHandler(handler)
            handler.close()
        root.setLevel(level)


def test_configure_logging_unwritable_dir_falls_back_to_console(tmp_path, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    root = logging.getLogger()
    before = list(root.handlers)
    level = root.level
    try:
        with caplog.at_level(logging.WARNING):
            config.configure_logging(LoggingConfig(dir=str(blocker / "logs")), filename="u.log")
        names = [h.get_name() for h in root.handlers if h not in before]
        assert names == ["tpd-console"]
        assert "console only" in caplog.text
    finally:
        for handler in [h for h in root.handlers if h not in before]:
            root.removeHandler(handler)
            handler.close()
        root.setLevel(level)
