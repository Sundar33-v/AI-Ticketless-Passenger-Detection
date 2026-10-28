"""Header notification bell: read state (session + ``alert_reads``) and the panel inside ``st.popover``.

The bell is a native ``st.popover`` (toggle, outside click, Escape and a portal above every
dashboard element come from Streamlit; opening it never reruns the script). The panel lists
only stored alert groups from ``app.alert_service.alert_groups``; nothing is synthesized.

Read state: ``st.session_state[READ_KEY]`` (``{group key: to_iso(last_at)}``) is the map every
reader uses (``shell.read_state`` delegates here). ``sync_read_state`` merges the saved map from
``alert_reads`` into it on each header refresh, and ``mark_read`` saves through
``app.alert_reads`` on an explicit Mark as read / Mark all as read / Notify Conductor only.
Both merges keep the newest timestamp per key (monotonic read state).

Must not import ``shell`` (``shell`` imports this module).
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import streamlit as st
from streamlit.delta_generator import DeltaGenerator

from app import alert_reads
from app.alert_service import AlertGroup, AlertGroupsResult, unread_count
from app.database import DatabaseError, to_iso, utc_now
from app.passenger_service import is_busy_error
from dashboard.components import data, nav
from dashboard.components import notification_cards as nc

logger = logging.getLogger("dashboard")

READ_KEY = "_alerts_read"
NOTICE_KEY = "_bell_notice"
LOADED_KEY = "_bell_loaded"
BELL_LABEL = "Notifications"
BELL_LIMIT = 15

LOADING_TEXT = "Loading notifications…"
ERROR_TEXT = "Notifications unavailable – the database could not be read. Retrying automatically."
EMPTY_TEXT = ("No notifications in this period. Alerts appear when a ticket check fails or a boarded passenger "
              "is not verified in time.")
LOAD_NOTICE = "Saved read status could not be loaded; showing this browser session's read status."
BUSY_MESSAGE = "Database busy – marked as read for this browser session only."
SAVE_ERROR_MESSAGE = "Could not save read status (database error) – marked as read for this browser session only."
LIVE_CAPTION = "See the Live Feed page for all alerts."

Item = Tuple[str, str]  # (group.key, to_iso(group.last_at))


def items_of(groups: Iterable[AlertGroup]) -> List[Item]:
    return [(g.key, to_iso(g.last_at)) for g in groups]


def session_read() -> dict:
    return st.session_state.setdefault(READ_KEY, {})


def _merge(read: Dict[str, str], pairs: Iterable[Tuple[str, str]]) -> None:
    for key, seen in pairs:
        if key not in read or seen > read[key]:
            read[key] = seen


def sync_read_state(conn: sqlite3.Connection, keys: Sequence[str]) -> bool:
    """Merge the saved read state of ``keys`` into the session map (max per key); False on a DB error."""
    try:
        stored = alert_reads.load_read_state(conn, keys)
    except sqlite3.Error as exc:
        logger.warning("Saved alert read status could not be loaded: %s", exc)
        st.session_state[NOTICE_KEY] = LOAD_NOTICE
        return False
    _merge(session_read(), stored.items())
    if st.session_state.get(NOTICE_KEY) == LOAD_NOTICE:
        st.session_state.pop(NOTICE_KEY, None)
    return True


def mark_read(items: Sequence[Item]) -> Optional[str]:
    """Mark ``(key, iso)`` pairs read in this session and save them; returns a message if not saved.

    Invalid pairs are dropped first (``alert_reads.valid_items``); nothing valid means no session
    change and no connection. The session map is updated before the write, so the UI reflects
    the mark even when the database is busy or fails.
    """
    clean = alert_reads.valid_items(items)
    if not clean:
        return None
    _merge(session_read(), clean)
    try:
        with data.write_conn() as conn:
            alert_reads.mark_read(conn, clean, utc_now())
    except (sqlite3.Error, OSError, DatabaseError) as exc:
        if is_busy_error(exc):
            logger.warning("Alert read status not saved (database busy): %s", exc)
            return BUSY_MESSAGE
        logger.warning("Alert read status not saved: %s", exc)
        return SAVE_ERROR_MESSAGE
    st.session_state.pop(NOTICE_KEY, None)
    return None


def _on_mark(items: Sequence[Item]) -> None:
    """Button callback (runs before the fragment body, so the same run shows the change)."""
    message = mark_read(items)
    if message:
        st.session_state[NOTICE_KEY] = message


@dataclass(frozen=True)
class BellSlots:
    body: DeltaGenerator    # st.empty() inside popover > st.container(key="bell-panel")
    badge: DeltaGenerator   # st.empty() inside tpd-bell, after the popover


def open_bell() -> BellSlots:
    """The constant bell skeleton (same widget tree in every state); filled by :func:`render_bell`."""
    with st.container(key="tpd-bell"):
        with st.popover(BELL_LABEL, icon=":material/notifications:", key="tpd_bell", wrap=True):
            with st.container(key="bell-panel"):
                st.html(nc.FOCUS_SCRIPT, unsafe_allow_javascript=True)
                body = st.empty()
        badge = st.empty()
    return BellSlots(body, badge)


def _digest(key: str) -> str:
    return hashlib.blake2s(key.encode(), digest_size=6).hexdigest()


def _is_unread(group: AlertGroup, read: Dict[str, str]) -> bool:
    return unread_count([group], read) == 1


def _ordered(groups: Iterable[AlertGroup], read: Dict[str, str]) -> List[AlertGroup]:
    """Unique by key; unread error/warning groups first, then the rest, each newest first."""
    unique: Dict[str, AlertGroup] = {}
    for group in groups:
        unique.setdefault(group.key, group)
    newest = sorted(unique.values(), key=lambda g: g.last_at, reverse=True)
    return [g for g in newest if _is_unread(g, read)] + [g for g in newest if not _is_unread(g, read)]


def render_bell(slots: BellSlots, alerts: Optional[AlertGroupsResult], unread: nc.BadgeState, tz: ZoneInfo) -> None:
    """Fill the badge and the panel: ``"loading"`` -> loading state, ``alerts None`` -> error state,
    otherwise the list (or the empty state). ``alerts.groups`` holds at most 3 x GROUP_CAP groups.
    """
    slots.badge.html(nc.badge_html(unread))
    if isinstance(unread, str):
        slots.body.html(nc.state_html("loading", LOADING_TEXT))
        return
    if alerts is None or unread is None:
        slots.body.html(nc.state_html("error", ERROR_TEXT))
        return
    if not alerts.groups:
        slots.body.html(nc.state_html("empty", EMPTY_TEXT))
        return
    read = session_read()
    ordered = _ordered(alerts.groups, read)
    shown = ordered[:BELL_LIMIT]
    # Mark all covers every error/warning group in the period, not only the ones shown.
    all_items = items_of(g for g in alerts.groups if g.severity in ("error", "warning"))
    with slots.body.container():
        with st.container(key="bell-head", horizontal=True):
            st.html(nc.bell_head_html(unread, len(ordered), alerts.truncated))
            st.button("Mark all as read", key="bell_read_all", type="tertiary", disabled=unread == 0,
                      on_click=_on_mark, args=(all_items,))
        notice = st.session_state.get(NOTICE_KEY)
        if notice:
            st.html(nc.state_html("warning", notice))
        for group in shown:
            digest = _digest(group.key)
            view = nc.view_of(group, read, tz)
            with st.container(key=f"bell-item-{digest}"):
                st.html(nc.item_html(view))
                if view.state == "Unread":
                    st.button("Mark as read", key=f"bell_read_{digest}", type="tertiary", on_click=_on_mark,
                              args=([(group.key, to_iso(group.last_at))],))
        if len(ordered) > len(shown) or alerts.truncated:
            total = "1000+" if alerts.truncated else str(len(ordered))
            st.caption(f"Showing {len(shown)} of {total}")
        if st.session_state.get(nav.ROUTER_KEY):
            st.page_link(nav.PAGE_LIVE, label="Open Live Feed for all alerts", icon=":material/videocam:")
        else:
            st.caption(LIVE_CAPTION)
