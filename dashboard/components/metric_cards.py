"""Pure HTML builders for the AI Model Performance & Metrics Calculator (no Streamlit calls, no DB access).

Every interpolated string passes through ``html.escape`` and every number through
``int()`` / ``format_percent``. ``data-metric`` / ``data-value`` are the test hooks.
Manual runs store percentages; pipeline runs (``model_evaluations``) store fractions,
which :func:`model_result` converts (×100, None stays None).
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence, Union
from zoneinfo import ZoneInfo

from app.evaluation import StoredEvaluation
from app.metric_evaluation import (MODE_LABELS, MODE_MANUAL, MODE_MODEL, EvaluationCounts, MetricResult,
                                   StoredManualEvaluation, format_percent)
from dashboard.components import icons
from dashboard.components.theme import CATEGORY_COLORS
from dashboard.components.tokens import tint

TITLE = "AI Model Performance & Metrics Calculator"
RUNS_TITLE = "Recent Evaluation Runs"
NO_RUNS = "No saved evaluation runs yet."
METRIC_SPECS = (("accuracy", "Accuracy Score (%)"), ("precision", "Precision (%)"), ("recall", "Recall (%)"),
                ("f1", "F1-Score (%)"))
RUN_COLUMNS = ("Run", "Date/time", "Mode", "Dataset", "Model", "Counts", "Total cases", "Accuracy", "Precision",
               "Recall", "F1", "Saved by")
MANUAL_SOURCE = "Dashboard"  # "Saved by" for manual runs (the dashboard has no user accounts)
TIME_FORMAT = "%d %b %Y %H:%M %Z"
EMPTY_RESULT = MetricResult(None, None, None, None)
SYNTHETIC_PILL = "Synthetic test data – not real passenger records"

Run = Union[StoredManualEvaluation, StoredEvaluation]


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def title_html() -> str:
    return (f'<h2 class="tpd-card-title tpd-section-title">{icons.icon(icons.SHIELD, "#2F6BFF", 20)} '
            f'{_e(TITLE)}</h2>')


def model_result(stored: StoredEvaluation) -> MetricResult:
    """Pipeline fractions (0..1) as percentages; None stays None (shown as N/A)."""
    s = stored.scores

    def pct(v: Optional[float]) -> Optional[float]:
        return None if v is None else float(v) * 100

    return MetricResult(pct(s.accuracy), pct(s.precision), pct(s.recall), pct(s.f1))


def metric_cards_html(result: Optional[MetricResult]) -> str:
    """Four cards in fixed order: Accuracy Score (%), Precision (%), Recall (%), F1-Score (%)."""
    result = result or EMPTY_RESULT
    cards = []
    for slug, label in METRIC_SPECS:
        text = format_percent(getattr(result, slug))
        colour = CATEGORY_COLORS[slug]
        sr = '<span class="tpd-sr"> (not available)</span>' if text == "N/A" else ""
        cards.append(f'<div class="tpd-metric" data-metric="{slug}" data-value="{_e(text)}" '
                     f'style="border-top-color:{colour};background:{tint(colour, .10)}">'
                     f'<div class="tpd-metric-label">{_e(label)}</div>'
                     f'<div class="tpd-metric-value">{_e(text)}{sr}</div></div>')
    return '<div class="tpd-metric-grid" role="group" aria-label="Model performance metrics">' + "".join(cards) \
        + "</div>"


def meta_html(status: str, total: str, when: str, model: str, dataset: str) -> str:
    """Evaluation status, Total evaluated cases, Evaluation date/time, Model version, Dataset."""
    items = (("Evaluation status", status, "status"), ("Total evaluated cases", total, "total"),
             ("Evaluation date/time", when, "when"), ("Model version", model, "model"),
             ("Dataset", dataset, "dataset"))
    body = "".join(f'<div data-meta="{key}"><dt>{_e(label)}</dt><dd>{_e(value)}</dd></div>'
                   for label, value, key in items)
    return f'<dl class="tpd-eval-meta">{body}</dl>'


@dataclass(frozen=True)
class DatasetMetaView:
    """What the dataset mode shows below the cards (built by ``metrics_panel._dataset_view``)."""

    status: str                          # "Dummy dataset (synthetic) · Calculated" | "… · Not evaluated"
    dataset: str                         # DATASET_NAME + " · " + file name
    file: str                            # evaluation_dataset.display_path(path)
    counts: Optional[EvaluationCounts]   # None -> total/tp/tn/fp/fn/check render "N/A"
    evaluated_at: Optional[datetime]     # None -> "N/A"
    method: str
    model: str
    generator: Optional[str]             # None -> "N/A"


def _meta_value(value: object) -> str:
    text = "N/A" if value is None else str(value)
    sr = '<span class="tpd-sr"> (not available)</span>' if text == "N/A" else ""
    return f"{_e(text)}{sr}"


def dataset_meta_html(view: DatasetMetaView, tz: ZoneInfo) -> str:
    """Status, dataset, synthetic pill, total, TP/TN/FP/FN, sum check, date/time, method, model, generator, file."""
    c = view.counts
    counts = {k: None if c is None else int(getattr(c, k)) for k in ("tp", "tn", "fp", "fn")}
    total = None if c is None else int(c.total)
    check = None if c is None else f"TP + TN + FP + FN = {total} ✓"
    when = None if view.evaluated_at is None else local_time(view.evaluated_at, tz)
    pill = f'<span class="tpd-synthetic-pill">{_e(SYNTHETIC_PILL)}</span>'
    items = (("status", "Evaluation status", _meta_value(view.status)),
             ("dataset", "Dataset", _meta_value(view.dataset)),
             ("synthetic", "Data type", pill),
             ("total", "Total samples", _meta_value(total)),
             ("tp", "True Positive (TP)", _meta_value(counts["tp"])),
             ("tn", "True Negative (TN)", _meta_value(counts["tn"])),
             ("fp", "False Positive (FP)", _meta_value(counts["fp"])),
             ("fn", "False Negative (FN)", _meta_value(counts["fn"])),
             ("check", "Sum check", _meta_value(check)),
             ("when", "Evaluation date/time", _meta_value(when)),
             ("method", "Evaluation method", _meta_value(view.method)),
             ("model", "Model", _meta_value(view.model)),
             ("generator", "Generator check", _meta_value(view.generator)),
             ("file", "File", _meta_value(view.file)))
    body = "".join(f'<div data-meta="{key}"><dt>{_e(label)}</dt><dd>{value}</dd></div>' for key, label, value in items)
    return f'<dl class="tpd-eval-meta tpd-eval-meta--dataset">{body}</dl>'


def status_note_html(text: str) -> str:
    return f'<div role="status" class="tpd-eval-empty">{_e(text)}</div>'


def source_html(text: str) -> str:
    return f'<p class="tpd-eval-source">{_e(text)}</p>'


def local_time(value, tz: ZoneInfo) -> str:
    return "N/A" if value is None else value.astimezone(tz).strftime(TIME_FORMAT)


def model_text(name: Optional[str], version: Optional[str], *, missing: str = "N/A") -> str:
    parts = [p for p in (name, version) if p]
    return " · ".join(parts) if parts else missing


def _run_cells(run: Run, tz: ZoneInfo) -> tuple:
    if isinstance(run, StoredManualEvaluation):
        c = run.counts
        return (f"#{int(run.run_id)}", local_time(run.evaluated_at, tz), MODE_LABELS[MODE_MANUAL],
                run.dataset_description, model_text(run.model_name, run.model_version, missing="Not provided"),
                f"TP {int(c.tp)} · TN {int(c.tn)} · FP {int(c.fp)} · FN {int(c.fn)}", str(int(c.total)),
                run.result, MANUAL_SOURCE)
    c = run.counts
    return (f"model #{int(run.evaluation_id)}", local_time(run.evaluated_at, tz), MODE_LABELS[MODE_MODEL],
            run.dataset_name, model_text(run.model_name, run.model_version),
            f"TP {int(c.tp)} · FP {int(c.fp)} · FN {int(c.fn)} (boxes)", f"{int(run.test_set_size)} images",
            model_result(run), "app.evaluate (CLI)")


def runs_table_html(rows: Sequence[Run], tz: ZoneInfo) -> str:
    """Manual and pipeline runs (already sorted newest first) as one striped table."""
    head = "".join(f'<th scope="col">{_e(c)}</th>' for c in RUN_COLUMNS)
    body = []
    for run in rows:
        run_label, when, mode, dataset, model, counts, total, result, saved_by = _run_cells(run, tz)
        metrics = "".join(f"<td>{_e(format_percent(getattr(result, slug)))}</td>" for slug, _ in METRIC_SPECS)
        body.append(f'<tr data-run="{_e(run_label)}"><td>{_e(run_label)}</td><td>{_e(when)}</td><td>{_e(mode)}</td>'
                    f'<td class="tpd-wrap">{_e(dataset)}</td><td class="tpd-wrap">{_e(model)}</td>'
                    f'<td>{_e(counts)}</td><td>{_e(total)}</td>{metrics}<td>{_e(saved_by)}</td></tr>')
    if not body:
        body.append(f'<tr><td colspan="{len(RUN_COLUMNS)}" class="tpd-empty">{_e(NO_RUNS)}</td></tr>')
    return ('<div class="tpd-table-wrap" role="region" aria-label="Recent evaluation runs" tabindex="0">'
            f'<table class="tpd-table"><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>')
