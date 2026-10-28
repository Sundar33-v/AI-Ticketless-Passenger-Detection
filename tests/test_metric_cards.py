"""Pure HTML builders of the AI Model Performance & Metrics Calculator (dashboard/components/metric_cards.py)."""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.evaluation import Counts, Scores, StoredEvaluation
from app.metric_evaluation import (NO_EVALUATION_MESSAGE, EvaluationCounts, StoredManualEvaluation,
                                   calculate_metrics)
from dashboard.components import metric_cards

IST = ZoneInfo("Asia/Kolkata")
AT = datetime(2026, 1, 15, 4, 37, tzinfo=timezone.utc)
EVIL = '<script>alert("x")</script>'


def values(text):
    return re.findall(r'data-metric="(\w+)" data-value="([^"]+)"', text)


def manual(run_id=1, dataset="bus test set", name="yolov8n.pt", version=None):
    return StoredManualEvaluation(run_id, AT, EvaluationCounts(50, 40, 5, 5), calculate_metrics(50, 40, 5, 5),
                                  name, version, dataset)


def model(evaluation_id=3):
    counts = Counts(10, 10, 10, 8, 2, 2, None, None, None, None)
    return StoredEvaluation(evaluation_id, AT, "import", "yolov8n.pt", "sha256:abc", "labelled frames", counts,
                            Scores(None, 0.8, 0.8, 0.8), 0.5, None, "person", "micro", {}, None)


def test_cards_in_order_with_labels_and_values():
    text = metric_cards.metric_cards_html(calculate_metrics(50, 40, 5, 5))
    assert values(text) == [("accuracy", "90.00%"), ("precision", "90.91%"), ("recall", "90.91%"),
                            ("f1", "90.91%")]
    labels = re.findall(r'class="tpd-metric-label">([^<]+)<', text)
    assert labels == ["Accuracy Score (%)", "Precision (%)", "Recall (%)", "F1-Score (%)"]
    assert "(not available)" not in text


def test_na_cards_have_screen_reader_text():
    text = metric_cards.metric_cards_html(None)
    assert [v for _, v in values(text)] == ["N/A"] * 4
    assert text.count("(not available)") == 4


def test_title_text():
    body = metric_cards.title_html()
    assert "AI Model Performance &amp; Metrics Calculator" in body
    assert html.unescape(re.sub(r"<[^>]+>", "", body)).strip() == "AI Model Performance & Metrics Calculator"


def test_meta_and_status_note_escape():
    meta = metric_cards.meta_html("Manual evaluation · Calculated · not saved", "100", "15 Jan 2026 10:07 IST",
                                  EVIL, EVIL)
    assert "Manual evaluation · Calculated · not saved" in meta and "<script>" not in meta
    for label in ("Evaluation status", "Total evaluated cases", "Evaluation date/time", "Model version", "Dataset"):
        assert label in meta
    note = metric_cards.status_note_html(NO_EVALUATION_MESSAGE)
    assert 'role="status"' in note and NO_EVALUATION_MESSAGE in note


def test_runs_table_rows_and_escaping():
    text = metric_cards.runs_table_html([manual(dataset=EVIL, name=EVIL), model()], IST)
    assert "<script>" not in text and "&lt;script&gt;" in text
    assert "#1" in text and "Manual evaluation" in text and "TP 50 · TN 40 · FP 5 · FN 5" in text
    assert "<td>100</td>" in text and f"<td>{metric_cards.MANUAL_SOURCE}</td>" in text
    assert "model #3" in text and "Model evaluation results" in text and "TP 8 · FP 2 · FN 2 (boxes)" in text
    assert "10 images" in text and "app.evaluate (CLI)" in text and "80.00%" in text
    assert "15 Jan 2026 10:07 IST" in text
    for column in metric_cards.RUN_COLUMNS:
        assert f">{html.escape(column)}</th>" in text


def test_runs_table_empty_and_no_user():
    assert metric_cards.NO_RUNS in metric_cards.runs_table_html([], IST)
    assert "Unknown user" not in metric_cards.runs_table_html([manual()], IST)
    assert "Not provided" in metric_cards.runs_table_html([manual(name=None)], IST)


def meta_values(text):
    found = re.findall(r'data-meta="(\w+)"><dt>[^<]*</dt><dd>(.*?)</dd></div>', text)
    return {k: html.unescape(re.sub(r"<[^>]+>", "", re.sub(r'<span class="tpd-sr">[^<]*</span>', "", v)))
            for k, v in found}


def dataset_view(**changes):
    base = dict(status="Dummy dataset (synthetic) · Calculated",
                dataset="Dummy evaluation dataset (synthetic) · dummy_evaluation_dataset.csv",
                file="data/dummy_evaluation_dataset.csv", counts=EvaluationCounts(40, 50, 5, 5), evaluated_at=AT,
                method="Confusion matrix", model="None – simulated",
                generator="Matches generator output (seed 20260115, 100 samples)")
    base.update(changes)
    return metric_cards.DatasetMetaView(**base)


def test_dataset_meta_success_view():
    text = metric_cards.dataset_meta_html(dataset_view(), IST)
    assert text.startswith('<dl class="tpd-eval-meta tpd-eval-meta--dataset">')
    got = meta_values(text)
    assert list(got) == ["status", "dataset", "synthetic", "total", "tp", "tn", "fp", "fn", "check", "when", "method",
                         "model", "generator", "file"]
    assert (got["tp"], got["tn"], got["fp"], got["fn"], got["total"]) == ("40", "50", "5", "5", "100")
    assert got["check"] == "TP + TN + FP + FN = 100 ✓"
    assert got["when"] == "15 Jan 2026 10:07 IST"
    assert got["synthetic"] == "Synthetic test data – not real passenger records"
    assert 'class="tpd-synthetic-pill"' in text and "(not available)" not in text


def test_dataset_meta_error_view_and_escaping():
    text = metric_cards.dataset_meta_html(dataset_view(status="Dummy dataset (synthetic) · Not evaluated",
                                                       counts=None, evaluated_at=None, generator=None, file=EVIL), IST)
    got = meta_values(text)
    assert got["status"] == "Dummy dataset (synthetic) · Not evaluated"
    for key in ("total", "tp", "tn", "fp", "fn", "check", "when", "generator"):
        assert got[key] == "N/A", key
    assert text.count("(not available)") == 8
    assert got["synthetic"] == "Synthetic test data – not real passenger records"
    assert "<script>" not in text and "&lt;script&gt;" in text


def test_model_result_converts_fractions():
    r = metric_cards.model_result(model())
    assert r.accuracy is None and abs(r.precision - 80.0) < 1e-9 and abs(r.f1 - 80.0) < 1e-9
