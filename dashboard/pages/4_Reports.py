"""Reports: period summary, failure reasons, AI evaluation and CSV exports (same queries as the dashboard).

No fragment: one ``now``/``period`` per script run, so the downloads stay stable.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Streamlit puts dashboard/ first on sys.path, where dashboard/app.py would shadow the
# project's `app` package. Always force the project root to the front.
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != ROOT]
sys.path.insert(0, str(ROOT))
if "app" in sys.modules and not hasattr(sys.modules["app"], "__path__"):
    del sys.modules["app"]  # drop a cached non-package `app` module

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from app.analytics import Period, compute_metrics  # noqa: E402
from app.database import utc_now  # noqa: E402
from app.evaluation import NA_MESSAGE, StoredEvaluation  # noqa: E402
from app.reports import (EXPORT_CAP, boardings_export, csv_bytes, report_summary, summary_frame,  # noqa: E402
                         validations_export)
from dashboard.components import charts, data, metrics, shell, tables  # noqa: E402
from dashboard.components.filters import PERIOD_LABELS  # noqa: E402


def evaluation_frame(ev: StoredEvaluation, tz) -> pd.DataFrame:
    c, s = ev.counts, ev.scores
    rows = [("Evaluation", f"#{ev.evaluation_id} ({ev.method})"),
            ("Evaluated (local)", f"{ev.evaluated_at.astimezone(tz):%Y-%m-%d %H:%M}"),
            ("Model", f"{ev.model_name} ({ev.model_version})"), ("Dataset", ev.dataset_name),
            ("Test set size", str(ev.test_set_size)), ("Positive class", ev.positive_class),
            ("IoU threshold", f"{ev.iou_threshold:g}"),
            ("Confidence threshold", "unknown" if ev.conf_threshold is None else f"{ev.conf_threshold:g}"),
            ("Averaging", ev.averaging), ("TP / FP / FN (boxes)", f"{c.tp} / {c.fp} / {c.fn}"),
            ("Image TP / FP / FN / TN", "unknown" if c.img_tp is None else
             f"{c.img_tp} / {c.img_fp} / {c.img_fn} / {c.img_tn}")]
    rows += list(zip(metrics.MODEL_LABELS, (metrics.percent(v) for v in (s.accuracy, s.precision, s.recall, s.f1))))
    rows.append(("Configuration", ", ".join(f"{k}={v}" for k, v in sorted(ev.config.items()))))
    return pd.DataFrame(rows, columns=["Item", "Value"])


def history_frame(rows) -> pd.DataFrame:
    return pd.DataFrame([{"#": ev.evaluation_id, "Evaluated (UTC)": f"{ev.evaluated_at:%Y-%m-%d %H:%M}",
                          "Model": f"{ev.model_name} ({ev.model_version})", "Dataset": ev.dataset_name,
                          "Size": ev.test_set_size, **{label: metrics.percent(v) for label, v in zip(
                              metrics.MODEL_LABELS, (ev.scores.accuracy, ev.scores.precision, ev.scores.recall,
                                                     ev.scores.f1))}} for ev in rows])


def download(label: str, frame: pd.DataFrame, kind: str, stamp: str) -> None:
    st.download_button(label, csv_bytes(frame), file_name=f"tpd_{kind}_{stamp}.csv", mime="text/csv",
                       on_click="ignore", key=f"dl_{kind}")


ctx = shell.start_page("Reports")
now = utc_now()
period = ctx.period_at(now)
with shell.guard():
    history = data.evaluations(10)
    with data.read_conn() as conn:
        summary = report_summary(conn, period, ctx.tz, now)
        validations, validations_over = validations_export(conn, period, list(ctx.statuses))
        boardings, boardings_over = boardings_export(conn, period, list(ctx.statuses))
        has_records = summary.metrics.is_empty() and not compute_metrics(conn, Period()).is_empty()
    st.markdown(f"**Report period ({PERIOD_LABELS.get(ctx.period_choice, ctx.period_choice)}):** "
                f"{summary.period_label}")
    metrics.render_metrics(summary.metrics, has_records=has_records)
    left, right = st.columns(2)
    with left:
        st.subheader("Failure reasons")
        if summary.failure_reasons.empty:
            st.caption("No failed ticket checks in this period.")
        else:
            st.dataframe(summary.failure_reasons.rename(columns={
                "label": "Outcome", "reason": "Reason", "credentials": "Distinct credentials",
                "records": "Checks"}).drop(columns=["outcome"]), hide_index=True, width="stretch")
    with right:
        st.subheader("Ticket verification distribution")
        if summary.distribution.empty:
            st.caption("No ticket checks in this period.")
        else:
            st.dataframe(charts.legend_frame(summary.distribution), hide_index=True, width="stretch")
    st.subheader("AI evaluation")
    if not history:
        st.info(NA_MESSAGE)
    else:
        st.dataframe(evaluation_frame(history[0], ctx.tz), hide_index=True, width="stretch")
        st.markdown("**Evaluation history**")
        st.dataframe(history_frame(history), hide_index=True, width="stretch")

    st.subheader("Exports")
    stamp = f"{now.astimezone(ctx.tz):%Y%m%d-%H%M}"
    st.caption("CSV files use the sidebar period; the table exports also use the validation status filter.")
    download("Download summary CSV", summary_frame(summary), "summary", stamp)
    if validations_over:
        st.warning(f"More than {EXPORT_CAP} validations in this period; choose a shorter period to export them.")
    else:
        export = tables.validation_table(validations, ctx.tz)
        if not validations.empty:
            export.insert(0, "validation_id", list(validations["validation_id"]))
            export["Time (UTC)"] = list(validations["validated_at"])
        download("Download validations CSV", export, "validations", stamp)
    if boardings_over:
        st.warning(f"More than {EXPORT_CAP} boardings in this period; choose a shorter period to export them.")
    else:
        download("Download boardings CSV", tables.boarding_table(boardings, ctx.tz), "boardings", stamp)
    st.caption("PDF export: not available (no PDF library is installed).")
