"""Person tracking with Ultralytics YOLO + ByteTrack (one ``model.track`` call per frame).

Track IDs are anonymous per-session handles produced by ByteTrack's motion/IoU
association: no face recognition, no identity lookup. IDs can change after
prolonged occlusion, tracker failure or when a person leaves and re-enters.

Confidence split (amendment M1):
  * ``tracking.input_confidence`` (0.1) is passed as ``conf=`` so ByteTrack's
    second stage can keep tracks alive through low-score (occluded) frames.
  * ``detection.confidence`` (0.35) is ByteTrack's ``new_track_thresh`` and the
    acceptance gate: a track is reported only once one of its boxes reached it.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import yaml

from app.config import resolve_path
from app.detector import PERSON_CLASS_ID, BBox, parse_person_boxes, resolve_device
from app.settings_schema import ConfigError, DetectionConfig, TrackingConfig

logger = logging.getLogger(__name__)

EFFECTIVE_YAML_NAME = "bytetrack_effective.yaml"


@dataclass(frozen=True)
class TrackedPerson:
    """A tracked person in one frame (``hits`` = frames this Track ID was seen so far)."""

    track_id: int
    bbox: BBox
    confidence: float
    class_id: int = PERSON_CLASS_ID
    hits: int = 1
    first_seen_frame: int = 0

    @property
    def center(self) -> Tuple[float, float]:
        x1, y1, x2, y2 = self.bbox
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)

    @property
    def bottom_center(self) -> Tuple[float, float]:
        """Approximate foot point; usually the most stable anchor for line crossing."""
        x1, _, x2, y2 = self.bbox
        return ((x1 + x2) / 2.0, y2)


@dataclass
class TrackState:
    """Internal bookkeeping for a Track ID across frames."""

    track_id: int
    first_seen_frame: int
    last_seen_frame: int
    hits: int = 1
    max_confidence: float = 0.0


def prepare_tracker_yaml(tracking_cfg: TrackingConfig, detection_cfg: DetectionConfig,
                         runtime_dir: Union[str, Path]) -> Path:
    """Validate ``tracking.tracker_config`` and write the effective ByteTrack YAML.

    The effective copy (``<runtime_dir>/bytetrack_effective.yaml``) always uses
    ``new_track_thresh = detection.confidence``. Raises :class:`ConfigError` for
    a missing/invalid file, a non-ByteTrack tracker or inconsistent thresholds.
    """
    key = "tracking.tracker_config"
    source = resolve_path(tracking_cfg.tracker_config)
    if not source.is_file():
        raise ConfigError(f"{key}: tracker file not found: {source}")
    try:
        data = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError) as exc:
        raise ConfigError(f"{key}: cannot read {source}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{key}: {source} must contain a mapping of ByteTrack parameters")
    if str(data.get("tracker_type", "")).strip().lower() != "bytetrack":
        raise ConfigError(f"{key}: tracker_type must be 'bytetrack', got {data.get('tracker_type')!r}")
    try:
        low = float(data["track_low_thresh"])
    except (KeyError, TypeError, ValueError):
        raise ConfigError(f"{key}: track_low_thresh must be a number in {source}") from None
    if tracking_cfg.input_confidence > low:
        raise ConfigError(f"tracking.input_confidence: {tracking_cfg.input_confidence} must be <= "
                          f"track_low_thresh ({low}) in {source.name}")
    if detection_cfg.confidence < low:
        raise ConfigError(f"detection.confidence: {detection_cfg.confidence} must be >= "
                          f"track_low_thresh ({low}) in {source.name}")

    committed = data.get("new_track_thresh")
    wanted = float(detection_cfg.confidence)
    if not isinstance(committed, (int, float)) or not math.isclose(float(committed), wanted):
        logger.warning("ByteTrack new_track_thresh %r in %s differs from detection.confidence %.2f; "
                       "the effective value comes from detection.confidence.", committed, source.name, wanted)
    data["new_track_thresh"] = wanted

    out_dir = Path(runtime_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / EFFECTIVE_YAML_NAME
    header = (f"# Generated from {source.name} by app.tracker.prepare_tracker_yaml; do not edit.\n"
              "# new_track_thresh = detection.confidence\n")
    out.write_text(header + yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return out


class PersonTracker:
    """Wraps ``model.track(frame, persist=True, tracker=<effective yaml>, classes=[0], ...)``.

    ``persist=True`` keeps the ByteTrack state attached to the predictor between
    calls, which is what keeps Track IDs stable across consecutive frames. Any
    object with a compatible ``track`` method can be injected as ``model``.
    """

    def __init__(self, model: Any, tracking_cfg: Optional[TrackingConfig] = None,
                 detection_cfg: Optional[DetectionConfig] = None,
                 tracker_yaml: Union[str, Path, None] = None) -> None:
        self.model = model
        self.tracking_cfg = tracking_cfg or TrackingConfig()
        self.detection_cfg = detection_cfg or DetectionConfig()
        self.tracker_yaml = str(tracker_yaml) if tracker_yaml else str(resolve_path(self.tracking_cfg.tracker_config))
        self.device = resolve_device(self.detection_cfg.device)
        self._frame_index = 0
        self._tracks: Dict[int, TrackState] = {}
        self._persist = True

    # --------------------------------------------------------------- tracking
    def track(self, frame: np.ndarray) -> List[TrackedPerson]:
        """Run detection + ByteTrack on one BGR frame; return accepted tracked people."""
        results = self.model.track(
            frame,
            persist=self._persist,
            tracker=self.tracker_yaml,
            classes=[PERSON_CLASS_ID],
            conf=self.tracking_cfg.input_confidence,
            iou=self.detection_cfg.iou,
            imgsz=self.detection_cfg.imgsz,
            device=self.device,
            verbose=False,
        )
        self._persist = True  # after a reset, re-enable persistence
        self._frame_index += 1

        threshold = self.detection_cfg.confidence
        people: List[TrackedPerson] = []
        for box in parse_person_boxes(results, require_ids=True):
            assert box.track_id is not None
            state = self._update_state(box.track_id, box.confidence)
            if state.max_confidence < threshold:
                continue  # never reached the person threshold: not reported
            people.append(TrackedPerson(
                track_id=box.track_id, bbox=box.bbox, confidence=box.confidence, class_id=box.class_id,
                hits=state.hits, first_seen_frame=state.first_seen_frame,
            ))
        self._prune()
        return people

    def _update_state(self, track_id: int, confidence: float) -> TrackState:
        state = self._tracks.get(track_id)
        if state is None:
            state = self._tracks[track_id] = TrackState(
                track_id, self._frame_index, self._frame_index, 1, confidence)
        elif state.last_seen_frame != self._frame_index:
            state.last_seen_frame = self._frame_index
            state.hits += 1
        state.max_confidence = max(state.max_confidence, confidence)
        return state

    def _prune(self) -> None:
        cutoff = self._frame_index - self.tracking_cfg.max_missing_frames
        for tid in [t for t, s in self._tracks.items() if s.last_seen_frame < cutoff]:
            del self._tracks[tid]

    # ----------------------------------------------------------------- state
    @property
    def frame_index(self) -> int:
        return self._frame_index

    @property
    def active_track_ids(self) -> List[int]:
        """Accepted IDs seen in the most recent frame."""
        threshold = self.detection_cfg.confidence
        return sorted(t for t, s in self._tracks.items()
                      if s.last_seen_frame == self._frame_index and s.max_confidence >= threshold)

    def get_track_state(self, track_id: int) -> Optional[TrackState]:
        return self._tracks.get(track_id)

    def reset(self) -> None:
        """Drop all tracks (e.g. when switching video source).

        The next ``track`` call is issued with ``persist=False`` so Ultralytics
        re-initialises ByteTrack.
        """
        self._tracks.clear()
        self._frame_index = 0
        self._persist = False
