"""Person tracking with Ultralytics YOLO + ByteTrack.

This module assigns persistent, anonymous integer Track IDs to people seen in a
video stream. It deliberately performs NO face recognition and NO identity
lookup: a Track ID is only a per-session handle produced by ByteTrack's motion /
IoU association, so the same passenger keeps the same ID across consecutive
frames for as long as the tracker can associate them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

BBox = Tuple[float, float, float, float]  # (x1, y1, x2, y2) in pixels

PERSON_CLASS_ID = 0  # COCO "person"


@dataclass(frozen=True)
class TrackedPerson:
    """A single tracked person in one frame."""

    track_id: int
    bbox: BBox
    confidence: float
    class_id: int = PERSON_CLASS_ID

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
    """Book-keeping for a Track ID across frames."""

    track_id: int
    first_seen_frame: int
    last_seen_frame: int
    hits: int = 1


@dataclass
class TrackerConfig:
    model_path: str = "yolov8n.pt"
    tracker: str = "bytetrack.yaml"  # Ultralytics built-in ByteTrack config
    conf: float = 0.4
    iou: float = 0.5
    imgsz: int = 640
    device: Optional[str] = None
    person_class_id: int = PERSON_CLASS_ID
    # Forget internal bookkeeping for tracks unseen for this many frames.
    max_missing_frames: int = 90

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "TrackerConfig":
        data = dict(data or {})
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        return cls(**known)


def _to_numpy(value: Any) -> np.ndarray:
    """Convert a torch tensor / numpy array / list to a numpy array."""
    if value is None:
        return np.empty((0,))
    if hasattr(value, "detach"):
        value = value.detach()
    if hasattr(value, "cpu"):
        value = value.cpu()
    if hasattr(value, "numpy"):
        return value.numpy()
    return np.asarray(value)


class PersonTracker:
    """Wraps ``YOLO.track(..., tracker="bytetrack.yaml", persist=True)``.

    ``persist=True`` keeps the ByteTrack state attached to the predictor between
    calls, which is what makes Track IDs stable across consecutive frames.

    A pre-built model (or any object exposing a compatible ``track`` method) can
    be injected via ``model`` for testing or to share weights.
    """

    def __init__(self, config: Optional[TrackerConfig] = None, model: Any = None) -> None:
        self.config = config or TrackerConfig()
        self._model = model
        self._frame_index = 0
        self._tracks: Dict[int, TrackState] = {}
        self._persist = True

    # ------------------------------------------------------------------ model
    @property
    def model(self) -> Any:
        if self._model is None:
            from ultralytics import YOLO  # lazy import: heavy dependency

            logger.info("Loading YOLO model for tracking: %s", self.config.model_path)
            self._model = YOLO(self.config.model_path)
        return self._model

    # --------------------------------------------------------------- tracking
    def track(self, frame: np.ndarray) -> List[TrackedPerson]:
        """Run detection + ByteTrack on one BGR frame and return tracked people.

        Detections that ByteTrack has not (yet) confirmed have no ID and are
        omitted, so every returned item has a valid persistent ``track_id``.
        """
        kwargs: Dict[str, Any] = dict(
            persist=self._persist,
            tracker=self.config.tracker,
            classes=[self.config.person_class_id],
            conf=self.config.conf,
            iou=self.config.iou,
            imgsz=self.config.imgsz,
            verbose=False,
        )
        if self.config.device is not None:
            kwargs["device"] = self.config.device

        results = self.model.track(frame, **kwargs)
        self._persist = True  # after a reset, re-enable persistence
        self._frame_index += 1

        people = self.parse_results(results, self.config.person_class_id)
        self._update_states(people)
        return people

    @staticmethod
    def parse_results(results: Any, person_class_id: int = PERSON_CLASS_ID) -> List[TrackedPerson]:
        """Convert Ultralytics ``Results`` into :class:`TrackedPerson` objects."""
        if not results:
            return []
        result = results[0] if isinstance(results, (list, tuple)) else results
        boxes = getattr(result, "boxes", None)
        if boxes is None or getattr(boxes, "id", None) is None:
            return []

        ids = _to_numpy(boxes.id).astype(int).reshape(-1)
        xyxy = _to_numpy(boxes.xyxy).astype(float).reshape(-1, 4)
        confs = _to_numpy(boxes.conf).astype(float).reshape(-1)
        classes = (
            _to_numpy(boxes.cls).astype(int).reshape(-1)
            if getattr(boxes, "cls", None) is not None
            else np.full(len(ids), person_class_id)
        )

        people: List[TrackedPerson] = []
        for tid, box, conf, cls in zip(ids, xyxy, confs, classes):
            if int(cls) != person_class_id:
                continue
            people.append(
                TrackedPerson(
                    track_id=int(tid),
                    bbox=(float(box[0]), float(box[1]), float(box[2]), float(box[3])),
                    confidence=float(conf),
                    class_id=int(cls),
                )
            )
        return people

    def _update_states(self, people: List[TrackedPerson]) -> None:
        for person in people:
            state = self._tracks.get(person.track_id)
            if state is None:
                self._tracks[person.track_id] = TrackState(
                    person.track_id, self._frame_index, self._frame_index
                )
            else:
                state.last_seen_frame = self._frame_index
                state.hits += 1

        cutoff = self._frame_index - self.config.max_missing_frames
        for tid in [t for t, s in self._tracks.items() if s.last_seen_frame < cutoff]:
            del self._tracks[tid]

    # ----------------------------------------------------------------- state
    @property
    def frame_index(self) -> int:
        return self._frame_index

    @property
    def active_track_ids(self) -> List[int]:
        """IDs seen in the most recent frame."""
        return sorted(t for t, s in self._tracks.items() if s.last_seen_frame == self._frame_index)

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
