"""app/evaluation.py (metrics, matching, parsers, storage) and the app.evaluate CLI against temp DBs."""

from __future__ import annotations

import ast
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import app.evaluate as evaluate
from app.database import Database
from app.evaluation import (
    NA_MESSAGE,
    Counts,
    EvaluationInputError,
    EvaluationRecord,
    iou,
    latest_evaluation,
    list_evaluations,
    match_frame,
    parse_mot,
    parse_yolo_labels,
    record_from_import,
    scores,
    store_evaluation,
)

ROOT = Path(__file__).resolve().parents[1]
FIXED_NOW = datetime(2026, 1, 15, 5, 30, tzinfo=timezone.utc)

A = (0.0, 0.0, 10.0, 10.0)


@pytest.fixture(autouse=True)
def no_file_logging(monkeypatch):
    monkeypatch.setattr(evaluate, "configure_logging", lambda *a, **k: None)


@pytest.fixture
def conn(db):
    c = db.connect()
    try:
        yield c
    finally:
        c.close()


def counts(tp, fp, fn, images=1, img=(0, 0, 0, None)):
    return Counts(images, tp + fn, tp + fp, tp, fp, fn, *([None] * 4 if img[3] is None else img))


# ------------------------------------------------------------------ IoU and matching
def test_iou_cases():
    assert iou(A, A) == 1.0
    assert iou(A, (20, 20, 30, 30)) == 0.0
    assert iou(A, (10, 0, 20, 10)) == 0.0  # touching edges
    assert iou(A, (0, 0, 10, 5)) == 0.5
    assert iou(A, (5, 0, 15, 10)) == pytest.approx(50 / 150)


def test_match_frame_edges():
    assert match_frame([A], [((0, 0, 10, 5), 0.9)], 0.5) == (1, 0, 0)          # IoU exactly 0.5 -> TP
    assert match_frame([A], [((0, 0, 10, 4.9), 0.9)], 0.5) == (0, 1, 1)        # 0.49 -> FP
    assert match_frame([A], [(A, 0.9), (A, 0.8)], 0.5) == (1, 1, 0)            # two on one GT
    assert match_frame([], [(A, 0.9)], 0.5) == (0, 1, 0)
    assert match_frame([A], [], 0.5) == (0, 0, 1)
    # confidence order: the higher-confidence, worse box takes the GT first
    gt = [A]
    pred = [((0, 0, 10, 9), 0.3), ((0, 0, 10, 6), 0.9)]
    assert match_frame(gt, pred, 0.5) == (1, 1, 0)


# ------------------------------------------------------------------ scores
def test_zero_denominators_are_none():
    assert scores(counts(0, 0, 3)).precision is None                    # no predictions
    assert scores(counts(0, 2, 0)).recall is None                       # no GT
    empty = scores(Counts())
    assert (empty.accuracy, empty.precision, empty.recall, empty.f1) == (None, None, None, None)
    assert scores(counts(5, 1, 1)).accuracy is None                     # TN unknown


def test_f1_rules():
    assert scores(counts(0, 3, 4)).f1 == 0.0                            # P = R = 0
    assert scores(counts(0, 3, 0)).f1 is None                           # recall N/A
    assert scores(counts(0, 0, 3)).f1 is None                           # precision N/A
    s = scores(counts(8, 2, 4))
    assert s.precision == pytest.approx(0.8) and s.recall == pytest.approx(2 / 3)
    assert round(s.f1, 4) == 0.7273


def test_image_level_accuracy():
    c = Counts()
    c = c.add_frame([A], [(A, 0.9)], 0.5)        # img TP
    c = c.add_frame([], [], 0.5)                 # img TN
    c = c.add_frame([], [(A, 0.9)], 0.5)         # img FP
    c = c.add_frame([A], [], 0.5)                # img FN
    assert (c.img_tp, c.img_fp, c.img_fn, c.img_tn, c.images) == (1, 1, 1, 1, 4)
    assert scores(c).accuracy == 0.5
    unknown = Counts().add_frame([A], [(A, 0.9)], 0.5, presence_known=False)
    assert unknown.img_tp is None and scores(unknown).accuracy is None


# ------------------------------------------------------------------ parsers
def test_yolo_parser(tmp_path):
    lbl = tmp_path / "a.txt"
    lbl.write_text("0 0.5 0.5 0.2 0.4\n2 0.5 0.5 0.1 0.1\n\n", encoding="utf-8")
    assert parse_yolo_labels(lbl, 100, 50, 0) == [(40.0, 15.0, 60.0, 35.0)]
    assert parse_yolo_labels(tmp_path / "missing.txt", 100, 50, 0) == []
    for bad in ("0 0.5 0.5 0.2", "0 1.5 0.5 0.2 0.2", "0 0.5 0.5 0 0.2", "-1 0.5 0.5 0.2 0.2", "x 1 1 1 1"):
        lbl.write_text(bad + "\n", encoding="utf-8")
        with pytest.raises(EvaluationInputError, match=r"a\.txt:1"):
            parse_yolo_labels(lbl, 100, 50, 0)


def test_mot_parser(tmp_path):
    gt = tmp_path / "gt.txt"
    gt.write_text("1,1,0,0,10,10\n2,1,0,0,10,10,1,1,1\n2,2,0,0,5,5,0,1,1\n2,3,0,0,5,5,1,8,1\n3,4,0,0,5,5,1,7,0.5\n",
                  encoding="utf-8")
    frames = parse_mot(gt, ground_truth=True)
    assert len(frames[1]) == 1 and len(frames[2]) == 1 and len(frames[3]) == 1
    assert frames[1][0][0] == (0, 0, 10, 10)
    assert parse_mot(gt, ground_truth=True, gt_classes=(8,))[2][0][0] == (0, 0, 5, 5)
    gt.write_text("1,1,0,0,-1,10\n", encoding="utf-8")
    with pytest.raises(EvaluationInputError, match="gt.txt:1"):
        parse_mot(gt, ground_truth=True)
    gt.write_text("1,1,0,0\n", encoding="utf-8")
    with pytest.raises(EvaluationInputError):
        parse_mot(gt, ground_truth=True)


# ------------------------------------------------------------------ storage
def record(c, **kw):
    base = dict(method="import", model_name="yolov8n.pt", model_version="v1", dataset_name="bus01", counts=c,
                iou_threshold=0.5, conf_threshold=0.35, config={"k": 1})
    base.update(kw)
    return EvaluationRecord(**base)


def test_store_and_latest_round_trip(conn):
    assert latest_evaluation(conn) is None
    store_evaluation(conn, record(counts(8, 2, 2, images=4, img=(3, 0, 0, 1))), FIXED_NOW)
    new_id = store_evaluation(conn, record(counts(1, 1, 1), model_version="v2"), FIXED_NOW)
    ev = latest_evaluation(conn)
    assert ev.evaluation_id == new_id and ev.model_version == "v2" and ev.scores.accuracy is None
    older = list_evaluations(conn)[1]
    assert older.scores.precision == pytest.approx(0.8) and older.scores.accuracy == 1.0
    assert older.config == {"k": 1} and older.test_set_size == 4 and older.evaluated_at == FIXED_NOW


def test_missing_table_returns_none(tmp_path, caplog):
    c = sqlite3.connect(str(tmp_path / "bare.db"))
    try:
        assert latest_evaluation(c) is None
    finally:
        c.close()
    assert "model_evaluations table is missing" in caplog.text


@pytest.mark.parametrize("over", [
    {"pred_boxes": 99},
    {"img_tp": 1, "img_fp": 0, "img_fn": 0, "img_tn": 0, "test_set_size": 5},
    {"img_tp": 1},
    {"config_json": "x" * 20001},
])
def test_check_constraints_are_backstops(conn, over):
    row = dict(evaluated_at="2026-01-15T05:30:00.000000Z", method="import", model_name="m", model_version="v",
               dataset_name="d", test_set_size=1, gt_boxes=1, pred_boxes=1, tp=1, fp=0, fn=0, iou_threshold=0.5,
               config_json="{}")
    row.update(over)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"INSERT INTO model_evaluations ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                     tuple(row.values()))


# ------------------------------------------------------------------ import validation
VALID_IMPORT = {"model_name": "yolov8n.pt", "model_version": "sha256:abc", "dataset_name": "bus01",
                "test_set_size": 10, "tp": 8, "fp": 2, "fn": 2, "iou_threshold": 0.5}


def test_record_from_import_valid_and_metrics_recomputed(caplog):
    rec = record_from_import({**VALID_IMPORT, "precision": 0.99, "image_counts": {"tp": 7, "fp": 1, "fn": 1,
                                                                                 "tn": 1}}, "r.json")
    assert rec.counts.gt_boxes == 10 and rec.counts.pred_boxes == 10 and rec.counts.img_tn == 1
    assert rec.config == {"source": "import", "file": "r.json", "iou_threshold": 0.5, "config": {}}
    assert scores(rec.counts).precision == pytest.approx(0.8)
    assert "Ignoring metric values" in caplog.text


@pytest.mark.parametrize("change, field", [
    ({"tp": None}, "tp"), ({"tp": True}, "tp"), ({"positive_class": "car"}, "positive_class"),
    ({"image_counts": {"tp": 1, "fp": 1, "fn": 1, "tn": 1}}, "image_counts"), ({"model_name": ""}, "model_name"),
    ({"iou_threshold": 0}, "iou_threshold"), ({"gt_boxes": 3}, "gt_boxes"), ({"averaging": "macro"}, "averaging"),
])
def test_record_from_import_rejects(change, field):
    data = {**VALID_IMPORT, **change}
    if change.get("tp", 0) is None:
        del data["tp"]
    with pytest.raises(EvaluationInputError, match=field):
        record_from_import(data, "r.json")


# ------------------------------------------------------------------ never from ticket records
def test_evaluation_never_reads_operational_tables():
    pattern = re.compile(r"(?i)\b(FROM|JOIN)\s+(validation_events|tickets|boarding_events|detection_records)\b")
    for name in ("evaluation.py", "evaluate.py", "metric_evaluation.py", "evaluation_dataset.py"):
        source = (ROOT / "app" / name).read_text(encoding="utf-8")
        assert not pattern.search(source), name
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
        assert not imported & {"app.analytics", "app.ticket_validator", "app.alert_service"}, name


def test_na_message_text():
    assert NA_MESSAGE == "Run model evaluation to calculate performance."


# ------------------------------------------------------------------ CLI
def cli(*args):
    return evaluate.main([str(a) for a in args])


def rows(db_path):
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    try:
        return c.execute("SELECT * FROM model_evaluations ORDER BY evaluation_id").fetchall()
    finally:
        c.close()


def test_cli_import_end_to_end(tmp_path, capsys):
    db_path = tmp_path / "e.db"
    src = tmp_path / "r.json"
    src.write_text(json.dumps(VALID_IMPORT), encoding="utf-8")
    assert cli("import", "--json", src, "--db", db_path) == 0
    out = capsys.readouterr().out
    assert "Precision: 80.0%" in out and "Stored evaluation #1" in out
    (row,) = rows(db_path)
    assert row["precision"] == pytest.approx(0.8) and row["method"] == "import"
    src.write_text(json.dumps({**VALID_IMPORT, "tp": -1}), encoding="utf-8")
    assert cli("import", "--json", src, "--db", db_path) == 1
    assert cli("import", "--json", tmp_path / "nope.json", "--db", db_path) == 1
    assert len(rows(db_path)) == 1
    assert cli("list", "--db", db_path) == 0 and "#1 " in capsys.readouterr().out
    assert cli("import", "--json", src.with_name("r.json"), "--dry-run", "--db", db_path) == 1


def test_cli_database_error_exit_3(tmp_path):
    src = tmp_path / "r.json"
    src.write_text(json.dumps(VALID_IMPORT), encoding="utf-8")
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"this is not a sqlite database" * 100)
    assert cli("import", "--json", src, "--db", broken) == 3


def write_mot(tmp_path):
    gt, pred = tmp_path / "gt.txt", tmp_path / "pred.txt"
    gt.write_text("1,1,0,0,10,10,1,1,1\n2,2,50,50,10,10,1,8,1\n", encoding="utf-8")   # frame 2: distractor only
    pred.write_text("1,1,0,0,10,10,0.9,-1,-1,-1\n2,5,50,50,10,10,0.8,-1,-1,-1\n", encoding="utf-8")
    return gt, pred


def test_cli_mot_end_to_end(tmp_path, capsys):
    db_path = tmp_path / "e.db"
    gt, pred = write_mot(tmp_path)
    assert cli("mot", "--gt", gt, "--pred", pred, "--model-version", "v1", "--db", db_path) == 0
    assert "conf unknown" in capsys.readouterr().out
    (row,) = rows(db_path)
    assert (row["tp"], row["fp"], row["fn"]) == (1, 1, 0)        # prediction on dropped class 8 -> FP
    assert row["conf_threshold"] is None and row["img_tp"] is None and row["accuracy"] is None
    cfg = json.loads(row["config_json"])
    assert cfg["ignored_gt"] == evaluate.IGNORED_GT and cfg["stage"] == "tracked (MOT export)"
    assert cli("mot", "--gt", gt, "--pred", pred, "--model-version", "v1", "--num-frames", 3, "--db", db_path) == 0
    last = rows(db_path)[-1]
    assert (last["img_tp"], last["img_fp"], last["img_fn"], last["img_tn"]) == (1, 1, 0, 1)
    assert cli("mot", "--gt", gt, "--pred", pred, "--model", tmp_path / "missing.pt", "--db", db_path) == 1
    assert cli("mot", "--gt", gt, "--pred", pred, "--model-version", "v1", "--num-frames", 1, "--db", db_path) == 1
    assert len(rows(db_path)) == 2


def test_cli_mot_empty_files_store_nothing(tmp_path, capsys):
    db_path = tmp_path / "e.db"
    gt, pred = tmp_path / "gt.txt", tmp_path / "pred.txt"
    gt.write_text("", encoding="utf-8")
    pred.write_text("", encoding="utf-8")
    assert cli("mot", "--gt", gt, "--pred", pred, "--model-version", "v1", "--db", db_path) == 1
    assert "no frames in --gt or --pred" in capsys.readouterr().err
    assert not db_path.exists() or rows(db_path) == []


@pytest.mark.parametrize("argv", [
    ["mot", "--gt", "g", "--pred", "p"],
    ["mot", "--gt", "g", "--pred", "p", "--model", "m", "--model-version", "v"],
    ["images", "--images", "x", "--max-images", "0"], ["images", "--images", "x", "--imgsz", "100"],
    ["images", "--images", "x", "--imgsz", "650"], ["images", "--images", "x", "--person-class", "-1"],
    ["mot", "--gt", "g", "--pred", "p", "--model-version", "v", "--num-frames", "0"],
    ["list", "--limit", "0"], ["list", "--limit", "1001"], ["import", "--json", "x", "--iou", "0"],
    ["images", "--images", "x", "--conf", "0.99"],
])
def test_cli_argument_errors_exit_2(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        cli(*argv)
    assert exc.value.code == 2
    if argv[:1] == ["mot"] and len(argv) == 5:
        err = capsys.readouterr().err
        assert "--model" in err and "--model-version" in err


class FakeModel:
    def predict(self, frame, **kwargs):
        assert kwargs["classes"] == [0]
        h, w = frame.shape[:2]
        boxes = SimpleNamespace(xyxy=np.array([[0, 0, w / 2, h / 2]]), conf=np.array([0.9]), cls=np.array([0.0]),
                                id=None)
        return [SimpleNamespace(boxes=boxes)]


def test_cli_images_with_fake_model(tmp_path, monkeypatch):
    import cv2

    root = tmp_path / "bus01"
    (root / "images").mkdir(parents=True)
    (root / "labels").mkdir()
    for name in ("a", "b", "c"):
        cv2.imwrite(str(root / "images" / f"{name}.png"), np.zeros((40, 40, 3), np.uint8))
    (root / "labels" / "a.txt").write_text("0 0.25 0.25 0.5 0.5\n", encoding="utf-8")   # matches the fake box
    weights = tmp_path / "fake.pt"
    weights.write_bytes(b"weights")
    monkeypatch.setattr(evaluate, "load_model", lambda det: FakeModel())
    db_path = tmp_path / "e.db"
    assert cli("images", "--images", root / "images", "--model", weights, "--max-images", 2, "--db", db_path) == 0
    (row,) = rows(db_path)
    assert row["test_set_size"] == 2 and (row["tp"], row["fp"], row["fn"]) == (1, 1, 0)
    assert (row["img_tp"], row["img_fp"]) == (1, 1) and row["model_name"] == "fake.pt"
    assert row["model_version"].startswith("sha256:")
    cfg = json.loads(row["config_json"])
    assert cfg["stage"] == "detection" and cfg["ignored_gt"] == evaluate.IGNORED_GT
    assert cli("images", "--images", tmp_path / "nowhere", "--db", db_path) == 1


def test_cli_images_requires_label_files(tmp_path, monkeypatch):
    root = tmp_path / "ds"
    (root / "images").mkdir(parents=True)
    (root / "labels").mkdir()
    monkeypatch.setattr(evaluate, "load_model", lambda det: FakeModel())
    assert cli("images", "--images", root / "images", "--db", tmp_path / "e.db") == 1


def test_database_init_creates_table(tmp_path):
    database = Database(tmp_path / "x.db")
    database.initialize()
    c = database.connect()
    try:
        assert latest_evaluation(c) is None
        assert c.execute("SELECT name FROM sqlite_master WHERE name = 'model_evaluations'").fetchone()
    finally:
        c.close()
