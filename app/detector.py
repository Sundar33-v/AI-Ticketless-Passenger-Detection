"""YOLO model loading and person-only box parsing.

Only COCO class 0 ("person") ever reaches the passenger pipeline. Tables,
chairs, bags, vehicles, tools and every other class are dropped here even if a
model backend ignores the ``classes=[0]`` request.

``load_model`` checks that the weights file exists BEFORE importing
Ultralytics and passes an absolute path, so the library never auto-downloads.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Tuple

import numpy as np

from app.config import resolve_path
from app.settings_schema import DetectionConfig

logger = logging.getLogger(__name__)

PERSON_CLASS_ID = 0  # COCO "person"
DOWNLOAD_COMMAND = (
    "cd models\n"
    "..\\.venv\\Scripts\\python.exe -c \"from ultralytics import YOLO; YOLO('yolov8n.pt')\""
)

BBox = Tuple[float, float, float, float]  # (x1, y1, x2, y2) in pixels


class ModelNotFoundError(RuntimeError):
    """The YOLO weights file is missing (or, via :class:`ModelLoadError`, unusable)."""


class ModelLoadError(ModelNotFoundError):
    """The weights file exists but Ultralytics could not load it."""


def _missing_model_message(path: Path) -> str:
    return (
        f"YOLO model file not found:\n{path}\n\n"
        "Download it (see models/README.md):\n"
        f"{DOWNLOAD_COMMAND}\n"
        "or set detection.model_path in config/config.yaml (or --model / TPD_MODEL_PATH) "
        "to an existing .pt file."
    )


def _cuda_available() -> bool:
    try:
        import torch
    except ImportError:
        return False
    try:
        return bool(torch.cuda.is_available())
    except Exception:  # broken CUDA install -> behave as CPU-only
        return False


def resolve_device(device: str) -> str:
    """Map ``cpu`` / ``auto`` / ``cuda:N`` to a device Ultralytics can use.

    ``auto`` picks ``cuda:0`` when available, else ``cpu``. A requested CUDA
    device on a machine without CUDA falls back to ``cpu`` with a WARNING.
    """
    text = str(device).strip().lower()
    if text == "cpu":
        return "cpu"
    if text == "auto":
        return "cuda:0" if _cuda_available() else "cpu"
    if re.fullmatch(r"cuda:\d+", text):
        if _cuda_available():
            return text
        logger.warning("CUDA device %s requested but CUDA is not available; using cpu.", text)
        return "cpu"
    raise ValueError(f"detection.device: invalid value {device!r}; expected cpu, auto or cuda:N")


def load_model(cfg: DetectionConfig) -> Any:
    """Load the YOLO weights named by ``cfg.model_path`` (relative -> project root)."""
    path = resolve_path(cfg.model_path)
    if not path.is_file():
        raise ModelNotFoundError(_missing_model_message(path))
    from ultralytics import YOLO  # heavy import, only after the file check

    try:
        model = YOLO(str(path))
    except Exception as exc:
        raise ModelLoadError(f"Could not load YOLO model {path}: {exc}") from exc
    logger.info("Loaded YOLO model %s", path)
    return model


# ------------------------------------------------------------------ parsing
@dataclass(frozen=True)
class PersonBox:
    """One person box from a model result (``track_id`` None when untracked)."""

    bbox: BBox
    confidence: float
    class_id: int = PERSON_CLASS_ID
    track_id: Optional[int] = None


def _to_numpy(value: Any) -> np.ndarray:
    """Convert a torch tensor / numpy array / sequence to a numpy array."""
    if value is None:
        return np.empty((0,))
    if hasattr(value, "detach"):
        value = value.detach()
    if hasattr(value, "cpu"):
        value = value.cpu()
    if hasattr(value, "numpy"):
        value = value.numpy()
    return np.asarray(value)


def parse_person_boxes(results: Any, require_ids: bool = True) -> List[PersonBox]:
    """Convert Ultralytics ``Results`` into person boxes.

    Keeps class 0 only, drops non-finite and degenerate (zero-area) boxes. With
    ``require_ids`` (tracking mode) boxes that ByteTrack has not assigned an ID
    are omitted, so every returned box has a persistent ``track_id``.
    """
    if results is None:
        return []
    if isinstance(results, (list, tuple)):
        if not results:
            return []
        results = results[0]
    boxes = getattr(results, "boxes", None)
    if boxes is None:
        return []
    raw_ids = getattr(boxes, "id", None)
    if require_ids and raw_ids is None:
        return []

    xyxy = _to_numpy(boxes.xyxy).astype(float).reshape(-1, 4)
    confs = _to_numpy(boxes.conf).astype(float).reshape(-1)
    n = len(xyxy)
    classes = (_to_numpy(boxes.cls).astype(float).reshape(-1)
               if getattr(boxes, "cls", None) is not None else np.full(n, PERSON_CLASS_ID, dtype=float))
    ids = _to_numpy(raw_ids).astype(float).reshape(-1) if raw_ids is not None else np.full(n, np.nan)
    if not (len(confs) == len(classes) == len(ids) == n):
        raise ValueError(f"inconsistent result arrays: boxes={n} conf={len(confs)} "
                         f"cls={len(classes)} id={len(ids)}")

    people: List[PersonBox] = []
    for box, conf, cls, tid in zip(xyxy, confs, classes, ids):
        if int(round(float(cls))) != PERSON_CLASS_ID:
            continue
        if not (np.isfinite(conf) and np.all(np.isfinite(box))):
            continue
        x1, y1, x2, y2 = (float(v) for v in box)
        if x2 <= x1 or y2 <= y1:
            continue
        track_id = int(tid) if np.isfinite(tid) else None
        if require_ids and track_id is None:
            continue
        people.append(PersonBox((x1, y1, x2, y2), float(conf), PERSON_CLASS_ID, track_id))
    return people
