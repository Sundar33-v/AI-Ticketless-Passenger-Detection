"""Tests for app/metric_evaluation.py (pure formulas, validation and the metric_evaluations store)."""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import app.metric_evaluation as me
from app.database import to_iso

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 1, 15, 5, 30, tzinfo=timezone.utc)
OPERATIONAL = ("passengers", "tickets", "detection_sessions", "detection_records", "boarding_events",
               "validation_events", "model_evaluations")


@pytest.fixture
def conn(db):
    c = db.connect()
    try:
        yield c
    finally:
        c.close()


def counts(tp=50, tn=40, fp=5, fn=5):
    return me.EvaluationCounts(tp, tn, fp, fn)


def save(conn, c=None, dataset="bus test set v1", name=None, version=None, now=NOW):
    return me.save_manual_evaluation(conn, c or counts(), dataset_description=dataset, model_name=name,
                                     model_version=version, now=now)


def row_count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ----------------------------------------------------------------- formulas
def test_reference_values():
    r = me.calculate_metrics(50, 40, 5, 5)
    assert r.accuracy == pytest.approx(90.0)
    assert r.precision == pytest.approx(90.909090909)
    assert r.recall == pytest.approx(90.909090909)
    assert r.f1 == pytest.approx(90.909090909)


@pytest.mark.parametrize("args, expected", [
    ((0, 0, 0, 0), (None, None, None, None)),
    ((0, 7, 0, 3), (70.0, None, 0.0, None)),         # TP = FP = 0 -> precision N/A, F1 N/A
    ((0, 7, 3, 0), (70.0, 0.0, None, None)),         # TP = FN = 0 -> recall N/A
    ((0, 0, 3, 4), (0.0, 0.0, 0.0, None)),           # P = R = 0 -> F1 is 0/0 -> N/A
    ((0, 9, 0, 0), (100.0, None, None, None)),       # only TN
])
def test_division_by_zero_is_none(args, expected):
    r = me.calculate_metrics(*args)
    for got, want in zip((r.accuracy, r.precision, r.recall, r.f1), expected):
        assert (got is None) if want is None else got == pytest.approx(want)


@pytest.mark.parametrize("bad", [(-1, 0, 0, 0), (1.5, 0, 0, 0), (True, 0, 0, 0), ("3", 0, 0, 0)])
def test_calculate_rejects_non_ints(bad):
    with pytest.raises(ValueError):
        me.calculate_metrics(*bad)


def test_format_percent():
    assert me.format_percent(None) == "N/A"
    assert me.format_percent(90.90909) == "90.91%"
    assert me.format_percent(0.0) == "0.00%"
    assert me.format_percent(100) == "100.00%"


# --------------------------------------------------------- confusion matrix
def pairs(tp=0, tn=0, fp=0, fn=0):
    return [(1, 1)] * tp + [(0, 0)] * tn + [(0, 1)] * fp + [(1, 0)] * fn


def metrics_of(pair_list):
    c = me.confusion_matrix(pair_list)
    assert c.tp + c.tn + c.fp + c.fn == c.total == len(pair_list)
    return c, me.calculate_metrics(c.tp, c.tn, c.fp, c.fn)


def shown(r):
    return [me.format_percent(v) for v in (r.accuracy, r.precision, r.recall, r.f1)]


def test_known_case_tp40_tn50_fp5_fn5():
    c, r = metrics_of(pairs(40, 50, 5, 5))
    assert c == me.EvaluationCounts(40, 50, 5, 5) and c.total == 100
    assert shown(r) == ["90.00%", "88.89%", "88.89%", "88.89%"]
    assert r.accuracy == pytest.approx(90.0)
    for value in (r.precision, r.recall, r.f1):
        assert value == pytest.approx(800 / 9)  # full precision internally, rounded only for display


def test_all_correct():
    c, r = metrics_of(pairs(tp=30, tn=70))
    assert (c.fp, c.fn) == (0, 0)
    assert shown(r) == ["100.00%"] * 4


def test_false_positives_present():
    c, r = metrics_of(pairs(tp=10, tn=80, fp=10))
    assert c == me.EvaluationCounts(10, 80, 10, 0)
    assert shown(r) == ["90.00%", "50.00%", "100.00%", "66.67%"]


def test_false_negatives_present():
    c, r = metrics_of(pairs(tp=10, tn=80, fn=10))
    assert c == me.EvaluationCounts(10, 80, 0, 10)
    assert shown(r) == ["90.00%", "100.00%", "50.00%", "66.67%"]


def test_zero_total():
    c, r = metrics_of([])
    assert c == me.EvaluationCounts(0, 0, 0, 0)
    assert shown(r) == ["N/A"] * 4


def test_zero_precision_denominator():
    c, r = metrics_of(pairs(tn=8, fn=2))  # TP + FP = 0: nothing predicted positive
    assert r.precision is None and r.f1 is None
    assert shown(r) == ["80.00%", "N/A", "0.00%", "N/A"]


def test_zero_recall_denominator():
    c, r = metrics_of(pairs(tn=8, fp=2))  # TP + FN = 0: no positive ground truth
    assert r.recall is None and r.f1 is None
    assert shown(r) == ["80.00%", "0.00%", "N/A", "N/A"]


@pytest.mark.parametrize("bad", [2, -1, True, None, "1", 1.0])
@pytest.mark.parametrize("position", [0, 1])
def test_confusion_matrix_rejects_invalid_labels(bad, position):
    pair = [1, 0]
    pair[position] = bad
    with pytest.raises(ValueError, match=r"^sample 2: ground_truth/predicted_label must be 0 or 1$"):
        me.confusion_matrix([(1, 1), tuple(pair)])


def test_confusion_matrix_total_invariant(monkeypatch):
    class Broken(me.EvaluationCounts):
        @property
        def total(self):
            return 0

    monkeypatch.setattr(me, "EvaluationCounts", Broken)
    with pytest.raises(RuntimeError, match="does not cover every sample"):
        me.confusion_matrix(pairs(tp=1))


# --------------------------------------------------------------- validation
@pytest.mark.parametrize("value, message", [
    (None, "True Positive (TP) is required. Enter a whole number of 0 or more."),
    ("", "True Positive (TP) is required. Enter a whole number of 0 or more."),
    (-1, "True Positive (TP) must be a whole number of 0 or more."),
    (1.5, "True Positive (TP) must be a whole number of 0 or more."),
    (True, "True Positive (TP) must be a whole number of 0 or more."),
    ("3", "True Positive (TP) must be a whole number of 0 or more."),
    (float("nan"), "True Positive (TP) must be a whole number of 0 or more."),
    (10_000_001, "True Positive (TP) must be at most 10,000,000."),
])
def test_validate_counts_messages(value, message):
    with pytest.raises(me.EvaluationInputError) as info:
        me.validate_counts({"tp": value, "tn": 1, "fp": 1, "fn": 1})
    assert info.value.field_errors == {"tp": message}


def test_validate_counts_collects_all_and_accepts_integral_floats():
    with pytest.raises(me.EvaluationInputError) as info:
        me.validate_counts({})
    assert set(info.value.field_errors) == set(me.COUNT_FIELDS)
    assert me.validate_counts({"tp": 3.0, "tn": 0, "fp": 10_000_000, "fn": 2}) == me.EvaluationCounts(3, 0, 10**7, 2)


def test_validate_save_fields():
    assert me.validate_save_fields("  bus   set ", "", None) == ("bus set", None, None)
    assert me.validate_save_fields("d", " yolov8n.pt ", "v2") == ("d", "yolov8n.pt", "v2")
    with pytest.raises(me.EvaluationInputError) as info:
        me.validate_save_fields("   ", "x" * 201, "bad\x00")
    assert info.value.field_errors["dataset"] == me.MSG_DATASET
    assert set(info.value.field_errors) == {"dataset", "model_name", "model_version"}


def test_messages_text():
    assert me.NO_EVALUATION_MESSAGE == ("No evaluated dataset available. Enter verified evaluation counts or run "
                                        "model evaluation.")
    assert me.MODE_LABELS == {"dataset": "Dummy dataset (synthetic)", "manual": "Manual evaluation",
                              "model": "Model evaluation results"}
    assert "ticketless" not in (me.DATASET_DISCLAIMER + me.LABEL_LEGEND).lower()
    assert "not real-world model accuracy" in me.DATASET_DISCLAIMER


# ------------------------------------------------------------------ storage
def test_save_recomputes_and_lists_without_user(conn):
    before = {t: row_count(conn, t) for t in OPERATIONAL}
    run_id = save(conn, name="yolov8n.pt", version="sha256:abc")
    row = conn.execute("SELECT * FROM metric_evaluations WHERE run_id = ?", (run_id,)).fetchone()
    assert (row["tp"], row["tn"], row["fp"], row["fn"], row["mode"]) == (50, 40, 5, 5, "manual")
    assert row["accuracy_pct"] == pytest.approx(90.0) and row["f1_pct"] == pytest.approx(90.909090909)
    assert row["evaluated_at"] == to_iso(NOW) and "created_by_user_id" not in row.keys()
    assert {t: row_count(conn, t) for t in OPERATIONAL} == before
    stored = me.list_manual_evaluations(conn)
    assert len(stored) == 1 and stored[0].counts == counts() and stored[0].model_version == "sha256:abc"


def test_save_works_on_db_with_legacy_user_column(tmp_path):
    """Databases from the removed sign-in feature keep users + created_by_user_id; saving leaves it NULL."""
    c = sqlite3.connect(str(tmp_path / "legacy.db"), isolation_level=None)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY)")
        c.execute("INSERT INTO users (user_id) VALUES (1)")
        c.execute(
            "CREATE TABLE metric_evaluations (run_id INTEGER PRIMARY KEY AUTOINCREMENT, evaluated_at TEXT NOT NULL, "
            "mode TEXT NOT NULL DEFAULT 'manual', tp INTEGER NOT NULL, tn INTEGER NOT NULL, fp INTEGER NOT NULL, "
            "fn INTEGER NOT NULL, accuracy_pct REAL, precision_pct REAL, recall_pct REAL, f1_pct REAL, "
            "model_name TEXT, model_version TEXT, dataset_description TEXT NOT NULL, "
            "created_by_user_id INTEGER REFERENCES users(user_id) ON DELETE SET NULL)")
        run_id = save(c)
        row = c.execute("SELECT created_by_user_id FROM metric_evaluations WHERE run_id = ?", (run_id,)).fetchone()
        assert row["created_by_user_id"] is None
        assert [r.run_id for r in me.list_manual_evaluations(c)] == [run_id]
        assert c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1  # leftover table untouched
    finally:
        c.close()


def test_save_rejects_missing_dataset_and_total_zero(conn):
    with pytest.raises(me.EvaluationInputError) as info:
        save(conn, dataset="  ")
    assert "dataset" in info.value.field_errors
    with pytest.raises(me.EvaluationInputError) as info:
        save(conn, c=counts(0, 0, 0, 0))
    assert info.value.field_errors == {"total": "Total evaluated cases must be at least 1 to save."}
    assert row_count(conn, "metric_evaluations") == 0


def test_save_is_append_only_and_listed_newest_first(conn):
    first = save(conn)
    second = save(conn, c=counts(1, 1, 1, 1), now=NOW + timedelta(minutes=1))
    assert second != first and row_count(conn, "metric_evaluations") == 2
    assert [r.run_id for r in me.list_manual_evaluations(conn)] == [second, first]
    assert [r.run_id for r in me.list_manual_evaluations(conn, 1)] == [second]


def test_list_missing_table_returns_empty(tmp_path, caplog):
    c = sqlite3.connect(str(tmp_path / "bare.db"))
    try:
        with caplog.at_level(logging.WARNING):
            assert me.list_manual_evaluations(c) == []
    finally:
        c.close()
    assert "metric_evaluations table is missing" in caplog.text


def test_find_recent_duplicate(conn):
    run_id = save(conn, version=None)
    since = NOW - timedelta(seconds=5)
    find = me.find_recent_duplicate
    assert find(conn, counts(), "bus test set v1", None, None, since=since) == run_id
    assert find(conn, counts(1, 40, 5, 5), "bus test set v1", None, None, since=since) is None
    assert find(conn, counts(), "other set", None, None, since=since) is None
    assert find(conn, counts(), "bus test set v1", None, "v2", since=since) is None
    assert find(conn, counts(), "bus test set v1", None, None, since=NOW + timedelta(seconds=1)) is None

def test_module_never_mentions_operational_tables():
    source = (ROOT / "app" / "metric_evaluation.py").read_text(encoding="utf-8")
    for name in ("validation_events", "tickets", "boarding_events", "detection_records", "detection_sessions"):
        assert not re.search(rf"\b{name}\b", source), name
    assert "import streamlit" not in source
