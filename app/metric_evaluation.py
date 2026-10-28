"""Manual evaluation metrics: pure formulas, input validation and the ``metric_evaluations`` store.

No streamlit. The four counts (TP, TN, FP, FN) come from a labelled test dataset, either
entered on the dashboard or counted by :func:`confusion_matrix` from per-sample labels
(``app.evaluation_dataset``); they are never derived from dashboard counters. Saved runs are
append-only (one INSERT per explicit Save, never UPDATE/DELETE) and this module is
the only writer of ``metric_evaluations``. Metrics are percentages 0..100, or
None (shown as "N/A") when a denominator is zero.
"""

from __future__ import annotations

import logging
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from app.database import parse_iso, to_iso, transaction

logger = logging.getLogger(__name__)

MAX_COUNT = 10_000_000
MAX_TEXT = 200
MODE_MANUAL = "manual"
MODE_MODEL = "model"
MODE_DATASET = "dataset"
MODE_LABELS = {MODE_DATASET: "Dummy dataset (synthetic)", MODE_MANUAL: "Manual evaluation",
               MODE_MODEL: "Model evaluation results"}
NO_EVALUATION_MESSAGE = "No evaluated dataset available. Enter verified evaluation counts or run model evaluation."
MANUAL_DISCLAIMER = "Counts entered by a user from a labelled test dataset. Not verified model performance."
DATASET_DISCLAIMER = ("Synthetic test data from a fixed-seed generator. These numbers only check the metric "
                      "calculation; they are not real-world model accuracy.")
LABEL_LEGEND = "Labels: 1 = passenger without a valid ticket (positive class), 0 = valid-ticket passenger."
ALL_ZERO_MESSAGE = "All counts are 0, so no metric can be calculated."
COUNT_FIELDS = ("tp", "tn", "fp", "fn")
COUNT_LABELS = {"tp": "True Positive (TP)", "tn": "True Negative (TN)",
                "fp": "False Positive (FP)", "fn": "False Negative (FN)"}
MSG_DATASET = ("Dataset description is required to save (for example: bus camera test set v2, "
               "400 labelled frames).")
MSG_TOTAL = "Total evaluated cases must be at least 1 to save."

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class EvaluationInputError(ValueError):
    """Invalid counts or fields; ``field_errors`` maps field -> user-facing message."""

    def __init__(self, field_errors: Dict[str, str]) -> None:
        super().__init__("; ".join(field_errors.values()))
        self.field_errors = dict(field_errors)


@dataclass(frozen=True)
class EvaluationCounts:
    tp: int
    tn: int
    fp: int
    fn: int

    @property
    def total(self) -> int:
        return self.tp + self.tn + self.fp + self.fn


@dataclass(frozen=True)
class MetricResult:
    """Percentages 0..100 or None (= N/A)."""

    accuracy: Optional[float]
    precision: Optional[float]
    recall: Optional[float]
    f1: Optional[float]


@dataclass(frozen=True)
class StoredManualEvaluation:
    run_id: int
    evaluated_at: datetime
    counts: EvaluationCounts
    result: MetricResult
    model_name: Optional[str]
    model_version: Optional[str]
    dataset_description: str


# ----------------------------------------------------------------- formulas
def _check_count(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative int")
    return value


def calculate_metrics(tp: int, tn: int, fp: int, fn: int) -> MetricResult:
    """Accuracy, Precision, Recall, F1 (percent); each None when its denominator is 0."""
    tp, tn, fp, fn = (_check_count(n, v) for n, v in zip(COUNT_FIELDS, (tp, tn, fp, fn)))
    total = tp + tn + fp + fn
    accuracy = (tp + tn) / total * 100 if total else None
    precision = tp / (tp + fp) * 100 if tp + fp else None
    recall = tp / (tp + fn) * 100 if tp + fn else None
    if precision is None or recall is None or precision + recall == 0:
        f1 = None
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return MetricResult(accuracy, precision, recall, f1)


def _label(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value in (0, 1)


def confusion_matrix(pairs: Iterable[Tuple[object, object]]) -> EvaluationCounts:
    """TP/TN/FP/FN of ``(ground_truth, predicted_label)`` pairs; labels are ints 0 or 1 (1 = positive class).

    TP = (1, 1), TN = (0, 0), FP = (0, 1), FN = (1, 0). Empty input gives all zeros.
    Raises ``ValueError`` for any other label and ``RuntimeError`` if the four counts
    do not add up to the number of samples (TP + TN + FP + FN == total).
    """
    tp = tn = fp = fn = 0
    n = 0
    for i, (truth, predicted) in enumerate(pairs, start=1):
        if not (_label(truth) and _label(predicted)):
            raise ValueError(f"sample {i}: ground_truth/predicted_label must be 0 or 1")
        n += 1
        if truth == 1 and predicted == 1:
            tp += 1
        elif truth == 0 and predicted == 0:
            tn += 1
        elif truth == 0:
            fp += 1
        else:
            fn += 1
    counts = EvaluationCounts(tp, tn, fp, fn)
    if counts.total != n:
        raise RuntimeError("confusion matrix does not cover every sample")
    return counts


def format_percent(value: Optional[float]) -> str:
    """Two-decimal display ("88.89%"); values stay full precision internally. None -> "N/A"."""
    return "N/A" if value is None else f"{value:.2f}%"


# --------------------------------------------------------------- validation
def _count_value(label: str, raw: object) -> Tuple[Optional[int], Optional[str]]:
    if raw is None or (isinstance(raw, str) and raw == ""):
        return None, f"{label} is required. Enter a whole number of 0 or more."
    invalid = f"{label} must be a whole number of 0 or more."
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None, invalid
    if isinstance(raw, float):
        if not math.isfinite(raw) or not raw.is_integer():
            return None, invalid
        raw = int(raw)
    if raw < 0:
        return None, invalid
    if raw > MAX_COUNT:
        return None, f"{label} must be at most 10,000,000."
    return int(raw), None


def validate_counts(values: Mapping[str, object]) -> EvaluationCounts:
    """The four counts as ints, or ``EvaluationInputError`` listing every bad field."""
    parsed: Dict[str, int] = {}
    errors: Dict[str, str] = {}
    for name in COUNT_FIELDS:
        value, error = _count_value(COUNT_LABELS[name], values.get(name))
        if error:
            errors[name] = error
        else:
            parsed[name] = value  # type: ignore[assignment]
    if errors:
        raise EvaluationInputError(errors)
    return EvaluationCounts(**parsed)


def _clean_text(value: object) -> Optional[str]:
    """Stripped text with internal whitespace collapsed; None when empty; ValueError if invalid."""
    if value is None:
        return None
    if not isinstance(value, str) or _CONTROL.search(value):
        raise ValueError
    text = " ".join(value.split())
    if len(text) > MAX_TEXT:
        raise ValueError
    return text or None


def validate_save_fields(dataset: object, model_name: object,
                         model_version: object) -> Tuple[str, Optional[str], Optional[str]]:
    """``(dataset_description, model_name, model_version)``; the dataset is required."""
    errors: Dict[str, str] = {}
    cleaned: List[Optional[str]] = []
    for key, label, value in (("dataset", "Dataset description", dataset), ("model_name", "Model name", model_name),
                              ("model_version", "Model version", model_version)):
        try:
            cleaned.append(_clean_text(value))
        except ValueError:
            errors[key] = f"{label} must be at most 200 characters without control characters."
            cleaned.append(None)
    if "dataset" not in errors and cleaned[0] is None:
        errors["dataset"] = MSG_DATASET
    if errors:
        raise EvaluationInputError(errors)
    return cleaned[0], cleaned[1], cleaned[2]  # type: ignore[return-value]


# ------------------------------------------------------------------ storage
def save_manual_evaluation(conn: sqlite3.Connection, counts: EvaluationCounts, *, dataset_description: str,
                           model_name: Optional[str], model_version: Optional[str], now: datetime) -> int:
    """Append one manual run (metrics recomputed here from the counts); returns its ``run_id``."""
    counts = validate_counts({f: getattr(counts, f) for f in COUNT_FIELDS})
    dataset, name, version = validate_save_fields(dataset_description, model_name, model_version)
    if counts.total < 1:
        raise EvaluationInputError({"total": MSG_TOTAL})
    r = calculate_metrics(counts.tp, counts.tn, counts.fp, counts.fn)
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO metric_evaluations (evaluated_at, mode, tp, tn, fp, fn, accuracy_pct, precision_pct, "
            "recall_pct, f1_pct, model_name, model_version, dataset_description) "
            "VALUES (?, 'manual', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (to_iso(now), counts.tp, counts.tn, counts.fp, counts.fn, r.accuracy, r.precision, r.recall, r.f1,
             name, version, dataset))
    run_id = int(cur.lastrowid)
    logger.info("Manual evaluation run %d saved", run_id)
    return run_id


def list_manual_evaluations(conn: sqlite3.Connection, limit: int = 10) -> List[StoredManualEvaluation]:
    """Saved manual runs, newest first ([] with a WARNING if the table does not exist)."""
    if conn.row_factory is not sqlite3.Row:
        conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM metric_evaluations ORDER BY evaluated_at DESC, run_id DESC LIMIT ?",
            (int(limit),)).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        logger.warning("metric_evaluations table is missing; no saved manual runs (%s)", exc)
        return []
    return [StoredManualEvaluation(
        run_id=int(r["run_id"]), evaluated_at=parse_iso(r["evaluated_at"]),
        counts=EvaluationCounts(int(r["tp"]), int(r["tn"]), int(r["fp"]), int(r["fn"])),
        result=MetricResult(r["accuracy_pct"], r["precision_pct"], r["recall_pct"], r["f1_pct"]),
        model_name=r["model_name"], model_version=r["model_version"], dataset_description=r["dataset_description"])
        for r in rows]


def find_recent_duplicate(conn: sqlite3.Connection, counts: EvaluationCounts, dataset_description: str,
                          model_name: Optional[str], model_version: Optional[str], *,
                          since: datetime) -> Optional[int]:
    """``run_id`` of an identical run saved at or after ``since`` (interrupted-save recovery)."""
    row = conn.execute(
        "SELECT run_id FROM metric_evaluations WHERE tp = ? AND tn = ? AND fp = ? AND fn = ? "
        "AND dataset_description = ? AND model_name IS ? AND model_version IS ? "
        "AND evaluated_at >= ? ORDER BY run_id DESC LIMIT 1",
        (counts.tp, counts.tn, counts.fp, counts.fn, dataset_description, model_name, model_version,
         to_iso(since))).fetchone()
    return None if row is None else int(row[0])
