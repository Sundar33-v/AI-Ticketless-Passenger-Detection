"""Tests for app.passenger.entry_detector."""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.passenger import (  # noqa: E402
    BOTH,
    BOTTOM_TO_TOP,
    LEFT_TO_RIGHT,
    RIGHT_TO_LEFT,
    TOP_TO_BOTTOM,
    EntryEvent,
    EntryLineConfig,
    EntryLineDetector,
)
from app.tracking import TrackedPerson  # noqa: E402

FRAME_SIZE = (640, 480)  # (width, height)


def person(tid, cx, foot_y, w=40, h=120):
    return TrackedPerson(tid, (cx - w / 2, foot_y - h, cx + w / 2, foot_y), 0.9)


def horizontal(direction=TOP_TO_BOTTOM, **kw):
    params = dict(orientation="horizontal", position=240, normalized=False, entry_direction=direction,
                  cooldown_seconds=2.0, hysteresis_px=5)
    params.update(kw)
    return EntryLineDetector(EntryLineConfig(**params))


def run(det, tid, ys, t0=0.0, dt=0.1, x=320):
    events = []
    for i, y in enumerate(ys):
        events += det.update([person(tid, x, y)], timestamp=t0 + i * dt, frame_size=FRAME_SIZE)
    return events


class TestHorizontalLine(unittest.TestCase):
    def test_top_to_bottom_crossing(self):
        events = run(horizontal(), 1, [200, 220, 238, 250, 270])
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertIsInstance(ev, EntryEvent)
        self.assertEqual((ev.track_id, ev.direction), (1, TOP_TO_BOTTOM))
        self.assertAlmostEqual(ev.timestamp, 0.3)
        self.assertEqual(set(ev.to_dict()), {"track_id", "direction", "timestamp"})

    def test_bottom_to_top_crossing(self):
        events = run(horizontal(BOTTOM_TO_TOP), 2, [300, 260, 242, 230, 200])
        self.assertEqual([(e.track_id, e.direction) for e in events], [(2, BOTTOM_TO_TOP)])

    def test_opposite_direction_ignored_when_not_configured(self):
        self.assertEqual(run(horizontal(TOP_TO_BOTTOM), 3, [300, 260, 200]), [])

    def test_both_directions_reports_direction(self):
        det = horizontal(BOTH, cooldown_seconds=0)
        events = run(det, 4, [200, 280]) + run(det, 5, [280, 200], t0=5)
        self.assertEqual([(e.track_id, e.direction) for e in events], [(4, TOP_TO_BOTTOM), (5, BOTTOM_TO_TOP)])

    def test_no_event_without_crossing_or_on_first_frame(self):
        det = horizontal()
        self.assertEqual(run(det, 6, [300, 310, 320]), [])  # starts below: never crossed
        self.assertEqual(run(det, 7, [200, 210, 239, 244]), [])  # stays within hysteresis band


class TestDuplicatePrevention(unittest.TestCase):
    def test_same_track_crossing_repeatedly_emits_once(self):
        det = horizontal(cooldown_seconds=0.5)
        # Down, back up, down again - spread over 10 s so cooldown has long expired.
        events = run(det, 10, [200, 280, 200, 280, 200, 280], dt=2.0)
        self.assertEqual([(e.track_id, e.direction) for e in events], [(10, TOP_TO_BOTTOM)])
        self.assertTrue(det.has_entered(10))

    def test_jitter_around_line_does_not_duplicate(self):
        det = horizontal()
        events = run(det, 11, [230, 246, 234, 247, 233, 250, 260])
        self.assertEqual(len(events), 1)

    def test_cooldown_blocks_opposite_bounce_then_allows_after_expiry(self):
        det = horizontal(BOTH, cooldown_seconds=2.0)
        first = run(det, 12, [200, 280], t0=0.0, dt=0.1)  # event at t=0.1
        bounce = run(det, 12, [200], t0=0.5)  # within cooldown -> suppressed
        self.assertEqual(len(first), 1)
        self.assertEqual(bounce, [])
        later = run(det, 12, [280, 200], t0=5.0)  # after cooldown, new direction
        self.assertEqual([e.direction for e in later], [BOTTOM_TO_TOP])

    def test_different_track_ids_are_independent(self):
        det = horizontal()
        events = []
        for i, y in enumerate([200, 280]):
            events += det.update([person(1, 100, y), person(2, 400, y)], timestamp=i * 0.1, frame_size=FRAME_SIZE)
        self.assertEqual(sorted(e.track_id for e in events), [1, 2])

    def test_duplicate_blocked_even_after_track_state_timeout(self):
        det = horizontal(track_timeout_seconds=1.0, cooldown_seconds=0)
        self.assertEqual(len(run(det, 13, [200, 280])), 1)
        det.update([], timestamp=10.0, frame_size=FRAME_SIZE)  # prunes side state
        self.assertEqual(run(det, 13, [200, 280], t0=11.0), [])


class TestVerticalAndConfig(unittest.TestCase):
    def test_vertical_left_to_right_and_right_to_left(self):
        det = EntryLineDetector(EntryLineConfig(orientation="vertical", position=0.5, entry_direction=BOTH,
                                                cooldown_seconds=0))
        events = []
        for i, x in enumerate([200, 300, 400]):
            events += det.update([person(1, x, 300)], timestamp=i, frame_size=FRAME_SIZE)
        for i, x in enumerate([450, 250]):
            events += det.update([person(2, x, 300)], timestamp=10 + i, frame_size=FRAME_SIZE)
        self.assertEqual([(e.track_id, e.direction) for e in events], [(1, LEFT_TO_RIGHT), (2, RIGHT_TO_LEFT)])

    def test_normalized_position_and_span(self):
        det = EntryLineDetector(EntryLineConfig(position=0.5, span_start=0.25, span_end=0.75, cooldown_seconds=0))
        self.assertEqual(det.line_endpoints(FRAME_SIZE), ((160, 240), (480, 240)))
        self.assertEqual(run(det, 1, [200, 280], x=50), [])  # outside span
        self.assertEqual(len(run(det, 2, [200, 280], x=320)), 1)  # inside span

    def test_from_dict_and_validation(self):
        cfg = EntryLineConfig.from_dict({"orientation": "Vertical", "position": 100, "normalized": False,
                                         "entry_direction": "right_to_left", "unknown_key": 1})
        self.assertEqual((cfg.orientation, cfg.entry_direction), ("vertical", RIGHT_TO_LEFT))
        with self.assertRaises(ValueError):
            EntryLineConfig(orientation="horizontal", entry_direction=LEFT_TO_RIGHT)
        with self.assertRaises(ValueError):
            EntryLineConfig(orientation="diagonal")
        with self.assertRaises(ValueError):
            EntryLineDetector(EntryLineConfig(position=0.5)).update([person(1, 1, 1)])  # no frame_size

    def test_accepts_mapping_tracks_and_update_point(self):
        det = horizontal()
        det.update([{"track_id": 9, "bbox": (0, 80, 10, 200)}], timestamp=0)
        self.assertEqual(len(det.update([{"track_id": 9, "bbox": (0, 160, 10, 280)}], timestamp=1)), 1)
        det.update_point(8, 10, 200, timestamp=0)
        self.assertEqual(det.update_point(8, 10, 280, timestamp=1).direction, TOP_TO_BOTTOM)


if __name__ == "__main__":
    unittest.main()
