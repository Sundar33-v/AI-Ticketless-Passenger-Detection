"""dashboard/components/filters.py: widgets go to the given ``target`` container, never to ``st.sidebar``."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
import streamlit as st

from app.analytics import OUTCOMES
from dashboard.components import filters

NOW = datetime(2026, 1, 15, 5, 30, tzinfo=timezone.utc)


class Recorder:
    """Fake container: records every widget call and returns a canned value per widget type."""

    def __init__(self, **values):
        self.values = values
        self.calls = []

    def __getattr__(self, name):
        def widget(label, *args, **kwargs):
            self.calls.append((name, label, kwargs.get("key")))
            return self.values[name]
        return widget


class NoSidebar:
    def __getattr__(self, name):
        raise AssertionError(f"st.sidebar.{name} must not be used when a target is given")


@pytest.fixture(autouse=True)
def no_sidebar(monkeypatch):
    monkeypatch.setattr(st, "sidebar", NoSidebar())
    monkeypatch.setattr(st, "session_state", {})


def test_select_choice_uses_target(cfg):
    box = Recorder(selectbox="today")
    assert filters.select_choice(cfg, NOW, target=box) == ("today", None)
    assert box.calls == [("selectbox", "Period", "period")]


def test_select_choice_custom_range_uses_target(cfg):
    box = Recorder(selectbox="custom", date_input=(date(2026, 1, 1), date(2026, 1, 3)))
    choice, custom = filters.select_choice(cfg, NOW, key="p", target=box)
    assert (choice, custom) == ("custom", (date(2026, 1, 1), date(2026, 1, 3)))
    assert box.calls == [("selectbox", "Period", "p"), ("date_input", "Date range", "p_range")]


def test_select_statuses_uses_target():
    box = Recorder(multiselect=["EXPIRED"])
    assert filters.select_statuses(target=box) == ["EXPIRED"]
    assert box.calls == [("multiselect", "Validation status (tables)", "statuses")]


def test_select_period_forwards_target(cfg):
    box = Recorder(selectbox="all")
    period = filters.select_period(cfg, NOW, key="check_period", target=box)
    assert period.start is None and period.end is None
    assert box.calls == [("selectbox", "Period", "check_period")]


def test_render_sidebar_forwards_target_without_header(cfg):
    box = Recorder(selectbox="today", multiselect=list(OUTCOMES))
    choice, custom, statuses = filters.render_sidebar(cfg, NOW, page="Dashboard", target=box)
    assert (choice, custom, statuses) == ("today", None, tuple(OUTCOMES))
    assert [c[0] for c in box.calls] == ["selectbox", "multiselect"]   # no "Filters" header
    assert st.session_state[filters.MIRRORS["period"]] == "today"
