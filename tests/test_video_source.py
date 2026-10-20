"""Tests for the configurable video source (webcam / phone / video)."""

from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np
import pytest

import app.settings_schema as schema
import app.video_source as video_source
from app.main import apply_source_overrides, parse_args
from app.settings_schema import ConfigError
from app.video_source import (
    DEFAULT_STREAM_URL,
    CameraConfig,
    CameraConnectionError,
    VideoSource,
    check_video_file,
    load_camera_config,
)


def _write_video(path: Path, frames: int = 5) -> Path:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (64, 48))
    assert writer.isOpened()
    for i in range(frames):
        writer.write(np.full((48, 64, 3), i * 20, dtype=np.uint8))
    writer.release()
    return path


def test_nested_yaml_config():
    cfg = CameraConfig.from_dict({
        "source_type": "video",
        "webcam": {"index": 2},
        "phone": {"url": "http://10.0.0.9:8080/video"},
        "video": {"path": "data/videos/x.mp4"},
        "reconnect": {"enabled": False, "attempts": 7, "delay_seconds": 1},
    })
    assert cfg.source_type == "video" and cfg.source == "data/videos/x.mp4"
    assert cfg.webcam_index == 2 and cfg.phone_url == "http://10.0.0.9:8080/video"
    assert (cfg.reconnect_enabled, cfg.reconnect_attempts, cfg.reconnect_delay_sec) == (False, 7, 1.0)


def test_legacy_source_key_still_works():
    assert CameraConfig.from_dict({"source": 0}).source_type == "webcam"
    assert CameraConfig.from_dict({"source": "http://1.2.3.4:8080/video"}).source_type == "phone"
    assert CameraConfig.from_dict({"source": "clip.mp4"}).source_type == "video"


def test_invalid_source_type():
    with pytest.raises(ValueError):
        CameraConfig.from_dict({"source_type": "cctv"})


def test_project_config_defaults_to_phone_url():
    cfg = load_camera_config()
    assert cfg.phone_url == DEFAULT_STREAM_URL == "http://192.0.0.4:8080/video"
    assert cfg.webcam_index == 0
    assert cfg.video_path == "data/videos/bus_video.mp4"


@pytest.mark.parametrize("argv, expected_type, expected_source", [
    (["--source", "phone"], "phone", "http://192.0.0.4:8080/video"),
    (["--source", "webcam"], "webcam", 0),
    (["--source", "video", "--video", "a.mp4"], "video", "a.mp4"),
    (["--video", "b.avi"], "video", "b.avi"),
    (["--source", "1"], "webcam", 1),
])
def test_cli_overrides(argv, expected_type, expected_source):
    cfg = CameraConfig()
    apply_source_overrides(cfg, parse_args(argv))
    assert cfg.source_type == expected_type
    assert cfg.source == expected_source


@pytest.mark.parametrize("argv, expected_type, expected_source", [
    (["--url", "http://10.0.0.5:8080/video"], "phone", "http://10.0.0.5:8080/video"),
    (["--phone-url", "http://10.0.0.6:8080/video"], "phone", "http://10.0.0.6:8080/video"),
    (["--source", "webcam", "--camera-index", "2"], "webcam", 2),
    (["--source", "webcam", "--webcam-index", "3"], "webcam", 3),
    (["--path", "c.mov"], "video", "c.mov"),
    (["--source", "http://10.0.0.7:8080/video"], "phone", "http://10.0.0.7:8080/video"),
])
def test_cli_alias_overrides(argv, expected_type, expected_source):
    cfg = CameraConfig()
    apply_source_overrides(cfg, parse_args(argv))
    assert (cfg.source_type, cfg.source) == (expected_type, expected_source)


def test_cli_rejects_out_of_range_camera_index():
    with pytest.raises(SystemExit):
        parse_args(["--camera-index", "17"])


def test_phone_url_env_loses_to_cli_url(monkeypatch):
    monkeypatch.setenv("TPD_PHONE_URL", "http://10.1.1.5:8080/video")
    cfg = load_camera_config()
    assert cfg.phone_url == "http://10.1.1.5:8080/video"  # env overrides config.yaml
    apply_source_overrides(cfg, parse_args(["--url", "http://10.9.9.9:8080/video"]))
    assert cfg.phone_url == "http://10.9.9.9:8080/video"  # --url wins over env


def test_fallback_webcam_flag():
    cfg = CameraConfig()
    apply_source_overrides(cfg, parse_args([]))
    assert cfg.fallback_to_webcam is False
    apply_source_overrides(cfg, parse_args(["--fallback-webcam"]))
    assert cfg.fallback_to_webcam is True


def test_video_file_reads_all_frames_then_finishes(tmp_path):
    path = _write_video(tmp_path / "clip.avi", frames=5)
    cam = VideoSource(CameraConfig(source_type="video", video_path=str(path)))
    cam.open()
    assert cam.is_opened()
    frames = []
    while True:
        ret, frame = cam.read()
        if not ret:
            break
        frames.append(frame)
    assert len(frames) == 5  # first frame is not dropped
    assert frames[0].shape == (48, 64, 3)
    assert cam.finished
    assert cam.read() == (False, None)  # no infinite loop
    cam.release()
    assert not cam.is_opened()


def test_missing_video_file_clear_error():
    cam = VideoSource(CameraConfig(source_type="video", video_path="data/videos/missing.mp4"))
    with pytest.raises(CameraConnectionError, match="Video file not found:\ndata/videos/missing.mp4"):
        cam.open()


def test_unreachable_phone_clear_error_without_fallback():
    cfg = CameraConfig(source_type="phone", phone_url="http://127.0.0.1:9/video",
                       fallback_to_webcam=False, open_timeout_sec=1)
    with pytest.raises(CameraConnectionError) as exc:
        VideoSource(cfg).open()
    msg = str(exc.value)
    assert "Unable to connect to Android phone camera:" in msg
    assert "same Wi-Fi network" in msg and "/video stream" in msg


def test_source_type_string_constructor():
    cam = VideoSource("webcam")
    assert cam.config.source_type == "webcam"
    assert cam.describe() == ("Camera index", "0")
    assert VideoSource("video").describe() == ("Source", "data/videos/bus_video.mp4")
    assert VideoSource("phone").describe() == ("Source", "http://192.0.0.4:8080/video")


def test_password_redacted_in_description():
    cam = VideoSource(CameraConfig(phone_url="http://user:secret@192.0.0.4:8080/video"))
    assert "secret" not in cam.describe()[1]



class _DeadCap:
    """Fake capture that never returns frames."""

    def read(self):
        return False, None

    def isOpened(self):
        return True

    def release(self):
        pass


def test_live_source_reconnect_is_bounded(monkeypatch):
    cfg = CameraConfig(source_type="webcam", reconnect_attempts=3, reconnect_delay_sec=0)
    cam = VideoSource(cfg)
    opens = []

    def fake_open(source_type, source):
        opens.append(source_type)
        if len(opens) == 1:
            return _DeadCap()  # initial open succeeds, then the stream dies
        raise CameraConnectionError("still down")

    monkeypatch.setattr(cam, "_open_source", fake_open)
    cam.open()
    with pytest.raises(CameraConnectionError, match="after 3 reconnect attempts"):
        cam.read()
    assert len(opens) == 1 + 3  # one initial open + exactly 3 retries
    assert not cam.is_opened()


def test_reconnect_disabled(monkeypatch):
    cam = VideoSource(CameraConfig(source_type="webcam", reconnect_enabled=False))
    monkeypatch.setattr(cam, "_open_source", lambda *_: _DeadCap())
    cam.open()
    with pytest.raises(CameraConnectionError, match="reconnect disabled"):
        cam.read()


# ------------------------------------------------------- single implementation
def test_camera_constants_have_one_definition():
    assert video_source.CameraConfig is schema.CameraConfig
    assert video_source.SOURCE_TYPES is schema.SOURCE_TYPES
    assert video_source.DEFAULT_STREAM_URL is schema.DEFAULT_STREAM_URL
    assert video_source.VIDEO_EXTENSIONS is schema.VIDEO_EXTENSIONS


def test_legacy_camera_package_is_gone():
    assert not (Path(video_source.__file__).parent / "camera").exists()
    assert VideoSource.__module__ == "app.video_source"


def test_check_video_file_extension_checked_before_existence():
    with pytest.raises(CameraConnectionError) as exc:
        check_video_file("data/videos/missing.txt", (".mp4", ".avi", ".mov", ".mkv"))
    assert str(exc.value) == ("Unsupported video file type '.txt': data/videos/missing.txt\n"
                              "Allowed: .mp4, .avi, .mov, .mkv")
    with pytest.raises(CameraConnectionError) as exc:
        check_video_file("data/videos/missing.mp4", (".mp4",))
    assert str(exc.value) == "Video file not found:\ndata/videos/missing.mp4"
    with pytest.raises(CameraConnectionError, match="Unsupported video file type '': noext"):
        check_video_file("noext", (".mp4",))


def test_check_video_file_upper_case_extension_accepted(tmp_path):
    path = tmp_path / "CLIP.MP4"
    path.write_bytes(b"")  # only extension + existence are checked here
    assert check_video_file(str(path), (".mp4",)) == path


def test_open_rejects_unsupported_extension(tmp_path):
    notes = tmp_path / "notes.txt"
    notes.write_text("not a video", encoding="utf-8")
    cam = VideoSource(CameraConfig(source_type="video", video_path=str(notes)))
    with pytest.raises(CameraConnectionError, match="Unsupported video file type '.txt'"):
        cam.open()


def test_corrupt_video_reports_unreadable(tmp_path):
    bad = tmp_path / "broken.mp4"
    bad.write_bytes(b"\x00" * 64)
    with pytest.raises(CameraConnectionError, match="Could not open video file|no readable frames"):
        VideoSource(CameraConfig(source_type="video", video_path=str(bad))).open()


def test_invalid_source_type_is_config_error():
    with pytest.raises(ConfigError) as exc:
        CameraConfig.from_dict({"source_type": "cctv"})
    assert str(exc.value) == "camera.source_type: invalid value 'cctv'; expected one of phone, webcam, video"
    with pytest.raises(ValueError):
        CameraConfig().select("cctv")


def test_fallback_to_webcam_defaults_off():
    assert CameraConfig().fallback_to_webcam is False
    assert CameraConfig.from_dict({}).fallback_to_webcam is False
    assert CameraConfig.from_dict({"fallback_to_webcam": True}).fallback_to_webcam is True


def test_phone_url_env_override(monkeypatch):
    monkeypatch.setenv("TPD_PHONE_URL", "http://10.1.1.5:8080/video")
    assert load_camera_config().phone_url == "http://10.1.1.5:8080/video"


def test_choose_video_file_returns_none_without_tkinter(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("tkinter"):
            raise ImportError("no tkinter")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert video_source.choose_video_file() is None
