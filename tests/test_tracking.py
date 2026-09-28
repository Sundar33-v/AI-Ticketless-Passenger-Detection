"""Tests for app.tracking.person_tracker (no model weights / camera required)."""

import os
import sys
import unittest
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.tracking import PersonTracker, TrackedPerson, TrackerConfig  # noqa: E402


def _fake_result(rows):
    """rows: list of (track_id|None, x1, y1, x2, y2, conf, cls)."""
    if not rows:
        return [SimpleNamespace(boxes=SimpleNamespace(id=None, xyxy=np.empty((0, 4)), conf=np.empty(0), cls=np.empty(0)))]
    ids = [r[0] for r in rows]
    boxes = SimpleNamespace(
        id=None if any(i is None for i in ids) else np.array(ids, dtype=float),
        xyxy=np.array([r[1:5] for r in rows], dtype=float),
        conf=np.array([r[5] for r in rows], dtype=float),
        cls=np.array([r[6] for r in rows], dtype=float),
    )
    return [SimpleNamespace(boxes=boxes)]


class FakeModel:
    """Mimics YOLO.track; replays pre-canned per-frame results and records kwargs."""

    def __init__(self, frames):
        self.frames = list(frames)
        self.calls = []

    def track(self, frame, **kwargs):
        self.calls.append(kwargs)
        return _fake_result(self.frames.pop(0))


FRAME = np.zeros((480, 640, 3), dtype=np.uint8)


class TestPersonTracker(unittest.TestCase):
    def test_uses_bytetrack_with_persist_and_person_class(self):
        model = FakeModel([[(1, 10, 10, 50, 100, 0.9, 0)]])
        PersonTracker(TrackerConfig(), model=model).track(FRAME)
        kw = model.calls[0]
        self.assertEqual(kw["tracker"], "bytetrack.yaml")
        self.assertTrue(kw["persist"])
        self.assertEqual(kw["classes"], [0])

    def test_stable_track_ids_across_frames(self):
        frames = [
            [(1, 100, 100, 150, 250, 0.9, 0), (2, 300, 100, 350, 250, 0.8, 0)],
            [(1, 104, 108, 154, 258, 0.9, 0), (2, 296, 104, 346, 254, 0.8, 0)],
            [(1, 108, 116, 158, 266, 0.9, 0), (2, 292, 108, 342, 258, 0.8, 0)],
        ]
        tracker = PersonTracker(model=FakeModel(frames))
        for _ in frames:
            people = tracker.track(FRAME)
            self.assertEqual(sorted(p.track_id for p in people), [1, 2])
        self.assertEqual(tracker.active_track_ids, [1, 2])
        state = tracker.get_track_state(1)
        self.assertEqual((state.first_seen_frame, state.last_seen_frame, state.hits), (1, 3, 3))

    def test_track_state_survives_short_gap_and_is_pruned_after_timeout(self):
        frames = [[(5, 0, 0, 10, 10, 0.9, 0)], [], [(5, 1, 1, 11, 11, 0.9, 0)]] + [[]] * 3
        tracker = PersonTracker(TrackerConfig(max_missing_frames=2), model=FakeModel(frames))
        tracker.track(FRAME)
        tracker.track(FRAME)
        self.assertEqual(tracker.active_track_ids, [])
        tracker.track(FRAME)
        self.assertEqual(tracker.get_track_state(5).hits, 2)  # same ID resumed
        for _ in range(3):
            tracker.track(FRAME)
        self.assertIsNone(tracker.get_track_state(5))

    def test_unassigned_ids_and_non_person_classes_are_ignored(self):
        self.assertEqual(PersonTracker.parse_results(_fake_result([(None, 0, 0, 1, 1, 0.9, 0)])), [])
        people = PersonTracker.parse_results(
            _fake_result([(3, 0, 0, 10, 20, 0.9, 0), (4, 0, 0, 10, 20, 0.9, 2)])
        )
        self.assertEqual([p.track_id for p in people], [3])
        self.assertEqual(people[0].bottom_center, (5.0, 20.0))
        self.assertEqual(PersonTracker.parse_results([]), [])

    def test_reset_disables_persist_once(self):
        model = FakeModel([[(1, 0, 0, 1, 1, 0.9, 0)]] * 3)
        tracker = PersonTracker(model=model)
        tracker.track(FRAME)
        tracker.reset()
        self.assertEqual(tracker.active_track_ids, [])
        tracker.track(FRAME)
        tracker.track(FRAME)
        self.assertEqual([c["persist"] for c in model.calls], [True, False, True])


class TestRealByteTrackStableIds(unittest.TestCase):
    """Runs Ultralytics' actual BYTETracker on synthetic detections."""

    def test_bytetrack_keeps_ids_for_moving_people(self):
        try:
            from ultralytics.engine.results import Boxes
            from ultralytics.trackers.byte_tracker import BYTETracker
            from ultralytics.utils import YAML, IterableSimpleNamespace
            from ultralytics.utils.checks import check_yaml
        except Exception as exc:  # pragma: no cover - version dependent
            self.skipTest(f"ultralytics tracker API unavailable: {exc}")

        cfg = IterableSimpleNamespace(**YAML.load(check_yaml("bytetrack.yaml")))
        bt = BYTETracker(cfg)
        ids_per_frame = []
        for f in range(15):
            # Two people walking in opposite directions: [x1, y1, x2, y2, conf, cls]
            data = np.array(
                [
                    [100 + 3 * f, 50 + 6 * f, 160 + 3 * f, 230 + 6 * f, 0.9, 0],
                    [400 - 3 * f, 60 + 2 * f, 460 - 3 * f, 240 + 2 * f, 0.85, 0],
                ],
                dtype=np.float32,
            )
            out = bt.update(Boxes(data, (480, 640)), FRAME)
            if f >= 2 and len(out):  # allow ByteTrack to confirm new tracks
                # output columns: x1, y1, x2, y2, track_id, score, cls, idx
                ids_per_frame.append({int(r[7]): int(r[4]) for r in out})

        self.assertGreater(len(ids_per_frame), 5)
        self.assertTrue(all(m == ids_per_frame[0] for m in ids_per_frame))
        self.assertEqual(len(set(ids_per_frame[0].values())), 2)


if __name__ == "__main__":
    unittest.main()
