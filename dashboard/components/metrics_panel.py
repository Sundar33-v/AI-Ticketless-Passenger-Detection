"""The AI Model Performance & Metrics Calculator card (Dashboard, below Recent Validation Results + Quick Actions).

Three modes: Dummy dataset (synthetic) (TP/TN/FP/FN counted from the per-sample labels of
``data/dummy_evaluation_dataset.csv`` by ``app.evaluation_dataset``), Manual evaluation
(TP/TN/FP/FN typed from a labelled test dataset) and Model evaluation results (the latest
``model_evaluations`` row stored by ``python -m app.evaluate``). No KPI, ticket or detection
counter is ever read here, so dashboard counts can never be substituted into the formulas.
The panel runs in its own fragment without ``run_every``: the 5-second body refresh never
wipes typed values or results.

Render order (binding): the results and messages slots are created above the form / buttons
and filled after the button handlers ran, so one click updates the cards in the same run.
Manual runs are written only by the explicit Save Evaluation (append-only). Dataset
evaluations are computed on demand and never written to the database; the only file the
panel writes is the synthetic dataset itself (Regenerate, never over a non-synthetic file).
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

import streamlit as st

from app import evaluation_dataset as ds
from app import metric_evaluation as me
from app.database import parse_iso, to_iso, utc_now
from app.evaluation import DEFAULT_IOU
from app.passenger_service import is_busy_error
from dashboard.components import data, metric_cards
from dashboard.components.shell import PageContext

logger = logging.getLogger("dashboard")

PLACEHOLDER = "Whole number, e.g. 120"
TEXT_FIELDS = ("dataset", "model_name", "model_version")
MODEL_NOTES = ("mAP: not computed by the evaluation pipeline.",
               "End-to-end ticket decision evaluation: not available (needs verified ground-truth boarding records).")
RUN_HINT = ("To add results, run scripts\\run_evaluation.bat (python -m app.evaluate images|mot|import) "
            "with a labelled dataset.")
MODES = (me.MODE_DATASET, me.MODE_MANUAL, me.MODE_MODEL)
DS_RESULT_KEY = "_ds_result"
DS_REGENERATED = (f"Dummy dataset regenerated (seed {ds.DEFAULT_SEED}, {ds.DEFAULT_SAMPLES} synthetic samples).")
DS_REGENERATE_HELP = (f"Rewrites the dataset file with the fixed-seed generator output (seed {ds.DEFAULT_SEED}, "
                      f"{ds.DEFAULT_SAMPLES} samples).")
DS_RECOVER = "Use Regenerate dummy dataset, or run scripts\\generate_evaluation_dataset.py."
DS_NOT_SYNTHETIC = ("This file is not a synthetic dataset and will not be overwritten. Unset TPD_EVAL_DATASET or "
                    "point it at a synthetic dataset file.")
DS_RUNS_CAPTION = "Dataset evaluations are calculated on demand and are not saved."


def render(ctx: PageContext) -> None:
    st.fragment(_metrics_fragment)(ctx)


def _bullets(errors: Dict[str, str]) -> str:
    return "\n".join(f"- {m}" for m in errors.values())


def _db_failed(exc: Exception) -> None:
    logger.warning("Metrics panel database read failed: %s", exc)
    st.error(f"Database error: {exc}")


# ------------------------------------------------------------------ mode
def _render_mode_selector() -> str:
    if "_eval_mode_default" in st.session_state:
        default = st.session_state["_eval_mode_default"]
    else:
        try:
            # a stored real labelled evaluation wins; otherwise the synthetic dataset
            default = me.MODE_MODEL if data.latest_evaluation() is not None else me.MODE_DATASET
            st.session_state["_eval_mode_default"] = default  # fixed once per session
        except (sqlite3.Error, OSError) as exc:
            logger.warning("Could not read stored model evaluations: %s", exc)
            default = me.MODE_DATASET  # fallback, not persisted
    mode = st.segmented_control("Evaluation mode", list(MODES), format_func=me.MODE_LABELS.get,
                                key="eval_mode", required=True, default=default)
    return mode if mode in MODES else me.MODE_DATASET


def _latest_model():
    try:
        return data.latest_evaluation(), True
    except (sqlite3.Error, OSError) as exc:
        _db_failed(exc)
        return None, False


def _render_source_line(mode: str) -> None:
    if mode == me.MODE_MANUAL:
        st.html(metric_cards.source_html(me.MANUAL_DISCLAIMER))
    elif mode == me.MODE_DATASET:
        st.html(metric_cards.source_html(me.DATASET_DISCLAIMER))
        st.html(metric_cards.source_html(me.LABEL_LEGEND))
    elif mode == me.MODE_MODEL:
        latest, _ok = _latest_model()
        iou = latest.iou_threshold if latest is not None else DEFAULT_IOU
        st.html(metric_cards.source_html(
            f"Labelled dataset evaluated by python -m app.evaluate (person detection, IoU ≥ {iou:g}). "
            "Accuracy is image-level person presence."))


# ------------------------------------------------------------------ dataset mode
def _evaluate_dataset(path: Path, now) -> dict:
    """Load + evaluate the dataset file; an error dict instead of raising (the fragment never raises)."""
    try:
        loaded = ds.load_dataset(path)
        evaluation = ds.evaluate(loaded)
    except ds.DatasetError as exc:
        logger.warning("Evaluation dataset %s could not be used: %s %s", path, exc.message, list(exc.details))
        return {"error": exc, "path": path}
    except (ValueError, RuntimeError) as exc:
        logger.warning("Dataset evaluation failed for %s: %s", path, exc, exc_info=True)
        return {"error": f"Dataset evaluation failed: {exc}", "path": path}
    return {"evaluation": evaluation, "dataset": loaded, "evaluated_at": now, "path": path}


def _handle_regenerate(path: Path, slot, now) -> None:
    """Rewrite the synthetic dataset (never over a non-synthetic file), then evaluate it."""
    try:
        ds.write_dataset(path, ds.generate_samples())
    except ds.DatasetError as exc:  # refused: the previous result and the file stay as they are
        logger.warning("Regenerate refused for %s: %s", path, exc.message)
        slot.error(exc.message)
        return
    except OSError as exc:
        logger.warning("Could not write the evaluation dataset %s: %s", path, exc)
        slot.error(f"Could not write the dataset file {ds.display_path(path)}: {exc.strerror or exc}")
        return
    result = _evaluate_dataset(path, now)
    st.session_state[DS_RESULT_KEY] = result
    if "error" not in result:
        slot.success(DS_REGENERATED)


def _dataset_view(result: dict, path: Path) -> metric_cards.DatasetMetaView:
    label = me.MODE_LABELS[me.MODE_DATASET]
    dataset = f"{ds.DATASET_NAME} · {Path(path).name}"
    shown = ds.display_path(path)
    if "error" in result:
        return metric_cards.DatasetMetaView(f"{label} · Not evaluated", dataset, shown, None, None,
                                            ds.METHOD_TEXT, ds.MODEL_TEXT, None)
    loaded = result["dataset"]
    if loaded.matches_generator:
        generator = f"Matches generator output (seed {ds.DEFAULT_SEED}, {len(loaded.samples)} samples)"
    else:
        generator = f"File differs from the seed-{ds.DEFAULT_SEED} generator output"
    return metric_cards.DatasetMetaView(f"{label} · Calculated", dataset, shown, result["evaluation"].counts,
                                        result["evaluated_at"], ds.METHOD_TEXT, ds.MODEL_TEXT, generator)


def _show_dataset_error(result: dict, slot) -> None:
    error = result["error"]
    if isinstance(error, ds.DatasetError):
        text = error.message + ("\n" + "\n".join(f"- {d}" for d in error.details) if error.details else "")
    else:
        text = str(error)
    slot.error(text)
    replaceable, _reason = ds.is_replaceable(result["path"])
    slot.caption(DS_RECOVER if replaceable else DS_NOT_SYNTHETIC)


def _render_dataset(ctx: PageContext, results_slot, messages_slot, now) -> None:
    with st.container(key="metric-dataset-buttons", horizontal=True):
        refresh = st.button("Calculate / Refresh Metrics", key="ds_refresh", type="primary")
        regenerate = st.button("Regenerate dummy dataset", key="ds_regenerate", help=DS_REGENERATE_HELP)
    path = ds.dataset_path()
    if regenerate:
        _handle_regenerate(path, messages_slot, now)
    elif refresh:
        st.session_state[DS_RESULT_KEY] = _evaluate_dataset(path, now)
    result = st.session_state.get(DS_RESULT_KEY)
    if not isinstance(result, dict) or result.get("path") != path:  # first render (auto-evaluate) or new file
        result = st.session_state[DS_RESULT_KEY] = _evaluate_dataset(path, now)
    with results_slot:
        evaluation = result.get("evaluation")
        st.html(metric_cards.metric_cards_html(evaluation.result if evaluation is not None else None))
        st.html(metric_cards.dataset_meta_html(_dataset_view(result, path), ctx.tz))
    if "error" in result:
        _show_dataset_error(result, messages_slot)


# ------------------------------------------------------------------ form
def _reset() -> None:
    for f in me.COUNT_FIELDS:
        st.session_state[f"eval_{f}"] = None
    for f in TEXT_FIELDS:
        st.session_state[f"eval_{f}"] = ""
    for key in ("_eval_result", "_eval_saved_fp", "_eval_saving", "_eval_inputs"):
        st.session_state.pop(key, None)


def _raw_inputs() -> Dict[str, object]:
    values = {f: st.session_state.get(f"eval_{f}") for f in me.COUNT_FIELDS}
    values.update({f: st.session_state.get(f"eval_{f}") or "" for f in TEXT_FIELDS})
    return values


def _render_form(ctx: PageContext) -> Optional[str]:
    """Count inputs, dataset/model fields and the three buttons; returns "calculate" | "save" | None."""
    for key, value in st.session_state.get("_eval_inputs", {}).items():
        st.session_state.setdefault(f"eval_{key}", value)  # restore after a mode switch (before the widgets)
    with st.form("eval-form", clear_on_submit=False, enter_to_submit=False, border=False):
        with st.container(key="metric-inputs"):
            for column, f in zip(st.columns(4), me.COUNT_FIELDS):
                with column:
                    st.number_input(me.COUNT_LABELS[f], min_value=0, max_value=me.MAX_COUNT, value=None, step=1,
                                    format="%d", key=f"eval_{f}", placeholder=PLACEHOLDER)
        with st.container(key="metric-details"):
            dataset_col, name_col, version_col = st.columns([2, 1.2, 1])
            with dataset_col:
                st.text_input("Dataset description (required to save)", key="eval_dataset", max_chars=200)
            with name_col:
                st.text_input("Model name", key="eval_model_name", max_chars=200,
                              placeholder=Path(ctx.cfg.detection.model_path).name)
            with version_col:
                st.text_input("Model version", key="eval_model_version", max_chars=200)
        with st.container(key="metric-buttons", horizontal=True):
            calculate = st.form_submit_button("Calculate Metrics", key="eval_calculate", type="primary")
            st.form_submit_button("Reset", key="eval_reset", on_click=_reset)
            save = st.form_submit_button("Save Evaluation", key="eval_save")
    if calculate:
        return "calculate"
    return "save" if save else None


def _session_result(counts: me.EvaluationCounts, raw: Dict[str, object], now, run_id: Optional[int]) -> dict:
    return {"counts": counts, "result": me.calculate_metrics(counts.tp, counts.tn, counts.fp, counts.fn),
            "calculated_at": now, "run_id": run_id, "source": "session",
            "dataset": " ".join(str(raw.get("dataset") or "").split()),
            "model_name": " ".join(str(raw.get("model_name") or "").split()),
            "model_version": " ".join(str(raw.get("model_version") or "").split())}


def _handle_calculate(slot, now) -> None:
    raw = _raw_inputs()
    st.session_state["_eval_inputs"] = raw
    try:
        counts = me.validate_counts(raw)
    except me.EvaluationInputError as exc:
        st.session_state["_eval_result"] = None  # cards show N/A, never stale values
        slot.error(_bullets(exc.field_errors))
        return
    st.session_state["_eval_result"] = _session_result(counts, raw, now, None)


def _handle_save(slot, now) -> None:
    raw = _raw_inputs()
    st.session_state["_eval_inputs"] = raw
    errors: Dict[str, str] = {}
    counts = None
    try:
        counts = me.validate_counts(raw)
    except me.EvaluationInputError as exc:
        errors.update(exc.field_errors)
        st.session_state["_eval_result"] = None
    try:
        dataset, name, version = me.validate_save_fields(raw["dataset"], raw["model_name"], raw["model_version"])
    except me.EvaluationInputError as exc:
        errors.update(exc.field_errors)
    if counts is not None and not errors and counts.total < 1:
        errors["total"] = me.MSG_TOTAL
    if errors:
        slot.error(_bullets(errors))
        return
    fingerprint = (counts.tp, counts.tn, counts.fp, counts.fn, dataset, name, version)
    saved = st.session_state.get("_eval_saved_fp")
    if saved and saved[0] == fingerprint:
        slot.info(f"Already saved as run #{saved[1]}. Change the counts or details to save a new run.")
        return
    try:
        with data.write_conn() as conn:
            prev = st.session_state.get("_eval_saving")
            if prev and prev[0] == fingerprint:
                # interrupted-save recovery: did the earlier attempt commit before it was interrupted?
                run_id = me.find_recent_duplicate(conn, counts, dataset, name, version,
                                                  since=parse_iso(prev[1]))
                if run_id is not None:
                    st.session_state["_eval_saved_fp"] = (fingerprint, run_id)
                    st.session_state["_eval_result"] = _session_result(counts, raw, now, run_id)
                    st.session_state.pop("_eval_saving", None)
                    slot.info(f"Already saved as run #{run_id}. Change the counts or details to save a new run.")
                    return
            st.session_state["_eval_saving"] = (fingerprint, to_iso(now))
            run_id = me.save_manual_evaluation(conn, counts, dataset_description=dataset, model_name=name,
                                               model_version=version, now=now)
    except me.EvaluationInputError as exc:
        st.session_state.pop("_eval_saving", None)
        slot.error(_bullets(exc.field_errors))
        return
    except sqlite3.OperationalError as exc:
        st.session_state.pop("_eval_saving", None)
        logger.warning("Saving a manual evaluation failed: %s", exc)
        if is_busy_error(exc):
            slot.warning("Database busy, try again. The evaluation was not saved.")
        else:
            slot.error("Could not save the evaluation: database error.")
        return
    except (sqlite3.Error, OSError) as exc:
        st.session_state.pop("_eval_saving", None)
        logger.warning("Saving a manual evaluation failed: %s", exc)
        slot.error("Could not save the evaluation: database error.")
        return
    st.session_state["_eval_result"] = _session_result(counts, raw, now, run_id)
    st.session_state["_eval_saved_fp"] = (fingerprint, run_id)
    st.session_state.pop("_eval_saving", None)
    slot.success(f"Saved as manual evaluation run #{run_id}.")


# ------------------------------------------------------------------ results
def _show(result, status: str, total: str, when: str, model: str, dataset: str, note: Optional[str]) -> None:
    st.html(metric_cards.metric_cards_html(result))
    st.html(metric_cards.meta_html(status, total, when, model, dataset))
    if note:
        st.html(metric_cards.status_note_html(note))


def _render_manual_results(ctx: PageContext) -> None:
    label = me.MODE_LABELS[me.MODE_MANUAL]
    res = st.session_state.get("_eval_result")
    if res:
        counts = res["counts"]
        state = f"Saved as run #{res['run_id']}" if res.get("run_id") else "Calculated · not saved"
        note = me.ALL_ZERO_MESSAGE if counts.total == 0 else None
        _show(res["result"], f"{label} · {state}", str(counts.total),
              metric_cards.local_time(res["calculated_at"], ctx.tz),
              metric_cards.model_text(res.get("model_name"), res.get("model_version"), missing="Not provided"),
              res.get("dataset") or "Not provided", note)
        return
    try:
        latest = data.manual_evaluations(1)
    except (sqlite3.Error, OSError) as exc:
        _db_failed(exc)
        latest = []
    if latest:
        run = latest[0]
        _show(run.result, f"{label} · Last saved run #{run.run_id}", str(run.counts.total),
              metric_cards.local_time(run.evaluated_at, ctx.tz),
              metric_cards.model_text(run.model_name, run.model_version, missing="Not provided"),
              run.dataset_description, None)
        return
    _show(None, f"{label} · Not evaluated", "N/A", "N/A", "N/A", "N/A", me.NO_EVALUATION_MESSAGE)


def _render_model_results(ctx: PageContext) -> None:
    label = me.MODE_LABELS[me.MODE_MODEL]
    latest, _ok = _latest_model()
    if latest is None:
        _show(None, f"{label} · Not evaluated", "N/A", "N/A", "N/A", "N/A", me.NO_EVALUATION_MESSAGE)
        return
    _show(metric_cards.model_result(latest), f"{label} · Stored evaluation #{latest.evaluation_id}",
          f"{latest.test_set_size} images · {latest.counts.gt_boxes} labelled boxes",
          metric_cards.local_time(latest.evaluated_at, ctx.tz),
          metric_cards.model_text(latest.model_name, latest.model_version), latest.dataset_name, None)


def _render_model_notes() -> None:
    st.caption(RUN_HINT)
    st.html('<div class="tpd-eval-notes">' + "".join(f"<p>{metric_cards._e(n)}</p>" for n in MODEL_NOTES)
            + "</div>")


def _render_runs_table(ctx: PageContext) -> None:
    st.html(f'<h3 class="tpd-card-title">{metric_cards._e(metric_cards.RUNS_TITLE)}</h3>')
    rows: List = []
    try:
        rows = list(data.manual_evaluations(10)) + list(data.evaluations(10))
    except (sqlite3.Error, OSError) as exc:
        _db_failed(exc)
        return
    rows.sort(key=lambda r: r.evaluated_at, reverse=True)
    st.html(metric_cards.runs_table_html(rows[:10], ctx.tz))


def _metrics_fragment(ctx: PageContext) -> None:
    now = utc_now()  # first, per the fragment rule (never ctx time)
    with st.container(border=True, key="card-metrics"):
        st.html(metric_cards.title_html())
        mode = _render_mode_selector()
        _render_source_line(mode)
        results_slot = st.container(key="metric-results")
        messages_slot = st.container(key="metric-messages")
        if mode == me.MODE_DATASET:
            _render_dataset(ctx, results_slot, messages_slot, now)
        elif mode == me.MODE_MANUAL:
            clicked = _render_form(ctx)
            if clicked == "calculate":
                _handle_calculate(messages_slot, now)
            elif clicked == "save":
                _handle_save(messages_slot, now)
            with results_slot:
                _render_manual_results(ctx)
        elif mode == me.MODE_MODEL:
            _render_model_notes()
            with results_slot:
                _render_model_results(ctx)
        _render_runs_table(ctx)
        if mode == me.MODE_DATASET:
            st.caption(DS_RUNS_CAPTION)
