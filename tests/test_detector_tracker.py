"""Tests for app.detector and app.tracker (no model weights, camera or GPU required)."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

import app.detector as detector
from app.config import PROJECT_ROOT
from app.detector import (
    PERSON_CLASS_ID,
    ModelLoadError,
    ModelNotFoundError,
    PersonBox,
    load_model,
    parse_person_boxes,
    resolve_device,
)
from app.settings_schema import ConfigError, DetectionConfig, TrackingConfig
from app.tracker import PersonTracker, TrackedPerson, prepare_tracker_yaml

FRAME = np.zeros((480, 640, 3), dtype=np.uint8)
BOX = (10.0, 20.0, 110.0, 220.0)
# COCO classes that must never reach the passenger pipeline:
# car, backpack, handbag, suitcase, bottle, chair, dining table, laptop, scissors ("tool").
NON_PERSON_CLASSES = [2, 24, 26, 28, 39, 56, 60, 63, 76]


def _fake_result(rows, with_ids=True):
    """rows: list of (track_id|None, x1, y1, x2, y2, conf, cls)."""
    if not rows:
        return [SimpleNamespace(boxes=SimpleNamespace(id=None, xyxy=np.empty((0, 4)), conf=np.empty(0),
                                                      cls=np.empty(0)))]
    ids = [r[0] for r in rows]
    boxes = SimpleNamespace(
        id=None if (not with_ids or any(i is None for i in ids)) else np.array(ids, dtype=float),
        xyxy=np.array([r[1:5] for r in rows], dtype=float),
        conf=np.array([r[5] for r in rows], dtype=float),
        cls=np.array([r[6] for r in rows], dtype=float),
    )
    return [SimpleNamespace(boxes=boxes)]


class FakeModel:
    """Mimics YOLO.track; replays per-frame rows and records the kwargs.

    It deliberately ignores ``classes``/``conf`` so our own filtering is exercised.
    """

    def __init__(self, frames):
        self.frames = list(frames)
        self.calls = []

    def track(self, frame, **kwargs):
        self.calls.append(kwargs)
        return _fake_result(self.frames.pop(0))


def _tracker(frames, tracking=None, detection=None, yaml_path="effective.yaml"):
    model = FakeModel(frames)
    return PersonTracker(model, tracking or TrackingConfig(), detection or DetectionConfig(), yaml_path), model


# ------------------------------------------------------------ person parsing
def test_person_box_accepted():
    [box] = parse_person_boxes(_fake_result([(1, *BOX, 0.9, PERSON_CLASS_ID)]))
    assert isinstance(box, PersonBox)
    assert (box.track_id, box.bbox, box.class_id) == (1, BOX, 0)
    assert box.confidence == pytest.approx(0.9)


@pytest.mark.parametrize("cls", NON_PERSON_CLASSES)
def test_each_non_person_class_rejected(cls):
    assert parse_person_boxes(_fake_result([(1, *BOX, 0.99, cls)])) == []


def test_mixed_classes_keep_only_people():
    rows = [(i + 1, *BOX, 0.95, cls) for i, cls in enumerate([0, *NON_PERSON_CLASSES, 0])]
    people = parse_person_boxes(_fake_result(rows))
    assert [p.track_id for p in people] == [1, len(rows)]
    assert all(p.class_id == PERSON_CLASS_ID for p in people)


def test_untracked_boxes_excluded_unless_ids_not_required():
    rows = [(None, *BOX, 0.9, 0)]
    assert parse_person_boxes(_fake_result(rows)) == []
    [box] = parse_person_boxes(_fake_result(rows), require_ids=False)
    assert box.track_id is None


def test_degenerate_and_non_finite_boxes_rejected():
    rows = [(1, 50, 50, 50, 100, 0.9, 0), (2, 0, 0, 10, 10, float("nan"), 0), (3, 0, 0, np.inf, 10, 0.9, 0),
            (4, 0, 0, 10, 10, 0.9, 0)]
    assert [p.track_id for p in parse_person_boxes(_fake_result(rows))] == [4]


def test_empty_results():
    assert parse_person_boxes([]) == []
    assert parse_person_boxes(None) == []
    assert parse_person_boxes(_fake_result([])) == []
    assert parse_person_boxes(SimpleNamespace(boxes=None)) == []


def test_invalid_detection_thresholds_raise():
    for data in ({"confidence": 1.5}, {"confidence": -0.1}, {"iou": 2.0}):
        with pytest.raises(ConfigError):
            DetectionConfig.from_dict(data)


# -------------------------------------------------------------- model / device
def test_missing_model_message_and_no_ultralytics_import(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "ultralytics", None)  # any import attempt would raise ImportError
    missing = tmp_path / "missing.pt"
    with pytest.raises(ModelNotFoundError) as exc:
        load_model(DetectionConfig(model_path=str(missing)))
    msg = str(exc.value)
    assert str(missing.resolve()) in msg
    assert "YOLO('yolov8n.pt')" in msg and "models/README.md" in msg


def test_relative_model_path_resolved_against_project_root(monkeypatch):
    monkeypatch.setitem(sys.modules, "ultralytics", None)
    with pytest.raises(ModelNotFoundError) as exc:
        load_model(DetectionConfig(model_path="models/does_not_exist.pt"))
    assert str(PROJECT_ROOT / "models" / "does_not_exist.pt") in str(exc.value)


def test_load_model_passes_absolute_path_and_wraps_errors(tmp_path, monkeypatch):
    weights = tmp_path / "w.pt"
    weights.write_bytes(b"fake")
    seen = []
    monkeypatch.setitem(sys.modules, "ultralytics", SimpleNamespace(YOLO=lambda p: seen.append(p) or "model"))
    assert load_model(DetectionConfig(model_path=str(weights))) == "model"
    assert seen == [str(weights.resolve())]

    def broken(_path):
        raise RuntimeError("bad weights")

    monkeypatch.setitem(sys.modules, "ultralytics", SimpleNamespace(YOLO=broken))
    with pytest.raises(ModelLoadError, match="bad weights"):
        load_model(DetectionConfig(model_path=str(weights)))
    assert issubclass(ModelLoadError, ModelNotFoundError)


def test_resolve_device(monkeypatch):
    monkeypatch.setattr(detector, "_cuda_available", lambda: False)
    assert resolve_device("cpu") == "cpu"
    assert resolve_device("auto") == "cpu"
    assert resolve_device("cuda:1") == "cpu"
    monkeypatch.setattr(detector, "_cuda_available", lambda: True)
    assert resolve_device("auto") == "cuda:0"
    assert resolve_device("cuda:1") == "cuda:1"
    with pytest.raises(ValueError):
        resolve_device("tpu")


# ------------------------------------------------------- tracker YAML (M1)
def test_committed_bytetrack_yaml_values():
    data = yaml.safe_load((PROJECT_ROOT / "config" / "bytetrack.yaml").read_text(encoding="utf-8"))
    assert data == {"tracker_type": "bytetrack", "track_high_thresh": 0.25, "track_low_thresh": 0.1,
                    "new_track_thresh": 0.35, "track_buffer": 60, "match_thresh": 0.8, "fuse_score": True}


def test_effective_yaml_uses_detection_confidence(tmp_path, caplog):
    out = prepare_tracker_yaml(TrackingConfig(), DetectionConfig(), tmp_path / "runtime")
    assert out == tmp_path / "runtime" / "bytetrack_effective.yaml"
    assert yaml.safe_load(out.read_text(encoding="utf-8"))["new_track_thresh"] == 0.35

    with caplog.at_level(logging.WARNING, logger="app.tracker"):
        out = prepare_tracker_yaml(TrackingConfig(), DetectionConfig(confidence=0.5), tmp_path / "runtime")
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert data["new_track_thresh"] == 0.5 and data["track_buffer"] == 60
    assert "effective value comes from detection.confidence" in caplog.text


def test_input_confidence_above_low_thresh_rejected(tmp_path):
    with pytest.raises(ConfigError, match="^tracking.input_confidence:"):
        prepare_tracker_yaml(TrackingConfig(input_confidence=0.2), DetectionConfig(), tmp_path)


def test_detection_confidence_below_low_thresh_rejected(tmp_path):
    with pytest.raises(ConfigError, match="^detection.confidence:"):
        prepare_tracker_yaml(TrackingConfig(input_confidence=0.05), DetectionConfig(confidence=0.05), tmp_path)


def test_wrong_tracker_type_rejected(tmp_path):
    botsort = tmp_path / "botsort.yaml"
    botsort.write_text("tracker_type: botsort\ntrack_low_thresh: 0.1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="tracker_type must be 'bytetrack'"):
        prepare_tracker_yaml(TrackingConfig(tracker_config=str(botsort)), DetectionConfig(), tmp_path)


def test_missing_tracker_yaml_rejected(tmp_path):
    with pytest.raises(ConfigError, match="^tracking.tracker_config:"):
        prepare_tracker_yaml(TrackingConfig(tracker_config=str(tmp_path / "x.yaml")), DetectionConfig(), tmp_path)


# ------------------------------------------------------------ PersonTracker
def test_track_call_arguments():
    tracker, model = _tracker([[(1, *BOX, 0.9, 0)]], detection=DetectionConfig(iou=0.6, imgsz=480))
    tracker.track(FRAME)
    kw = model.calls[0]
    assert kw["tracker"] == "effective.yaml"
    assert kw["persist"] is True and kw["classes"] == [0] and kw["verbose"] is False
    assert kw["conf"] == 0.1  # tracking.input_confidence, never detection.confidence
    assert (kw["iou"], kw["imgsz"], kw["device"]) == (0.6, 480, "cpu")


def test_never_accepted_track_not_returned():
    frames = [[(7, *BOX, 0.2, 0)], [(7, *BOX, 0.3, 0)], [(7, *BOX, 0.34, 0)]]
    tracker, _ = _tracker(frames)
    for _ in frames:
        assert tracker.track(FRAME) == []
    assert tracker.get_track_state(7).hits == 3
    assert tracker.active_track_ids == []


def test_accepted_track_keeps_low_score_continuation_box():
    tracker, _ = _tracker([[(3, *BOX, 0.9, 0)], [(3, *BOX, 0.15, 0)]])
    tracker.track(FRAME)
    [person] = tracker.track(FRAME)
    assert person.track_id == 3 and person.confidence == pytest.approx(0.15)
    assert person.hits == 2


def test_track_accepted_once_threshold_reached():
    tracker, _ = _tracker([[(4, *BOX, 0.2, 0)], [(4, *BOX, 0.4, 0)], [(4, *BOX, 0.2, 0)]])
    assert tracker.track(FRAME) == []
    [second] = tracker.track(FRAME)
    [third] = tracker.track(FRAME)
    assert (second.hits, second.first_seen_frame) == (2, 1)
    assert (third.hits, third.first_seen_frame) == (3, 1)


def test_stable_track_ids_hits_and_first_seen():
    frames = [
        [(1, 100, 100, 150, 250, 0.9, 0), (2, 300, 100, 350, 250, 0.8, 0)],
        [(1, 104, 108, 154, 258, 0.9, 0), (2, 296, 104, 346, 254, 0.8, 0)],
        [(1, 108, 116, 158, 266, 0.9, 0), (2, 292, 108, 342, 258, 0.8, 0), (5, 0, 0, 20, 40, 0.9, 0)],
    ]
    tracker, _ = _tracker(frames)
    results = [tracker.track(FRAME) for _ in frames]
    assert [sorted(p.track_id for p in r) for r in results] == [[1, 2], [1, 2], [1, 2, 5]]
    by_id = {p.track_id: p for p in results[-1]}
    assert (by_id[1].hits, by_id[1].first_seen_frame) == (3, 1)
    assert (by_id[5].hits, by_id[5].first_seen_frame) == (1, 3)
    assert isinstance(by_id[1], TrackedPerson)
    assert tracker.active_track_ids == [1, 2, 5] and tracker.frame_index == 3
    state = tracker.get_track_state(1)
    assert (state.first_seen_frame, state.last_seen_frame, state.hits) == (1, 3, 3)


def test_track_state_survives_short_gap_and_is_pruned_after_timeout():
    frames = [[(5, 0, 0, 10, 10, 0.9, 0)], [], [(5, 1, 1, 11, 11, 0.9, 0)]] + [[]] * 3
    tracker, _ = _tracker(frames, tracking=TrackingConfig(max_missing_frames=2))
    tracker.track(FRAME)
    tracker.track(FRAME)
    assert tracker.active_track_ids == []
    [resumed] = tracker.track(FRAME)
    assert resumed.hits == 2 and resumed.first_seen_frame == 1  # same ID resumed
    for _ in range(3):
        tracker.track(FRAME)
    assert tracker.get_track_state(5) is None


def test_non_person_and_untracked_boxes_never_returned():
    tracker, _ = _tracker([[(3, 0, 0, 10, 20, 0.9, 0), (4, 0, 0, 10, 20, 0.9, 2)], [(None, 0, 0, 5, 5, 0.9, 0)]])
    [person] = tracker.track(FRAME)
    assert person.track_id == 3 and person.bottom_center == (5.0, 20.0) and person.center == (5.0, 10.0)
    assert tracker.track(FRAME) == []


def test_reset_disables_persist_once():
    tracker, model = _tracker([[(1, 0, 0, 1, 1, 0.9, 0)]] * 3)
    tracker.track(FRAME)
    tracker.reset()
    assert tracker.active_track_ids == [] and tracker.frame_index == 0
    tracker.track(FRAME)
    tracker.track(FRAME)
    assert [c["persist"] for c in model.calls] == [True, False, True]


def test_tracked_person_positional_defaults():
    person = TrackedPerson(1, BOX, 0.9)
    assert (person.class_id, person.hits, person.first_seen_frame) == (0, 1, 0)


# --------------------------------------------------- real ByteTrack (integration)
def test_bytetrack_keeps_ids_for_moving_people_with_effective_yaml(tmp_path):
    try:
        from ultralytics.engine.results import Boxes
        from ultralytics.trackers.byte_tracker import BYTETracker
        from ultralytics.utils import IterableSimpleNamespace
    except Exception as exc:  # pragma: no cover - version dependent
        pytest.skip(f"ultralytics tracker API unavailable: {exc}")

    effective = prepare_tracker_yaml(TrackingConfig(), DetectionConfig(), tmp_path)
    cfg = IterableSimpleNamespace(**yaml.safe_load(Path(effective).read_text(encoding="utf-8")))
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

    assert len(ids_per_frame) > 5
    assert all(m == ids_per_frame[0] for m in ids_per_frame)
    assert len(set(ids_per_frame[0].values())) == 2
