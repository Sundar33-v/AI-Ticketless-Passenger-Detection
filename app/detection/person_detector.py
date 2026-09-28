"""Person detection using Ultralytics YOLO.

This module is responsible ONLY for detecting people (COCO class 0) in a frame.
Tracking, entry-line logic and ticket/QR validation live in other modules.

Non-person objects (tables, chairs, bags, bottles, laptops, tools, ...) are
filtered out twice:
  1. YOLO is asked to predict only ``classes=[PERSON_CLASS_ID]``.
  2. Every returned box is re-checked for class ID and confidence before it is
     emitted, so nothing else can leak into the passenger pipeline even if the
     model backend ignores the ``classes`` argument.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Sequence

import numpy as np

logger = logging.getLogger(__name__)

PERSON_CLASS_ID: int = 0
DEFAULT_CONFIDENCE: float = 0.50
DEFAULT_IOU: float = 0.45
DEFAULT_MODEL_PATH: str = "yolov8n.pt"


@dataclass(frozen=True, slots=True)
class Detection:
    """A single person detection.

    Attributes:
        bbox: Bounding box as ``(x1, y1, x2, y2)`` in pixel coordinates.
        confidence: Detection confidence in ``[0, 1]``.
        class_id: COCO class ID (always ``PERSON_CLASS_ID``).
    """

    bbox: tuple[float, float, float, float]
    confidence: float
    class_id: int

    def to_dict(self) -> dict[str, Any]:
        """Return a plain-dict representation (e.g. for logging/serialisation)."""
        data = asdict(self)
        data["bbox"] = list(self.bbox)
        return data


def _validate_threshold(name: str, value: float) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number in [0, 1], got {value!r}") from exc
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be in [0, 1], got {value}")
    return value


def _to_numpy(data: Any) -> np.ndarray:
    """Convert a torch tensor / numpy array / sequence to a numpy array."""
    if data is None:
        return np.empty((0,))
    if hasattr(data, "detach"):  # torch.Tensor
        data = data.detach()
    if hasattr(data, "cpu"):
        data = data.cpu()
    if hasattr(data, "numpy"):
        data = data.numpy()
    return np.asarray(data)


def filter_person_detections(
    boxes_xyxy: Any,
    confidences: Any,
    class_ids: Any,
    conf_threshold: float = DEFAULT_CONFIDENCE,
) -> list[Detection]:
    """Keep only person detections with ``confidence >= conf_threshold``.

    Args:
        boxes_xyxy: ``(N, 4)`` array-like of boxes.
        confidences: ``(N,)`` array-like of scores.
        class_ids: ``(N,)`` array-like of class IDs.
        conf_threshold: Minimum confidence to keep a detection.

    Returns:
        List of :class:`Detection` sorted by descending confidence.
    """
    conf_threshold = _validate_threshold("conf_threshold", conf_threshold)

    boxes = _to_numpy(boxes_xyxy).reshape(-1, 4)
    confs = _to_numpy(confidences).reshape(-1)
    classes = _to_numpy(class_ids).reshape(-1)

    if not (len(boxes) == len(confs) == len(classes)):
        raise ValueError(
            "boxes, confidences and class_ids must have the same length "
            f"(got {len(boxes)}, {len(confs)}, {len(classes)})"
        )

    detections: list[Detection] = []
    for box, conf, cls in zip(boxes, confs, classes):
        if int(round(float(cls))) != PERSON_CLASS_ID:
            continue
        conf = float(conf)
        if not np.isfinite(conf) or conf < conf_threshold:
            continue
        if not np.all(np.isfinite(box)):
            continue
        x1, y1, x2, y2 = (float(v) for v in box)
        if x2 <= x1 or y2 <= y1:  # degenerate box
            continue
        detections.append(
            Detection(bbox=(x1, y1, x2, y2), confidence=conf, class_id=PERSON_CLASS_ID)
        )

    detections.sort(key=lambda d: d.confidence, reverse=True)
    return detections


class PersonDetector:
    """YOLO-based detector that returns only people.

    Example:
        >>> detector = PersonDetector(conf=0.5, iou=0.45)
        >>> detections = detector.detect(frame)  # frame: BGR np.ndarray
        >>> for d in detections:
        ...     print(d.bbox, d.confidence, d.class_id)
    """

    def __init__(
        self,
        model_path: str = DEFAULT_MODEL_PATH,
        conf: float = DEFAULT_CONFIDENCE,
        iou: float = DEFAULT_IOU,
        device: str | None = None,
        imgsz: int = 640,
        model: Any | None = None,
    ) -> None:
        """
        Args:
            model_path: Path/name of pretrained COCO YOLO weights.
            conf: Minimum confidence threshold (default 0.50).
            iou: IoU threshold for non-maximum suppression.
            device: Inference device, e.g. ``"cpu"``, ``"cuda:0"``. ``None`` = auto.
            imgsz: Inference image size.
            model: Optional pre-built model object (dependency injection / tests).
                If given, ``model_path`` is not loaded.
        """
        self.model_path = model_path
        self.conf = _validate_threshold("conf", conf)
        self.iou = _validate_threshold("iou", iou)
        self.device = device
        if int(imgsz) <= 0:
            raise ValueError(f"imgsz must be positive, got {imgsz}")
        self.imgsz = int(imgsz)
        self._model = model

    @property
    def model(self) -> Any:
        """Lazily load the YOLO model on first use."""
        if self._model is None:
            from ultralytics import YOLO  # imported lazily to keep import cheap

            logger.info("Loading YOLO model from %s", self.model_path)
            self._model = YOLO(self.model_path)
        return self._model

    def detect(self, frame: np.ndarray) -> list[Detection]:
        """Detect people in a single frame.

        Args:
            frame: Image as a ``(H, W, 3)`` numpy array (BGR, as from OpenCV).

        Returns:
            Person detections sorted by descending confidence.
        """
        if frame is None or not isinstance(frame, np.ndarray) or frame.size == 0:
            raise ValueError("frame must be a non-empty numpy array")

        results = self.model.predict(
            source=frame,
            conf=self.conf,
            iou=self.iou,
            classes=[PERSON_CLASS_ID],
            device=self.device,
            imgsz=self.imgsz,
            verbose=False,
        )
        return self._parse_results(results)

    def _parse_results(self, results: Iterable[Any] | None) -> list[Detection]:
        detections: list[Detection] = []
        for result in results or []:
            boxes = getattr(result, "boxes", None)
            if boxes is None or len(boxes) == 0:
                continue
            detections.extend(
                filter_person_detections(boxes.xyxy, boxes.conf, boxes.cls, self.conf)
            )
        detections.sort(key=lambda d: d.confidence, reverse=True)
        return detections


__all__: Sequence[str] = [
    "DEFAULT_CONFIDENCE",
    "DEFAULT_IOU",
    "DEFAULT_MODEL_PATH",
    "PERSON_CLASS_ID",
    "Detection",
    "PersonDetector",
    "filter_person_detections",
]
