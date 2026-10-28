"""Period reports and CSV exports built from the same queries as the dashboard (read-only)."""

from __future__ import annotations

import sqlite3
from dataclasses import astuple, dataclass
from datetime import datetime
from typing import Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

from app.analytics import DashboardMetrics, P, Period, _frame, _in_clause, compute_metrics, recent_boardings
from app.analytics import count_validations, recent_validations
from app.database import parse_iso
from app.trends import failure_reasons, outcome_distribution

EXPORT_CAP = 100_000
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True)
class ReportSummary:
    metrics: DashboardMetrics
    distribution: pd.DataFrame
    failure_reasons: pd.DataFrame
    boardings_by_status: pd.DataFrame
    period_label: str
    generated_at: datetime


def period_label(period: Period, tz: ZoneInfo) -> str:
    if period.start is None and period.end is None:
        return "All time"
    fmt = "%Y-%m-%d %H:%M"
    start = parse_iso(period.start).astimezone(tz).strftime(fmt) if period.start else "beginning"
    end = parse_iso(period.end).astimezone(tz).strftime(fmt) if period.end else "now"
    return f"{start} – {end} ({tz.key})"


def report_summary(conn: sqlite3.Connection, period: Period, tz: ZoneInfo, now: datetime) -> ReportSummary:
    by_status = _frame(conn, "SELECT verification_status, COUNT(*) AS boardings FROM boarding_events WHERE "
                       + P("boarded_at") + " GROUP BY verification_status ORDER BY boardings DESC",
                       period.params())
    return ReportSummary(compute_metrics(conn, period), outcome_distribution(conn, period),
                         failure_reasons(conn, period), by_status, period_label(period, tz), now)


def summary_frame(summary: ReportSummary) -> pd.DataFrame:
    """Key/value rows of every number in the summary (for the summary CSV)."""
    rows = [("Report period", summary.period_label), ("Generated (UTC)", summary.generated_at.isoformat())]
    rows += list(zip(DashboardMetrics.LABELS, astuple(summary.metrics)))
    rows += [(f"Ticket checks: {r.label}", f"{r.count} ({r.percent}%)") for r in summary.distribution.itertuples()]
    rows += [(f"Failure: {r.label} – {r.reason}", f"{r.credentials} credentials / {r.records} checks")
             for r in summary.failure_reasons.itertuples()]
    rows += [(f"Boardings: {r.verification_status}", r.boardings) for r in summary.boardings_by_status.itertuples()]
    return pd.DataFrame([(k, str(v)) for k, v in rows], columns=["Item", "Value"])


def validations_export(conn: sqlite3.Connection, period: Period, statuses: Optional[Sequence[str]]
                       ) -> Tuple[pd.DataFrame, bool]:
    """All validations in the period matching ``statuses`` (newest first); ``(frame, over_cap)``."""
    total = count_validations(conn, period, statuses)
    if total > EXPORT_CAP:
        return pd.DataFrame(), True
    return recent_validations(conn, period, statuses, limit=max(total, 1)), False


def boardings_export(conn: sqlite3.Connection, period: Period, statuses: Optional[Sequence[str]]
                     ) -> Tuple[pd.DataFrame, bool]:
    params = {**period.params()}
    total = int(conn.execute("SELECT COUNT(*) FROM boarding_events WHERE " + P("boarded_at")
                             + _in_clause("verification_status", statuses, params, "s"), params).fetchone()[0])
    if total > EXPORT_CAP:
        return pd.DataFrame(), True
    return recent_boardings(conn, period, statuses, limit=max(total, 1)), False


def _neutralise(value):
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def csv_bytes(df: pd.DataFrame) -> bytes:
    """UTF-8 with BOM (Excel), spreadsheet formulas neutralised, no index."""
    safe = df.copy()
    for column in safe.columns:
        if safe[column].dtype == object or pd.api.types.is_string_dtype(safe[column]):
            safe[column] = safe[column].map(_neutralise)
    return safe.to_csv(index=False).encode("utf-8-sig")
