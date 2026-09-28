"""Person detection package (YOLO, COCO class 0 only)."""

from app.detection.person_detector import (
    DEFAULT_CONFIDENCE,
    DEFAULT_IOU,
    DEFAULT_MODEL_PATH,
    PERSON_CLASS_ID,
    Detection,
    PersonDetector,
    filter_person_detections,
)

__all__ = [
    "DEFAULT_CONFIDENCE",
    "DEFAULT_IOU",
    "DEFAULT_MODEL_PATH",
    "PERSON_CLASS_ID",
    "Detection",
    "PersonDetector",
    "filter_person_detections",
]
