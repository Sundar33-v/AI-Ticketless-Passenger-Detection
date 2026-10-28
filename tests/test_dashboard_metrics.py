"""Streamlit AppTests of the AI Model Performance & Metrics Calculator on the Dashboard."""

from __future__ import annotations

import html
import re
import sqlite3
from datetime import timedelta

import pytest
from streamlit.testing.v1.element_tree import Block

from app.database import Database, to_iso, utc_now
from app.evaluation import Counts, EvaluationRecord, store_evaluation
from app.metric_evaluation import ALL_ZERO_MESSAGE, NO_EVALUATION_MESSAGE, EvaluationCounts, save_manual_evaluation
from test_dashboard import TABLES, raw_html, row_counts, run
from test_dashboard_pages import prepare

PAGE = "0_Dashboard.py"
REFERENCE = {"eval_tp": 50, "eval_tn": 40, "eval_fp": 5, "eval_fn": 5}


@pytest.fixture
def path(tmp_path):
    return prepare("empty", tmp_path)


def walk(node):
    yield node
    for child in getattr(node, "children", {}).values():
        yield from walk(child)


def block_keys(root):
    return [n.key for n in walk(root) if isinstance(n, Block) and getattr(n, "key", None)]


def cards(at):
    blob = " ".join(raw_html(at))
    return re.findall(r'data-metric="(\w+)" data-value="([^"]+)"', blob)


def card_values(at):
    return [v for _, v in cards(at)]


def meta(at, key):
    blob = " ".join(raw_html(at))
    found = re.findall(rf'data-meta="{key}"><dt>[^<]*</dt><dd>([^<]*)</dd>', blob)
    return html.unescape(found[-1]) if found else None


def notes(at):
    return [html.unescape(m) for m in re.findall(r'class="tpd-eval-empty">([^<]*)<', " ".join(raw_html(at)))]


def runs_html(at):
    return next((b for b in raw_html(at) if 'aria-label="Recent evaluation runs"' in b), "")


def count_rows(path):
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT COUNT(*) FROM metric_evaluations").fetchone()[0]
    finally:
        conn.close()


def fill(at, values=REFERENCE, dataset=None):
    for key, value in values.items():
        at.number_input(key=key).set_value(value)
    if dataset is not None:
        at.text_input(key="eval_dataset").input(dataset)
    return at


def click(at, key):
    at.button(key=key).click()
    return run(at)


def manual(at):
    """Switch the calculator to Manual evaluation (the empty-DB default is the synthetic dataset)."""
    at.segmented_control(key="eval_mode").set_value("manual")
    return run(at)


def ds_meta(at, key):
    """A dataset-mode meta value (text only, the screen-reader suffix removed)."""
    blob = " ".join(raw_html(at))
    found = re.findall(rf'data-meta="{key}"><dt>[^<]*</dt><dd>(.*?)</dd>', blob)
    if not found:
        return None
    text = re.sub(r'<span class="tpd-sr">[^<]*</span>', "", found[-1])
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def store_model_eval(path):
    database = Database(path)
    database.initialize()
    conn = database.connect()
    try:
        store_evaluation(conn, EvaluationRecord("import", "yolov8n.pt", "v1", "bus01",
                                                Counts(4, 10, 10, 8, 2, 2, None, None, None, None)), utc_now())
    finally:
        conn.close()


# ------------------------------------------------------------------ placement (verify first)
@pytest.mark.parametrize("router", [False, True])
def test_panel_below_validation_results_and_quick_actions(open_dashboard, path, router):
    at = run(open_dashboard(path) if router else open_dashboard(path, PAGE))
    keys = block_keys(at.main)
    assert "card-metrics" in keys
    assert keys.index("card-validations") < keys.index("card-metrics")
    assert keys.index("card-actions") < keys.index("card-metrics")
    assert keys.index("dash-bottom") < keys.index("card-metrics")
    titles = [b for b in raw_html(at) if "tpd-section-title" in b]
    assert titles and "AI Model Performance &amp; Metrics Calculator" in titles[0]
    assert len(at.metric) == 0


# ------------------------------------------------------------------ empty state and calculation
def test_empty_db_defaults_to_dataset_mode(open_dashboard, path):
    at = run(open_dashboard(path, PAGE))
    assert at.segmented_control(key="eval_mode").value == "dataset"
    assert meta(at, "status") == "Dummy dataset (synthetic) · Calculated"
    assert at.button(key="ds_refresh").proto.label == "Calculate / Refresh Metrics"
    assert at.button(key="ds_regenerate").proto.label == "Regenerate dummy dataset"
    assert not at.error and not at.number_input
    assert any(c.value == "Dataset evaluations are calculated on demand and are not saved." for c in at.caption)


def test_empty_db_shows_na_and_message(open_dashboard, path):
    at = manual(run(open_dashboard(path, PAGE)))
    assert card_values(at) == ["N/A"] * 4
    assert notes(at) == [NO_EVALUATION_MESSAGE]
    assert at.segmented_control(key="eval_mode").value == "manual"
    assert meta(at, "status") == "Manual evaluation · Not evaluated"
    assert "No saved evaluation runs yet." in runs_html(at)
    for f in ("tp", "tn", "fp", "fn"):
        assert at.number_input(key=f"eval_{f}").value is None
        assert at.number_input(key=f"eval_{f}").proto.placeholder == "Whole number, e.g. 120"


def test_calculate_updates_cards_in_the_same_run(open_dashboard, path):
    at = manual(run(open_dashboard(path, PAGE)))
    click(fill(at), "eval_calculate")
    assert card_values(at) == ["90.00%", "90.91%", "90.91%", "90.91%"]
    assert meta(at, "status") == "Manual evaluation · Calculated · not saved"
    assert meta(at, "total") == "100"
    assert NO_EVALUATION_MESSAGE not in notes(at)
    assert count_rows(path) == 0
    keys = block_keys(at.main.get_by_key("card-metrics") if hasattr(at.main, "get_by_key") else at.main)
    assert keys.index("metric-results") < keys.index("metric-messages") < keys.index("metric-inputs")


def test_blank_count_error_and_na(open_dashboard, path):
    at = manual(run(open_dashboard(path, PAGE)))
    click(fill(at, {"eval_tn": 1, "eval_fp": 1, "eval_fn": 1}), "eval_calculate")
    assert any("True Positive (TP) is required" in e.value for e in at.error)
    assert card_values(at) == ["N/A"] * 4


def test_reset_clears_inputs_and_result(open_dashboard, path):
    at = manual(run(open_dashboard(path, PAGE)))
    click(fill(at), "eval_calculate")
    click(at, "eval_reset")
    assert all(at.number_input(key=k).value is None for k in REFERENCE)
    assert card_values(at) == ["N/A"] * 4 and notes(at) == [NO_EVALUATION_MESSAGE]


def test_all_zero_state(open_dashboard, path):
    at = manual(run(open_dashboard(path, PAGE)))
    click(fill(at, {k: 0 for k in REFERENCE}), "eval_calculate")
    assert card_values(at) == ["N/A"] * 4
    assert meta(at, "status") == "Manual evaluation · Calculated · not saved" and meta(at, "total") == "0"
    assert notes(at) == [ALL_ZERO_MESSAGE]


def test_mode_round_trip_keeps_counts(open_dashboard, path):
    at = manual(run(open_dashboard(path, PAGE)))
    click(fill(at), "eval_calculate")
    at.segmented_control(key="eval_mode").set_value("model")
    run(at)
    assert not at.number_input  # the form is manual-only
    assert notes(at) == [NO_EVALUATION_MESSAGE]
    assert any("mAP: not computed" in b for b in raw_html(at))
    at.segmented_control(key="eval_mode").set_value("dataset")
    run(at)
    assert not at.number_input and meta(at, "status") == "Dummy dataset (synthetic) · Calculated"
    at.segmented_control(key="eval_mode").set_value("manual")
    run(at)
    assert at.number_input(key="eval_tp").value == 50
    assert card_values(at) == ["90.00%", "90.91%", "90.91%", "90.91%"]


def test_demo_db_never_substitutes_ticket_counts(open_dashboard, tmp_path):
    path = prepare("demo", tmp_path)
    before = row_counts(path)
    at = manual(run(open_dashboard(path, PAGE)))
    assert card_values(at) == ["N/A"] * 4 and notes(at) == [NO_EVALUATION_MESSAGE]
    run(at)
    assert row_counts(path) == before and set(before) == set(TABLES)


# ------------------------------------------------------------------ saving
def test_save_lists_run_in_same_run_and_rejects_duplicate(open_dashboard, path):
    at = manual(run(open_dashboard(path, PAGE)))
    assert not at.button(key="eval_save").disabled and not at.button(key="eval_save").proto.help
    click(fill(at, dataset="bus camera test set v2"), "eval_save")
    assert count_rows(path) == 1
    assert meta(at, "status") == "Manual evaluation · Saved as run #1"
    assert any("Saved as manual evaluation run #1." in s.value for s in at.success)
    table = runs_html(at)
    assert 'data-run="#1"' in table and "Manual evaluation" in table and "TP 50 · TN 40 · FP 5 · FN 5" in table
    assert "<td>Dashboard</td>" in table
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute("SELECT accuracy_pct, f1_pct, dataset_description FROM metric_evaluations").fetchone()
    finally:
        conn.close()
    assert row[0] == pytest.approx(90.0) and row[1] == pytest.approx(90.909090909) and row[2].startswith("bus")
    click(at, "eval_save")
    assert any(i.value.startswith("Already saved as run #1.") for i in at.info)
    assert count_rows(path) == 1


def test_save_without_dataset(open_dashboard, path):
    at = manual(run(open_dashboard(path, PAGE)))
    click(fill(at), "eval_save")
    assert any("Dataset description is required to save" in e.value for e in at.error)
    assert count_rows(path) == 0


def test_interrupted_save_recovered(open_dashboard, path):
    database = Database(path)
    database.initialize()
    started = utc_now() - timedelta(seconds=5)
    conn = database.connect()
    try:
        save_manual_evaluation(conn, EvaluationCounts(50, 40, 5, 5), dataset_description="bus set", model_name=None,
                               model_version=None, now=utc_now())
    finally:
        conn.close()
    at = open_dashboard(path, PAGE)
    at.session_state["_eval_saving"] = ((50, 40, 5, 5, "bus set", None, None), to_iso(started))
    manual(run(at))
    click(fill(at, dataset="bus set"), "eval_save")
    assert any(i.value.startswith("Already saved as run #1.") for i in at.info)
    assert count_rows(path) == 1
    assert "_eval_saving" not in at.session_state


def test_fresh_session_shows_last_saved_run(open_dashboard, path):
    at = manual(run(open_dashboard(path, PAGE)))
    click(fill(at, dataset="bus set"), "eval_save")
    fresh = manual(run(open_dashboard(path, PAGE)))
    assert meta(fresh, "status") == "Manual evaluation · Last saved run #1"
    assert card_values(fresh) == ["90.00%", "90.91%", "90.91%", "90.91%"]


# ------------------------------------------------------------------ model evaluation results
def test_stored_model_evaluation_is_default_mode(open_dashboard, path):
    store_model_eval(path)
    at = run(open_dashboard(path, PAGE))
    assert at.segmented_control(key="eval_mode").value == "model"
    assert card_values(at) == ["N/A", "80.00%", "80.00%", "80.00%"]
    assert meta(at, "status") == "Model evaluation results · Stored evaluation #1"
    assert meta(at, "model") == "yolov8n.pt · v1" and meta(at, "total") == "4 images · 10 labelled boxes"
    for _ in range(3):
        run(at)
        assert at.segmented_control(key="eval_mode").value == "model"
    assert not at.warning
    table = runs_html(at)
    assert "model #1" in table and "Model evaluation results" in table and "TP 8 · FP 2 · FN 2 (boxes)" in table
    assert "4 images" in table


def test_runs_table_lists_manual_and_model_rows(open_dashboard, path):
    store_model_eval(path)
    at = run(open_dashboard(path, PAGE))
    at.segmented_control(key="eval_mode").set_value("manual")
    run(at)
    click(fill(at, dataset="bus set"), "eval_save")
    table = runs_html(at)
    assert table.index('data-run="#1"') < table.index('data-run="model #1"')  # newest first
    assert "Manual evaluation" in table and "Model evaluation results" in table


def test_reruns_write_nothing_operational(open_dashboard, path):
    before = row_counts(path)
    at = manual(run(open_dashboard(path, PAGE)))
    click(fill(at), "eval_calculate")
    run(at)
    assert row_counts(path) == before and count_rows(path) == 0
    assert len(at.metric) == 0


# ------------------------------------------------------------------ dummy dataset mode
def independent_metrics(csv_path):
    """TP/TN/FP/FN and the four percentages counted here straight from the CSV text (no app code)."""
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    header = lines[0].split(",")
    gi, pi = header.index("ground_truth"), header.index("predicted_label")
    tp = tn = fp = fn = 0
    for line in lines[1:]:
        cells = line.split(",")
        g, p = cells[gi], cells[pi]
        tp += (g, p) == ("1", "1")
        tn += (g, p) == ("0", "0")
        fp += (g, p) == ("0", "1")
        fn += (g, p) == ("1", "0")
    precision, recall = tp / (tp + fp) * 100, tp / (tp + fn) * 100
    values = [(tp + tn) / (tp + tn + fp + fn) * 100, precision, recall,
              2 * precision * recall / (precision + recall)]
    return (tp, tn, fp, fn), [f"{v:.2f}%" for v in values]


def test_dataset_cards_match_the_csv(open_dashboard, tmp_path):
    path = prepare("demo", tmp_path)  # KPI counters are non-zero here and must not leak into the cards
    csv_path = tmp_path / "dummy_evaluation_dataset.csv"
    (tp, tn, fp, fn), expected = independent_metrics(csv_path)
    at = run(open_dashboard(path, PAGE))
    assert card_values(at) == expected
    assert all(v != "N/A" for v in card_values(at))
    assert [ds_meta(at, k) for k in ("tp", "tn", "fp", "fn")] == [str(tp), str(tn), str(fp), str(fn)]
    assert ds_meta(at, "total") == str(tp + tn + fp + fn) == "200"
    assert ds_meta(at, "check") == "TP + TN + FP + FN = 200 ✓"
    assert ds_meta(at, "synthetic") == "Synthetic test data – not real passenger records"
    assert ds_meta(at, "dataset") == "Dummy evaluation dataset (synthetic) · dummy_evaluation_dataset.csv"
    assert ds_meta(at, "file") == "dummy_evaluation_dataset.csv"
    assert ds_meta(at, "generator") == "Matches generator output (seed 20260115, 200 samples)"
    assert ds_meta(at, "method").startswith("Confusion matrix of ground_truth vs predicted_label")
    assert ds_meta(at, "when") not in (None, "N/A")
    blob = " ".join(raw_html(at))
    assert "not real-world model accuracy" in blob and "1 = passenger without a valid ticket" in blob


def test_dataset_refresh_updates_time(open_dashboard, path):
    at = run(open_dashboard(path, PAGE))
    first = at.session_state["_ds_result"]["evaluated_at"]
    run(at)
    assert at.session_state["_ds_result"]["evaluated_at"] == first  # plain reruns reuse the result
    click(at, "ds_refresh")
    assert at.session_state["_ds_result"]["evaluated_at"] > first
    assert meta(at, "status") == "Dummy dataset (synthetic) · Calculated"


def test_dataset_regenerate_restores_deleted_file(open_dashboard, path, tmp_path):
    csv_path = tmp_path / "dummy_evaluation_dataset.csv"
    original = csv_path.read_bytes()
    at = run(open_dashboard(path, PAGE))
    expected = card_values(at)
    csv_path.unlink()
    click(at, "ds_refresh")
    assert any(e.value.startswith("Dataset file not found: dummy_evaluation_dataset.csv") for e in at.error)
    assert card_values(at) == ["N/A"] * 4
    assert any("Use Regenerate dummy dataset" in c.value for c in at.caption)
    click(at, "ds_regenerate")
    assert csv_path.read_bytes() == original
    assert any(s.value.startswith("Dummy dataset regenerated (seed 20260115, 200 synthetic samples).")
               for s in at.success)
    assert card_values(at) == expected and not at.error


@pytest.mark.parametrize("content", [b"", b"sample_id,tracking_id,ground_truth\nSYN-1,SYN-T1,1\n",
                                     b"sample_id,tracking_id,ground_truth,predicted_label\nSYN-1,SYN-T1,2,1\n",
                                     b"sample_id,tracking_id,ground_truth,predicted_label\nSYN-1,SYN-T1,,1\n"])
def test_dataset_malformed_file_shows_error_and_na(open_dashboard, path, tmp_path, content):
    (tmp_path / "dummy_evaluation_dataset.csv").write_bytes(content)
    at = run(open_dashboard(path, PAGE))
    assert at.error and card_values(at) == ["N/A"] * 4
    assert meta(at, "status") == "Dummy dataset (synthetic) · Not evaluated"
    for key in ("total", "tp", "tn", "fp", "fn", "check", "when", "generator"):
        assert ds_meta(at, key) == "N/A", key
    assert ds_meta(at, "synthetic") == "Synthetic test data – not real passenger records"


def test_dataset_missing_file_shows_error(open_dashboard, path, tmp_path):
    (tmp_path / "dummy_evaluation_dataset.csv").unlink()
    at = run(open_dashboard(path, PAGE))
    assert any(e.value.startswith("Dataset file not found") for e in at.error)
    assert card_values(at) == ["N/A"] * 4


def test_dataset_locked_file_shows_error_without_exception(open_dashboard, path, monkeypatch):
    from pathlib import Path

    real = Path.read_bytes

    def locked(self):
        if self.name == "dummy_evaluation_dataset.csv":
            raise PermissionError(13, "Permission denied")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", locked)
    at = open_dashboard(path, PAGE)
    at.run()
    assert not at.exception
    assert any(e.value.startswith("Dataset file could not be read: dummy_evaluation_dataset.csv (Permission denied)")
               for e in at.error)
    assert card_values(at) == ["N/A"] * 4


def test_dataset_non_synthetic_file_is_never_overwritten(open_dashboard, path, tmp_path, monkeypatch):
    real = tmp_path / "real_labels.csv"
    real.write_bytes(b"sample_id,tracking_id,ground_truth,predicted_label\nREAL-001,42,1,0\n")
    before = real.read_bytes()
    monkeypatch.setenv("TPD_EVAL_DATASET", str(real))
    at = run(open_dashboard(path, PAGE))
    assert any("not marked synthetic" in e.value for e in at.error)
    assert any("will not be overwritten" in c.value for c in at.caption)
    assert card_values(at) == ["N/A"] * 4
    click(at, "ds_regenerate")
    assert any(e.value.startswith("Refusing to overwrite real_labels.csv") for e in at.error)
    assert not at.success
    assert real.read_bytes() == before
