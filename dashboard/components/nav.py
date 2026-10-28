"""Page registry for the ``st.navigation`` router, the custom sidebar nav items and the quick actions.

Leaf module: imports only ``tokens`` and ``icons`` (never ``shell``, ``actions`` or ``theme``).
"""

from __future__ import annotations

import html
from typing import List, Tuple

import streamlit as st

from dashboard.components import icons, tokens

PAGE_DASHBOARD = "pages/0_Dashboard.py"
PAGE_LIVE = "pages/2_Live_Feed.py"
PAGE_TRACKING = "pages/3_Passenger_Tracking.py"
PAGE_TICKET = "pages/1_Ticket_Check.py"
PAGE_REPORTS = "pages/4_Reports.py"
PAGE_SETTINGS = "pages/5_Settings.py"

# Session-state keys shared by shell and the router (kept here so nav stays a leaf).
ROUTER_KEY = "_tpd_router"
TOAST_KEY = "_tpd_toast"

# (label, page, material icon, slug) in mockup order; labels equal the st.Page titles.
NAV_ITEMS: Tuple[Tuple[str, str, str, str], ...] = (
    ("Dashboard", PAGE_DASHBOARD, ":material/home:", "dashboard"),
    ("Live Feed", PAGE_LIVE, ":material/videocam:", "live"),
    ("Passenger Tracking", PAGE_TRACKING, ":material/group:", "tracking"),
    ("Ticket Checker", PAGE_TICKET, ":material/qr_code_scanner:", "ticket"),
    ("Reports", PAGE_REPORTS, ":material/bar_chart:", "reports"),
    ("Settings", PAGE_SETTINGS, ":material/settings:", "settings"),
)

# SVG equivalents for the static (non-router) nav list, which is plain st.html.
STATIC_NAV_ICONS = {"dashboard": icons.HOME, "live": icons.CAMERA, "tracking": icons.PEOPLE,
                    "ticket": icons.QR_SCAN, "reports": icons.BAR_CHART, "settings": icons.GEAR}

# (slug, title, subtitle, page, material icon, colour) for the Dashboard's Quick Actions tiles.
# The subtitles are static constants (styles.py turns them into CSS ``content``).
QUICK_ACTIONS: Tuple[Tuple[str, str, str, str, str, str], ...] = (
    ("scan", "Scan Ticket (QR)", "Validate passenger ticket", PAGE_TICKET, ":material/qr_code_scanner:",
     tokens.PRIMARY),
    ("live", "View Live Feed", "Monitor in real-time", PAGE_LIVE, ":material/videocam:", tokens.PURPLE),
    ("track", "Track Passenger", "Check tracking ID", PAGE_TRACKING, ":material/location_on:", tokens.SUCCESS),
    ("report", "Generate Report", "Download session report", PAGE_REPORTS, ":material/description:", tokens.SLATE),
)


def render_sidebar_nav(active_title: str, *, routed: bool) -> None:
    """The custom six-item sidebar nav; ``routed`` is ``shell.under_router()`` (passed in, nav never imports shell).

    Under the router each item is an ``st.page_link`` (client-side navigation keeps the session state)
    in a ``nav-<slug>`` container (``nav-active`` for the current page). Direct page runs cannot resolve
    sibling pages, so the items render as one static, non-link HTML list.
    """
    if routed:
        for label, page, icon, slug in NAV_ITEMS:
            box = st.sidebar.container(key="nav-active" if label == active_title else f"nav-{slug}")
            box.page_link(page, label=label, icon=icon, width="stretch")
        return
    items = []
    for label, _page, _icon, slug in NAV_ITEMS:
        active = label == active_title
        css = "tpd-nav-item tpd-nav-active" if active else "tpd-nav-item"
        current = ' aria-current="page"' if active else ""
        mark = icons.icon(STATIC_NAV_ICONS[slug], "#FFFFFF" if active else tokens.MUTED, 22)
        items.append(f'<div class="{css}" data-nav="{slug}"{current}>{mark}<span>{html.escape(label)}</span></div>')
    st.sidebar.html('<nav class="tpd-nav" aria-label="Pages">' + "".join(items) + "</nav>")


def build_pages() -> List[st.Page]:
    return [
        st.Page(page, title=label, icon=icon, url_path=url_path, default=(page == PAGE_DASHBOARD))
        for (label, page, icon, _slug), url_path in zip(NAV_ITEMS, _URL_PATHS)
    ]


_URL_PATHS = (None, "live-feed", "passenger-tracking", "ticket-checker", "reports", "settings")
