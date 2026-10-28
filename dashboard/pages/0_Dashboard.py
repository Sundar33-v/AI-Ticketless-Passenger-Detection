"""Dashboard: welcome banner, five KPI cards with period deltas, 10-minute trend, ticket-status donut,
live feed + latest failed check, recent validation results and quick actions, then the
AI Model Performance & Metrics Calculator.

The body is read-only apart from Notify Conductor, which marks an alert read and saves that
read status (alert_reads, via dashboard/components/notifications.py); nothing is sent. It
refreshes every ``dashboard.refresh_sec`` with one read connection per run. The
calculator below it is its own non-refreshing fragment; it writes only on an explicit
Save Evaluation (administrators). The evaluation history is also on Reports, the full
alert list on Live Feed and recent boardings on Passenger Tracking.
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

import streamlit as st  # noqa: E402

from app.alert_service import alert_groups  # noqa: E402
from app.analytics import Period, comparison_period, compute_metrics  # noqa: E402
from app.database import utc_now  # noqa: E402
from app.trends import minute_trend, outcome_distribution  # noqa: E402
from dashboard.components import (actions, charts, data, filters, metrics, metrics_panel, shell,  # noqa: E402
                                  status, tables)
from dashboard.components.shell import PageContext  # noqa: E402


def _body_fragment(ctx: PageContext) -> None:
    now = utc_now()
    period = ctx.period_at(now)
    cfg = ctx.cfg
    with shell.guard():
        with data.read_conn() as conn:
            health = shell.current_health(conn, cfg, now)
            current = compute_metrics(conn, period)
            prev_period = comparison_period(period, now)
            previous = compute_metrics(conn, prev_period) if prev_period else None
            trend = minute_trend(conn, now, ctx.tz)
            distribution = outcome_distribution(conn, period)
            groups = alert_groups(conn, period, now, cfg).groups
            # all-time check only for an empty period, so normal refreshes do no extra query
            has_records = current.is_empty() and not compute_metrics(conn, Period()).is_empty()

            shell.render_banner(ctx, health)
            metrics.render_kpi_row(current, previous,
                                   filters.PERIOD_LABELS.get(ctx.period_choice, ctx.period_choice),
                                   has_records=has_records)
            with st.container(key="dash-mid"):
                trend_col, status_col, feed_col = st.columns([1.15, 1, 1.6], gap="medium")
                with trend_col, st.container(border=True, key="card-trend"):
                    charts.render_trend_card(trend)
                with status_col, st.container(border=True, key="card-status"):
                    charts.render_status_card(distribution)
                with feed_col, st.container(border=True, key="card-feed"):
                    status.render_feed_card(health, cfg, groups, now, ctx.tz)
            with st.container(key="dash-bottom"):
                table_col, qa_col = st.columns([3, 1], gap="medium")
                with table_col:
                    tables.render_validation_card(conn, ctx, period)
                with qa_col:
                    actions.render_quick_actions_card()


# ---- module bottom: runs after _body_fragment is defined
ctx = shell.start_page("Dashboard", show_title=False)
st.fragment(run_every=ctx.cfg.dashboard.refresh_sec)(_body_fragment)(ctx)
metrics_panel.render(ctx)  # directly below Recent Validation Results + Quick Actions (own fragment, no refresh)
