"""Unit tests for app.detection (person-only YOLO detector).

Written with ``unittest`` so they run under both ``pytest`` and
``python -m unittest``. The YOLO model is replaced by a fake, so no weights are
downloaded and no GPU is required.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.detection import (  # noqa: E402
    DEFAULT_CONFIDENCE,
    PERSON_CLASS_ID,
    Detection,
    PersonDetector,
    filter_person_detections,
)

# COCO class IDs for objects that must never reach the passenger pipeline.
COCO_BACKPACK = 24
COCO_HANDBAG = 26
COCO_SUITCASE = 28
COCO_BOTTLE = 39
COCO_CHAIR = 56
COCO_DINING_TABLE = 60
COCO_LAPTOP = 63
COCO_SCISSORS = 76  # "tool"
NON_PERSON_CLASSES = [
    COCO_BACKPACK,
    COCO_HANDBAG,
    COCO_SUITCASE,
    COCO_BOTTLE,
    COCO_CHAIR,
    COCO_DINING_TABLE,
    COCO_LAPTOP,
    COCO_SCISSORS,
]


class _FakeBoxes:
    def __init__(self, xyxy, conf, cls):
        self.xyxy = np.asarray(xyxy, dtype=np.float32).reshape(-1, 4)
        self.conf = np.asarray(conf, dtype=np.float32)
        self.cls = np.asarray(cls, dtype=np.float32)

    def __len__(self) -> int:
        return len(self.conf)


class _FakeModel:
    """Mimics ultralytics.YOLO.predict, deliberately ignoring ``classes``/``conf``
    so the detector's own filtering is exercised."""

    def __init__(self, xyxy, conf, cls):
        self._boxes = _FakeBoxes(xyxy, conf, cls)
        self.calls: list[dict] = []

    def predict(self, **kwargs):
        self.calls.append(kwargs)
        return [SimpleNamespace(boxes=self._boxes)]


FRAME = np.zeros((480, 640, 3), dtype=np.uint8)
BOX = [10.0, 20.0, 110.0, 220.0]


class TestPersonAccepted(unittest.TestCase):
    def test_person_class_accepted(self):
        dets = filter_person_detections([BOX], [0.9], [PERSON_CLASS_ID])
        self.assertEqual(len(dets), 1)
        det = dets[0]
        self.assertIsInstance(det, Detection)
        self.assertEqual(det.class_id, PERSON_CLASS_ID)
        self.assertEqual(det.bbox, tuple(BOX))
        self.assertAlmostEqual(det.confidence, 0.9, places=5)

    def test_detector_returns_clean_person_data(self):
        model = _FakeModel([BOX], [0.8], [PERSON_CLASS_ID])
        dets = PersonDetector(model=model).detect(FRAME)
        self.assertEqual(len(dets), 1)
        self.assertEqual(
            set(dets[0].to_dict()), {"bbox", "confidence", "class_id"}
        )
        self.assertEqual(dets[0].to_dict()["class_id"], 0)

    def test_detector_requests_person_class_only(self):
        model = _FakeModel([BOX], [0.8], [PERSON_CLASS_ID])
        PersonDetector(model=model, conf=0.6, iou=0.4).detect(FRAME)
        call = model.calls[0]
        self.assertEqual(call["classes"], [PERSON_CLASS_ID])
        self.assertEqual(call["conf"], 0.6)
        self.assertEqual(call["iou"], 0.4)


class TestNonPersonRejected(unittest.TestCase):
    def test_each_non_person_class_rejected(self):
        for cls in NON_PERSON_CLASSES:
            with self.subTest(cls=cls):
                self.assertEqual(filter_person_detections([BOX], [0.99], [cls]), [])

    def test_mixed_frame_keeps_only_people(self):
        classes = [PERSON_CLASS_ID, *NON_PERSON_CLASSES, PERSON_CLASS_ID]
        n = len(classes)
        model = _FakeModel([BOX] * n, [0.95] * n, classes)
        dets = PersonDetector(model=model).detect(FRAME)
        self.assertEqual(len(dets), 2)
        self.assertTrue(all(d.class_id == PERSON_CLASS_ID for d in dets))

    def test_no_detections(self):
        model = _FakeModel(np.empty((0, 4)), [], [])
        self.assertEqual(PersonDetector(model=model).detect(FRAME), [])


class TestConfidenceFiltering(unittest.TestCase):
    def test_default_threshold_is_050(self):
        self.assertEqual(DEFAULT_CONFIDENCE, 0.50)
        self.assertEqual(PersonDetector(model=object()).conf, 0.50)

    def test_below_threshold_rejected_at_threshold_kept(self):
        confs = [0.49, 0.50, 0.75]
        dets = filter_person_detections([BOX] * 3, confs, [PERSON_CLASS_ID] * 3, 0.50)
        self.assertEqual([round(d.confidence, 2) for d in dets], [0.75, 0.50])

    def test_detector_applies_custom_conf(self):
        model = _FakeModel([BOX] * 3, [0.55, 0.65, 0.85], [PERSON_CLASS_ID] * 3)
        dets = PersonDetector(model=model, conf=0.7).detect(FRAME)
        self.assertEqual(len(dets), 1)
        self.assertAlmostEqual(dets[0].confidence, 0.85, places=5)

    def test_results_sorted_by_confidence(self):
        model = _FakeModel([BOX] * 3, [0.6, 0.9, 0.7], [PERSON_CLASS_ID] * 3)
        confs = [d.confidence for d in PersonDetector(model=model).detect(FRAME)]
        self.assertEqual(confs, sorted(confs, reverse=True))

    def test_invalid_thresholds_raise(self):
        for kwargs in ({"conf": 1.5}, {"conf": -0.1}, {"iou": 2.0}):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    PersonDetector(model=object(), **kwargs)


class TestInputValidation(unittest.TestCase):
    def test_empty_frame_raises(self):
        detector = PersonDetector(model=_FakeModel([BOX], [0.9], [0]))
        with self.assertRaises(ValueError):
            detector.detect(np.empty((0, 0, 3), dtype=np.uint8))

    def test_degenerate_box_rejected(self):
        self.assertEqual(
            filter_person_detections([[50, 50, 50, 100]], [0.9], [PERSON_CLASS_ID]), []
        )


if __name__ == "__main__":
    unittest.main()
