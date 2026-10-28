"""Person-detection model evaluation against labelled ground truth (pure functions + storage).

Methodology (also in docs/limitations.md "Evaluation method"):

* Positive class: ``person`` (COCO class 0). Ground-truth boxes of other
  classes are dropped before matching, so a prediction on one is a FP.
* Box matching per image/frame: predictions sorted by confidence (descending,
  stable); each takes the unmatched GT box with the highest IoU; IoU >=
  ``iou_threshold`` (default 0.5, inclusive) is a TP and consumes the GT box,
  otherwise a FP. Unmatched GT boxes are FN.
* Precision = TP/(TP+FP), Recall = TP/(TP+FN), F1 = 2PR/(P+R); a zero
  denominator gives ``None`` (shown as N/A). F1 is None when P or R is None.
* Accuracy: TN is undefined for boxes, so Accuracy is image-level person
  presence accuracy (TP+TN)/(TP+TN+FP+FN) over images; None when unknown.
* Averaging: single class, counts pooled over all images (micro).

Only labelled data produces these numbers. This module never reads the
operational (ticket / boarding / detection) tables and never treats live,
unlabelled predictions as ground truth. ``store_evaluation`` is the only writer
of ``model_evaluations`` and computes the metrics from the counts itself.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.database import parse_iso, to_iso

logger = logging.getLogger(__name__)

Box = Tuple[float, float, float, float]  # x1, y1, x2, y2 pixels
POSITIVE_CLASS = "person"
DEFAULT_IOU = 0.5
AVERAGING = "micro"
NA_MESSAGE = "Run model evaluation to calculate performance."
METHODS = ("images", "mot", "import")
MAX_TEXT = 200
MAX_NOTES = 500
MAX_CONFIG_CHARS = 20000


class EvaluationInputError(ValueError):
    """Invalid dataset, label file or import JSON (message names the file:line or field)."""


# ------------------------------------------------------------------ matching
def iou(a: Box, b: Box) -> float:
    """Intersection over union on continuous pixel coordinates (no +1)."""
    iw = min(a[2], b[2]) - max(a[0], b[0])
    ih = min(a[3], b[3]) - max(a[1], b[1])
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def match_frame(gt: Sequence[Box], pred: Sequence[Tuple[Box, float]], iou_threshold: float) -> Tuple[int, int, int]:
    """Greedy one-to-one matching by confidence; returns ``(tp, fp, fn)``."""
    order = sorted(range(len(pred)), key=lambda i: -float(pred[i][1]))  # stable: ties keep input order
    unmatched = list(range(len(gt)))
    tp = fp = 0
    for i in order:
        box = pred[i][0]
        best, best_iou = None, -1.0
        for g in unmatched:
            value = iou(box, gt[g])
            if value > best_iou:
                best, best_iou = g, value
        if best is not None and best_iou >= iou_threshold:
            tp += 1
            unmatched.remove(best)
        else:
            fp += 1
    return tp, fp, len(unmatched)


@dataclass(frozen=True)
class Counts:
    """Pooled counts over the evaluated images/frames (``img_*`` None = presence TN unknown)."""

    images: int = 0
    gt_boxes: int = 0
    pred_boxes: int = 0
    tp: int = 0
    fp: int = 0
    fn: int = 0
    img_tp: Optional[int] = 0
    img_fp: Optional[int] = 0
    img_fn: Optional[int] = 0
    img_tn: Optional[int] = 0

    def add_frame(self, gt: Sequence[Box], pred: Sequence[Tuple[Box, float]], iou_threshold: float, *,
                  presence_known: bool = True) -> "Counts":
        tp, fp, fn = match_frame(gt, pred, iou_threshold)
        changes: Dict[str, Any] = dict(images=self.images + 1, gt_boxes=self.gt_boxes + len(gt),
                                       pred_boxes=self.pred_boxes + len(pred), tp=self.tp + tp, fp=self.fp + fp,
                                       fn=self.fn + fn)
        if not presence_known or self.img_tp is None:
            changes.update(img_tp=None, img_fp=None, img_fn=None, img_tn=None)
        else:
            has_gt, has_pred = bool(gt), bool(pred)
            changes.update(img_tp=self.img_tp + (has_gt and has_pred), img_fp=self.img_fp + (has_pred and not has_gt),
                           img_fn=self.img_fn + (has_gt and not has_pred),
                           img_tn=self.img_tn + (not has_gt and not has_pred))
        return replace(self, **changes)


@dataclass(frozen=True)
class Scores:
    accuracy: Optional[float]
    precision: Optional[float]
    recall: Optional[float]
    f1: Optional[float]


def scores(c: Counts) -> Scores:
    """Standard formulas; every zero denominator (or unknown TN) gives None."""
    precision = c.tp / (c.tp + c.fp) if (c.tp + c.fp) else None
    recall = c.tp / (c.tp + c.fn) if (c.tp + c.fn) else None
    if precision is None or recall is None:
        f1 = None
    else:
        f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    accuracy = None
    if None not in (c.img_tp, c.img_fp, c.img_fn, c.img_tn):
        total = c.img_tp + c.img_fp + c.img_fn + c.img_tn
        accuracy = (c.img_tp + c.img_tn) / total if total else None
    return Scores(accuracy, precision, recall, f1)


# ------------------------------------------------------------------- parsers
def parse_yolo_labels(path: Path, width: int, height: int, person_class: int) -> List[Box]:
    """Person boxes (pixels) from a YOLO label file; a missing file means "no objects"."""
    path = Path(path)
    if not path.is_file():
        return []
    boxes: List[Box] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 5:
            raise EvaluationInputError(f"{path}:{lineno}: expected 5 fields 'class cx cy w h', got {len(parts)}")
        try:
            cls = int(parts[0])
            cx, cy, w, h = (float(p) for p in parts[1:])
        except ValueError:
            raise EvaluationInputError(f"{path}:{lineno}: non-numeric field") from None
        if cls < 0:
            raise EvaluationInputError(f"{path}:{lineno}: class must be >= 0")
        if not (0 <= cx <= 1 and 0 <= cy <= 1 and 0 < w <= 1 and 0 < h <= 1):
            raise EvaluationInputError(f"{path}:{lineno}: cx, cy must be in [0, 1] and w, h in (0, 1]")
        if cls != person_class:
            continue
        boxes.append(((cx - w / 2) * width, (cy - h / 2) * height, (cx + w / 2) * width, (cy + h / 2) * height))
    return boxes


def parse_mot(path: Path, *, ground_truth: bool, gt_classes: Sequence[int] = (1, 2, 7)
              ) -> Dict[int, List[Tuple[Box, float]]]:
    """MOT CSV ``frame,id,x,y,w,h[,conf[,class[,vis]]]`` -> {frame: [(box, conf)]}.

    Ground truth with >= 8 columns keeps rows with conf != 0 (the "consider"
    flag) and class in ``gt_classes``; dropped rows are removed before matching
    (strict rule: a prediction on them counts as FP). Frames that only contain
    dropped rows are still listed (with no boxes).
    """
    path = Path(path)
    frames: Dict[int, List[Tuple[Box, float]]] = {}
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            raise EvaluationInputError(f"{path}:{lineno}: expected at least 6 comma-separated fields")
        try:
            values = [float(p) for p in parts]
        except ValueError:
            raise EvaluationInputError(f"{path}:{lineno}: non-numeric field") from None
        if not all(math.isfinite(v) for v in values):
            raise EvaluationInputError(f"{path}:{lineno}: non-finite value")
        frame, x, y, w, h = values[0], values[2], values[3], values[4], values[5]
        if frame < 1 or not float(frame).is_integer():
            raise EvaluationInputError(f"{path}:{lineno}: frame must be an integer >= 1")
        if w <= 0 or h <= 0:
            raise EvaluationInputError(f"{path}:{lineno}: width and height must be > 0")
        rows = frames.setdefault(int(frame), [])
        conf = values[6] if len(values) >= 7 else 1.0
        if ground_truth:
            if len(values) >= 8 and (values[6] == 0 or int(values[7]) not in set(gt_classes)):
                continue
            conf = 1.0
        rows.append(((x, y, x + w, y + h), conf))
    return frames


# ------------------------------------------------------------------- storage
@dataclass(frozen=True)
class EvaluationRecord:
    method: str
    model_name: str
    model_version: str
    dataset_name: str
    counts: Counts
    iou_threshold: float = DEFAULT_IOU
    conf_threshold: Optional[float] = None
    config: Mapping[str, Any] = field(default_factory=dict)
    notes: Optional[str] = None


@dataclass(frozen=True)
class StoredEvaluation:
    evaluation_id: int
    evaluated_at: datetime
    method: str
    model_name: str
    model_version: str
    dataset_name: str
    counts: Counts
    scores: Scores
    iou_threshold: float
    conf_threshold: Optional[float]
    positive_class: str
    averaging: str
    config: Dict[str, Any]
    notes: Optional[str]

    @property
    def test_set_size(self) -> int:
        return self.counts.images


_COLUMNS = ("evaluated_at", "method", "model_name", "model_version", "dataset_name", "test_set_size", "gt_boxes",
            "pred_boxes", "tp", "fp", "fn", "img_tp", "img_fp", "img_fn", "img_tn", "accuracy", "precision",
            "recall", "f1", "iou_threshold", "conf_threshold", "positive_class", "averaging", "config_json", "notes")


def store_evaluation(conn: sqlite3.Connection, record: EvaluationRecord, now: datetime) -> int:
    """Insert one evaluation (metrics computed here from the counts); returns its id."""
    if record.method not in METHODS:
        raise EvaluationInputError(f"method: expected one of {', '.join(METHODS)}")
    c = record.counts
    s = scores(c)
    values = (to_iso(now), record.method, record.model_name, record.model_version, record.dataset_name, c.images,
              c.gt_boxes, c.pred_boxes, c.tp, c.fp, c.fn, c.img_tp, c.img_fp, c.img_fn, c.img_tn, s.accuracy,
              s.precision, s.recall, s.f1, float(record.iou_threshold), record.conf_threshold, POSITIVE_CLASS,
              AVERAGING, json.dumps(dict(record.config), sort_keys=True, default=str), record.notes)
    sql = (f"INSERT INTO model_evaluations ({', '.join(_COLUMNS)}) "
           f"VALUES ({', '.join('?' for _ in _COLUMNS)})")
    return int(conn.execute(sql, values).lastrowid)


def _row_to_stored(row: sqlite3.Row) -> StoredEvaluation:
    counts = Counts(row["test_set_size"], row["gt_boxes"], row["pred_boxes"], row["tp"], row["fp"], row["fn"],
                    row["img_tp"], row["img_fp"], row["img_fn"], row["img_tn"])
    try:
        config = json.loads(row["config_json"])
    except ValueError:
        logger.warning("Evaluation #%s has unreadable config_json", row["evaluation_id"])
        config = {}
    return StoredEvaluation(
        evaluation_id=int(row["evaluation_id"]), evaluated_at=parse_iso(row["evaluated_at"]), method=row["method"],
        model_name=row["model_name"], model_version=row["model_version"], dataset_name=row["dataset_name"],
        counts=counts, scores=Scores(row["accuracy"], row["precision"], row["recall"], row["f1"]),
        iou_threshold=float(row["iou_threshold"]), conf_threshold=row["conf_threshold"],
        positive_class=row["positive_class"], averaging=row["averaging"], config=config, notes=row["notes"])


def _missing_table(exc: sqlite3.OperationalError) -> bool:
    return "no such table: model_evaluations" in str(exc)


def list_evaluations(conn: sqlite3.Connection, limit: int = 10) -> List[StoredEvaluation]:
    """Stored evaluations, newest first ([] with a WARNING if the table does not exist)."""
    if conn.row_factory is not sqlite3.Row:
        conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM model_evaluations ORDER BY evaluated_at DESC, evaluation_id DESC "
                            "LIMIT ?", (int(limit),)).fetchall()
    except sqlite3.OperationalError as exc:
        if not _missing_table(exc):
            raise
        logger.warning("model_evaluations table is missing; no evaluation results available (%s)", exc)
        return []
    return [_row_to_stored(r) for r in rows]


def latest_evaluation(conn: sqlite3.Connection) -> Optional[StoredEvaluation]:
    rows = list_evaluations(conn, 1)
    return rows[0] if rows else None


# ------------------------------------------------------------------- import
def _text_field(data: Mapping, key: str, limit: int = MAX_TEXT, *, required: bool = True) -> Optional[str]:
    value = data.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise EvaluationInputError(f"{key}: expected a non-empty string of at most {limit} characters")
    return value.strip()


def _int_field(data: Mapping, key: str, minimum: int, *, prefix: str = "") -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise EvaluationInputError(f"{prefix}{key}: expected an integer >= {minimum}, got {value!r}")
    return value


def _number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def record_from_import(data: Mapping, file_name: str) -> EvaluationRecord:
    """Validate an imported result (counts only; metric values in the file are recomputed)."""
    if not isinstance(data, Mapping):
        raise EvaluationInputError("the JSON file must contain an object")
    model_name = _text_field(data, "model_name")
    model_version = _text_field(data, "model_version")
    dataset_name = _text_field(data, "dataset_name")
    size = _int_field(data, "test_set_size", 1)
    tp, fp, fn = (_int_field(data, k, 0) for k in ("tp", "fp", "fn"))
    thr = data.get("iou_threshold")
    if not _number(thr) or not 0 < thr <= 1:
        raise EvaluationInputError(f"iou_threshold: expected a number with 0 < x <= 1, got {thr!r}")
    for key, derived in (("gt_boxes", tp + fn), ("pred_boxes", tp + fp)):
        if key in data and data[key] != derived:
            raise EvaluationInputError(f"{key}: {data[key]!r} does not match the counts ({derived})")
    img = data.get("image_counts")
    img_values: Tuple[Optional[int], ...] = (None, None, None, None)
    if img is not None:
        if not isinstance(img, Mapping):
            raise EvaluationInputError("image_counts: expected an object with tp, fp, fn, tn")
        img_values = tuple(_int_field(img, k, 0, prefix="image_counts.") for k in ("tp", "fp", "fn", "tn"))
        if sum(img_values) != size:
            raise EvaluationInputError(f"image_counts: tp+fp+fn+tn = {sum(img_values)} must equal "
                                       f"test_set_size ({size})")
    conf = data.get("conf_threshold")
    if conf is not None and (not _number(conf) or not 0 < conf < 1):
        raise EvaluationInputError(f"conf_threshold: expected null or a number with 0 < x < 1, got {conf!r}")
    if data.get("positive_class", POSITIVE_CLASS) != POSITIVE_CLASS:
        raise EvaluationInputError("positive_class: only 'person' is supported")
    if data.get("averaging", AVERAGING) != AVERAGING:
        raise EvaluationInputError("averaging: only 'micro' is supported (single class)")
    given = data.get("config", {})
    if not isinstance(given, Mapping):
        raise EvaluationInputError("config: expected an object")
    config = {"source": "import", "file": file_name, "iou_threshold": float(thr), "config": dict(given)}
    if len(json.dumps(config, sort_keys=True, default=str)) > MAX_CONFIG_CHARS:
        raise EvaluationInputError(f"config: serialized size exceeds {MAX_CONFIG_CHARS} characters")
    notes = _text_field(data, "notes", MAX_NOTES, required=False)
    ignored = [k for k in ("accuracy", "precision", "recall", "f1", "f1_score") if k in data]
    if ignored:
        logger.warning("Ignoring metric values in %s (%s); they are recomputed from the counts",
                       file_name, ", ".join(ignored))
    counts = Counts(size, tp + fn, tp + fp, tp, fp, fn, *img_values)
    return EvaluationRecord("import", model_name, model_version, dataset_name, counts, float(thr),
                            None if conf is None else float(conf), config, notes)


def imported_evaluated_at(data: Mapping) -> Optional[datetime]:
    """``evaluated_at`` from an import (ISO 8601 with offset), converted to UTC; None if absent."""
    value = data.get("evaluated_at")
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise EvaluationInputError(f"evaluated_at: not an ISO 8601 timestamp: {value!r}") from None
    if parsed.tzinfo is None:
        raise EvaluationInputError("evaluated_at: a UTC offset is required (e.g. 2026-10-21T10:00:00+05:30)")
    return parsed.astimezone(timezone.utc)


def model_identity(path: Path) -> Tuple[str, str]:
    """``(file name, "sha256:<12 hex>")`` of a weights file."""
    path = Path(path)
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return path.name, f"sha256:{digest.hexdigest()[:12]}"
