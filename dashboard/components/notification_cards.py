"""HTML builders for the header notification bell and panel (pure functions, no Streamlit).

Leaf module: imports only ``html``, ``tokens``, ``cards.track_label`` and ``app.alert_service``
(never ``shell``/``theme``/``actions``). Every value goes through ``html.escape``; nothing is
Markdown, so URLs in source labels are never auto-linked. ``data-*`` attributes are test hooks.
The CSS for these classes lives in ``styles.LAYOUT_CSS``.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import timezone
from typing import Literal, Mapping, Optional, Union
from zoneinfo import ZoneInfo

from app.alert_service import AlertGroup, unread_count
from dashboard.components.cards import track_label

BadgeState = Union[int, None, Literal["loading"]]  # int = unread count, None = DB error

KIND_LABELS = {
    "invalid_ticket": "Ticket check",
    "expired_ticket": "Ticket check",
    "unrecognised_qr": "QR scan",
    "unverified_passenger": "Boarding",
    "detection_stopped": "Detection",
    "camera_disconnected": "Detection",
    "tracking_failure": "Detection",
}
STATE_KINDS = ("loading", "error", "empty", "warning")

# Runs when the panel content mounts (each open); never steals focus from inside the panel.
FOCUS_SCRIPT = (
    "<script>"
    "requestAnimationFrame(function(){var p=document.querySelector('[data-testid=\"stPopoverBody\"]:has(.st-key-bell-panel)');\n"
    "var h=document.getElementById('tpd-bell-heading');if(p&&h&&!p.contains(document.activeElement)){h.focus({preventScroll:true});}});"
    "</script>"
)


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


@dataclass(frozen=True)
class NotificationView:
    title: str
    detail: str
    type_label: str
    time_text: str           # local "HH:MM:SS · DD Mon"
    time_iso: str            # UTC ISO value for <time datetime>
    track: Optional[str]     # "Track ID 003"
    ticket: Optional[str]    # "Ticket TKT-…"
    count: Optional[str]     # "×3 events"
    state: str               # Unread | Read | Info
    severity: str            # error | warning | info


def view_of(group: AlertGroup, read: Mapping[str, str], tz: ZoneInfo) -> NotificationView:
    """Display fields of one stored alert group (pure; ``read`` is the read-state map)."""
    if group.severity in ("error", "warning"):
        state = "Unread" if unread_count([group], read) else "Read"
    else:
        state = "Info"
    local = group.last_at.astimezone(tz)
    return NotificationView(
        title=group.title,
        detail=group.detail or group.message,
        type_label=KIND_LABELS.get(group.kind, "Alert"),
        time_text=local.strftime("%H:%M:%S · %d %b"),
        time_iso=group.last_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
        track=f"Track ID {track_label(group.track_id)}" if group.track_id is not None else None,
        ticket=f"Ticket {group.ticket_ref}" if group.ticket_ref else None,
        count=f"×{group.count} events" if group.count > 1 else None,
        state=state,
        severity=group.severity,
    )


def item_html(view: NotificationView) -> str:
    """One notification: head (dot, title, state pill, type pill), message and meta row."""
    state = view.state.lower()
    meta = [f'<time datetime="{_e(view.time_iso)}">{_e(view.time_text)}</time>']
    meta += [f"<span>{_e(text)}</span>" for text in (view.track, view.ticket, view.count) if text]
    return (f'<article class="tpd-note tpd-note--{_e(view.severity)} tpd-note--{_e(state)}" data-state="{_e(state)}" '
            f'aria-label="{_e(view.title)}, {_e(view.state)}, {_e(view.time_text)}">'
            '<div class="tpd-note-head"><span class="tpd-note-dot" aria-hidden="true"></span>'
            f'<span class="tpd-note-title">{_e(view.title)}</span>'
            f'<span class="tpd-note-pill tpd-note-pill--state">{_e(view.state)}</span>'
            f'<span class="tpd-note-pill tpd-note-pill--type">{_e(view.type_label)}</span></div>'
            f'<p class="tpd-note-msg">{_e(view.detail)}</p>'
            f'<div class="tpd-note-meta">{"".join(meta)}</div></article>')


def bell_head_html(unread: int, total: int, truncated: bool = False) -> str:
    """Panel heading (focus target) and the unread/total summary."""
    total_text = "1000+" if truncated else str(int(total))
    return ('<div class="tpd-bell-head"><h2 id="tpd-bell-heading" tabindex="-1">Notifications</h2>'
            f'<p role="status">{int(unread)} unread · {_e(total_text)} in this period</p></div>')


def badge_html(state: BadgeState) -> str:
    """The bell badge; exactly one ``.tpd-bell-badge-wrap`` wrapper (CSS anchor + ``data-count`` hook)."""
    if isinstance(state, str) and state == "loading":
        count, cls, text, sr, hidden = "…", " tpd-bell-badge--loading", "…", "Loading notifications", ""
    elif state is None:
        count, cls, text, sr, hidden = "–", " tpd-bell-badge--error", "–", "Notifications unavailable", ""
    elif isinstance(state, int) and not isinstance(state, bool) and state >= 0:
        if state == 0:
            count, cls, text, sr, hidden = "0", "", "0", "No unread notifications", " hidden"
        elif state > 99:
            count, cls, text, sr, hidden = "99+", "", "99+", "More than 99 unread notifications", ""
        else:
            noun = "notification" if state == 1 else "notifications"
            count, cls, text, sr, hidden = str(state), "", str(state), f"{state} unread {noun}", ""
    else:
        raise ValueError(f"invalid badge state: {state!r}")
    return (f'<span class="tpd-bell-badge-wrap" data-count="{count}">'
            f'<span class="tpd-bell-badge{cls}" aria-hidden="true"{hidden}>{text}</span>'
            f'<span class="tpd-sr">{sr}</span></span>')


def state_html(kind: str, message: str) -> str:
    """Loading / error / empty panel state, or an in-panel warning notice.

    ``role="alert"`` for errors, ``role="status"`` otherwise. The loading/error/empty states
    replace the heading, so their message carries the ``tpd-bell-heading`` focus target;
    the warning notice is shown under the real heading and gets no id (no duplicate ids).
    """
    if kind not in STATE_KINDS:
        raise ValueError(f"invalid state kind: {kind!r}")
    role = "alert" if kind == "error" else "status"
    target = '' if kind == "warning" else ' id="tpd-bell-heading" tabindex="-1"'
    return (f'<div class="tpd-bell-state tpd-bell-state--{kind}" role="{role}" data-state="{kind}">'
            f'<p{target}>{_e(message)}</p></div>')
