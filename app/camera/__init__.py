"""Camera / video-input layer."""

from .video_source import (
    DEFAULT_STREAM_URL,
    CameraConfig,
    CameraConnectionError,
    VideoSource,
    load_camera_config,
)

__all__ = [
    "DEFAULT_STREAM_URL",
    "CameraConfig",
    "CameraConnectionError",
    "VideoSource",
    "load_camera_config",
]
