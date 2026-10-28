"""Sidebar filters: reporting period (metrics, charts, alerts, tables) and validation status (tables only).

The main filters persist across pages: Streamlit drops the state of widgets
that are not rendered on the current page, so their values are mirrored in
``_flt_*`` session keys and restored before the widgets are created.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

import streamlit as st

from app.alert_service import outcome_label
from app.analytics import OUTCOMES, PERIOD_CHOICES, Period, period_for
from app.settings_schema import AppConfig

PERIOD_LABELS = {"today": "Today", "last_24_hours": "Last 24 hours", "yesterday": "Yesterday",
                 "last_7_days": "Last 7 days", "all": "All time", "custom": "Custom range"}
STATUS_HELP = "Filters the tables and CSV table exports. Metrics, charts and alerts always count every outcome."
MIRRORS = {"period": "_flt_period", "period_range": "_flt_range", "statuses": "_flt_statuses"}

Custom = Optional[Tuple[date, date]]


def _restore(key: str, mirror: bool, *, force: bool = False) -> bool:
    """Copy the mirror into the widget key when the key is missing (or after a page switch, ``force``).

    Returns True if the widget already has a value in Session State (so no default may be passed).
    """
    state = st.session_state
    if mirror and (force or key not in state) and MIRRORS.get(key) in state:
        state[key] = state[MIRRORS[key]]
    return key in state


def _normalise(picked, today: date) -> Tuple[date, date]:
    dates = list(picked) if isinstance(picked, (list, tuple)) else [picked]
    dates = [d for d in dates if d is not None] or [today]
    return dates[0], dates[-1]


def select_choice(cfg: AppConfig, now: datetime, *, key: str = "period", mirror: bool = False,
                  force: bool = False, target=None) -> Tuple[str, Custom]:
    """Period selectbox (and date range for "custom") in ``target`` (default: the sidebar);
    returns ``(choice, custom)``."""
    box = target if target is not None else st.sidebar
    kwargs = {}
    if not _restore(key, mirror, force=force):
        kwargs["index"] = PERIOD_CHOICES.index(cfg.dashboard.default_period)
    choice = box.selectbox("Period", PERIOD_CHOICES, format_func=PERIOD_LABELS.get, key=key, **kwargs)
    custom = None
    if choice == "custom":
        today = now.astimezone(ZoneInfo(cfg.app.timezone)).date()
        range_key = f"{key}_range"
        range_kwargs = {} if _restore(range_key, mirror, force=force) else {"value": (today, today)}
        picked = box.date_input("Date range", key=range_key, **range_kwargs)
        custom = _normalise(picked, today)
    if mirror:
        st.session_state[MIRRORS[key]] = choice
        if custom is not None:
            st.session_state[MIRRORS[f"{key}_range"]] = custom
    return choice, custom


def select_period(cfg: AppConfig, now: datetime, *, key: str = "period", target=None) -> Period:
    """Period selectbox in ``target`` (default: the sidebar; default value ``dashboard.default_period``)."""
    choice, custom = select_choice(cfg, now, key=key, target=target)
    return period_for(choice, cfg.app.timezone, now, custom=custom)


def select_statuses(*, key: str = "statuses", mirror: bool = False, force: bool = False,
                    target=None) -> List[str]:
    """Validation-status multiselect in ``target`` (default: the sidebar; default value: all six outcomes)."""
    box = target if target is not None else st.sidebar
    kwargs = {} if _restore(key, mirror, force=force) else {"default": list(OUTCOMES)}
    chosen = box.multiselect("Validation status (tables)", OUTCOMES, format_func=outcome_label, key=key,
                             help=STATUS_HELP, **kwargs)
    if mirror:
        st.session_state[MIRRORS[key]] = list(chosen)
    return chosen


def render_sidebar(cfg: AppConfig, now: datetime, *, page: str = "", target=None
                   ) -> Tuple[str, Custom, Tuple[str, ...]]:
    """Main filters in ``target`` (default: the sidebar). After a page switch the widgets are re-seeded
    from the mirrors (each page's widgets are new to Streamlit, so their old keyed state would otherwise
    be ignored)."""
    force = st.session_state.get("_flt_page") != page
    st.session_state["_flt_page"] = page
    choice, custom = select_choice(cfg, now, mirror=True, force=force, target=target)
    statuses = select_statuses(mirror=True, force=force, target=target)
    return choice, custom, tuple(statuses)


def current_selection(cfg: AppConfig, now: datetime) -> Tuple[str, Custom, Tuple[str, ...]]:
    """The persisted main selection without rendering widgets (pages without the filter sidebar)."""
    state = st.session_state
    choice = state.get(MIRRORS["period"], cfg.dashboard.default_period)
    custom = None
    if choice == "custom":
        today = now.astimezone(ZoneInfo(cfg.app.timezone)).date()
        custom = tuple(state.get(MIRRORS["period_range"], (today, today)))
    return choice, custom, tuple(state.get(MIRRORS["statuses"], OUTCOMES))
